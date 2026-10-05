"""RC policy: local observations, bounded Canary presets and reversible switches."""
from __future__ import annotations
import hashlib
import json
import math
import time
import threading

POLICY_VERSION = "rc1-policy-v1"
LOCK = threading.RLock()
PRESETS = {
    "conservative": {"thread_memory_enable": True, "thread_worker_enable": True, "thread_mode": "shadow", "thread_canary_percent": 0, "consistency_mode": "shadow", "thread_projection_enable": True, "thread_claim_enable": True, "thread_projection_min_confidence": .95, "thread_llm_arbitration_enable": False, "thread_worker_interval_seconds": 300, "thread_subgraph_nodes": 3, "thread_evolution_nodes": 5, "thread_prospective_enable": False, "thread_observation_retention_days": 120},
    "balanced": {"thread_memory_enable": True, "thread_worker_enable": True, "thread_mode": "canary", "thread_canary_percent": 10, "consistency_mode": "shadow", "thread_projection_enable": True, "thread_claim_enable": True, "thread_projection_min_confidence": .84, "thread_llm_arbitration_enable": True, "thread_llm_daily_budget": 30, "thread_llm_batch_size": 3, "thread_worker_interval_seconds": 180, "thread_subgraph_nodes": 5, "thread_evolution_nodes": 5, "thread_prospective_enable": True, "thread_observation_retention_days": 180},
    "effect": {"thread_memory_enable": True, "thread_worker_enable": True, "thread_mode": "canary", "thread_canary_percent": 20, "consistency_mode": "shadow", "thread_projection_enable": True, "thread_claim_enable": True, "thread_projection_min_confidence": .84, "thread_llm_arbitration_enable": True, "thread_llm_daily_budget": 60, "thread_llm_batch_size": 5, "thread_worker_interval_seconds": 120, "thread_subgraph_nodes": 5, "thread_evolution_nodes": 7, "thread_prospective_enable": True, "thread_observation_retention_days": 180},
    "cost": {"thread_memory_enable": True, "thread_worker_enable": True, "thread_mode": "shadow", "thread_canary_percent": 0, "consistency_mode": "shadow", "thread_projection_enable": True, "thread_claim_enable": True, "thread_projection_min_confidence": .90, "thread_llm_arbitration_enable": False, "thread_worker_interval_seconds": 900, "thread_subgraph_nodes": 3, "thread_evolution_nodes": 3, "thread_prospective_enable": False, "thread_observation_retention_days": 90},
}
FIELDS = frozenset(k for p in PRESETS.values() for k in p) | {"thread_canary_percent"}

def fingerprint(plugin):
    values = {k: getattr(plugin, k, getattr(plugin, "config", {}).get(k)) for k in sorted(FIELDS - {"thread_mode", "consistency_mode"})}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:20]

def gates(plugin):
    """Only real persisted Canary observations count; no client-submitted metrics."""
    reasons = []
    episodes = getattr(plugin, "_episodes", None)
    scope_id = str(getattr(plugin, "character_name", None) or "default")
    try:
        rows = episodes.thread_query_observations(scope_id=scope_id, limit=500)
    except Exception:
        rows = []
    policy = fingerprint(plugin)
    now = time.time()
    rows = [r for r in rows if isinstance(r.get("metrics"), dict) and r["metrics"].get("mode") == "canary" and r["metrics"].get("policy_fingerprint") == policy and now - 7*86400 <= float(r.get("created_ts") or 0) <= now]
    injected = [r for r in rows if r["metrics"].get("injected") is True]
    span = max([float(r.get("created_ts") or 0) for r in rows], default=0) - min([float(r.get("created_ts") or 0) for r in rows], default=0)
    if len(rows) < 100 or len(injected) < 30 or span < 86400:
        reasons.append("需要同策略至少100条在线Canary观测、30次真实注入、跨度24小时")
    if any(r["metrics"].get("outcome") == "fail_open" or r["metrics"].get("extra_llm_calls") != 0 or r["metrics"].get("extra_recall_calls") != 0 for r in rows):
        reasons.append("在线观测存在回退或额外调用")
    # Current evaluator generates source-holdout cases, not qualified human gold.
    # Until a validated gold importer exists this release cannot certify Active.
    reasons.append("真实金标校准及最终完整5.x基线尚未验收；预览候选不授予Active资格")
    return {"policy_version": POLICY_VERSION, "policy_fingerprint": policy, "scope_id": scope_id, "active_qualified": False, "active_reasons": reasons, "online_samples": len(rows), "online_injected": len(injected), "online_span_seconds": span, "guarded_qualified": False, "guarded_reason": "无正向真实数据净收益与完整修复审计验证；仅off/shadow", "presets": PRESETS}

def validate(values, plugin, *, manual=False):
    from pathlib import Path
    schema = json.loads((Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8"))
    for key, value in values.items():
        if key not in FIELDS:
            continue
        meta = schema[key]
        kind = meta.get("type")
        if kind == "bool" and type(value) is not bool:
            raise ValueError(key + " must be boolean")
        if kind == "int" and type(value) is not int:
            raise ValueError(key + " must be integer")
        if kind == "float" and type(value) not in (int, float):
            raise ValueError(key + " must be numeric")
        if kind == "string" and type(value) is not str:
            raise ValueError(key + " must be string")
        if kind in {"int", "float"} and (not math.isfinite(value) or value < meta.get("min", -math.inf) or value > meta.get("max", math.inf)):
            raise ValueError(key + " outside allowed range")
    if "consistency_mode" in values and values["consistency_mode"] not in {"off", "shadow"}:
        raise ValueError("guarded未通过验证；只允许off/shadow")
    if "thread_mode" in values:
        mode = values["thread_mode"]
        if mode not in {"shadow", "canary", "active"}:
            raise ValueError("invalid thread_mode")
        if mode == "active" and (not manual or not gates(plugin)["active_qualified"]):
            raise ValueError("Active需要人工操作及合格在线与金标证据；当前预览门槛未通过")
    for key in ("thread_subgraph_nodes", "thread_evolution_nodes"):
        if key in values and (type(values[key]) is not int or not 3 <= values[key] <= 7):
            raise ValueError(key + " must be an integer in 3..7")
    for key, value in values.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(key + " must be finite")

def hot_apply(plugin, values):
    for key, value in values.items():
        if hasattr(plugin, key) or key in FIELDS:
            setattr(plugin, key, value)
    service = getattr(getattr(plugin, "_episodes", None), "consistency_service", None)
    if service is not None and "consistency_mode" in values:
        service.mode = values["consistency_mode"]
    lifecycle = getattr(plugin, "_apply_thread_runtime_policy", None)
    if callable(lifecycle) and {"thread_memory_enable", "thread_worker_enable"} & set(values):
        lifecycle()

def snapshot(plugin):
    with LOCK:
        current = {k: getattr(plugin, k, plugin.config.get(k)) for k in sorted(FIELDS)}
        history = list(plugin.config.get("_thread_policy_history", []))[-50:]
        revision = hashlib.sha256(json.dumps([current, history], sort_keys=True).encode()).hexdigest()
        undone = {r.get("rollback_of") for r in history if r.get("rollback_of")}
        target = next((r for r in reversed(history) if r.get("source") != "rollback" and record_id(r) not in undone), None)
        return {"current": current, "history": history, "revision": revision,
                "matching_preset": next((name for name, values in PRESETS.items() if all(current[k] == v for k, v in values.items())), None),
                "rollback_available": target is not None, "rollback_target": target}


def record_id(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def rollback(plugin, *, expected_revision=None, manual=False):
    with LOCK:
        state = snapshot(plugin)
        target = state["rollback_target"]
        if target is None:
            raise ValueError("no policy change to roll back")
        return switch(plugin, target["before"], source="rollback", manual=manual,
                      expected_revision=expected_revision, rollback_of=record_id(target))


def switch(plugin, values, *, source, manual=False, expected_revision=None, rollback_of=None):
    if not values or set(values) - FIELDS or any(type(v) not in (str, int, float, bool) for v in values.values()):
        raise ValueError("only allowlisted scalar strategy fields are accepted")
    values = dict(values)
    if values.get("thread_mode") == "canary":
        # An explicit Canary switch must not leave the master/worker switches
        # disabled, which would produce a UI-only mode change with no runtime.
        values.setdefault("thread_memory_enable", True)
        values.setdefault("thread_worker_enable", True)
    validate(values, plugin, manual=manual)
    with LOCK:
        config = plugin.config
        if expected_revision is not None and expected_revision != snapshot(plugin)["revision"]:
            raise ValueError("策略已在其他操作中更新，请刷新后重试")
        values = {k: v for k, v in values.items() if getattr(plugin, k, config.get(k)) != v}
        if not values and rollback_of is None:
            return {"changed": {}, "record": None, "gates": gates(plugin)}
        save = getattr(config, "save_config", None)
        if not callable(save):
            raise RuntimeError("configuration persistence unavailable")
        before = dict(config)
        old_runtime = {k: getattr(plugin, k, config.get(k)) for k in values}
        history = list(config.get("_thread_policy_history", []))[-49:]
        record = {"time": time.time(), "policy_version": POLICY_VERSION, "source": source, "before": old_runtime, "after": dict(values)}
        if rollback_of is not None:
            record["rollback_of"] = rollback_of
        try:
            config.update(values)
            config["_thread_policy_history"] = history + [record]
            save()
            hot_apply(plugin, values)
        except Exception:
            config.clear(); config.update(before)
            hot_apply(plugin, old_runtime)
            try:
                save()
            except Exception:
                pass
            raise
        return {"changed": values, "record": record, "gates": gates(plugin)}
