"""Xinchao 2.0 compatible deterministic state engine."""
from __future__ import annotations

import copy
import math
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo


SATURATE_CEIL = 0.80
SATURATE_FLOOR = 0.65
SCHEMA_VERSION = 7

DIMENSIONS: dict[str, dict[str, Any]] = {
    "possess": {"label": "亲密、占有与靠近", "grow": 0.105, "satisfy": 0.30, "night": 0.4, "activation_half_life": 6.0},
    "monitor": {"label": "牵挂、想知道对方近况", "grow": 0.090, "satisfy": 0.70, "activation_half_life": 5.0},
    "crave": {"label": "依恋与身体接近", "grow": 0.060, "satisfy": 0.35, "activation_half_life": 4.0},
    "share": {"label": "想分享自己的发现和感受", "grow": 0.045, "satisfy": 0.40, "activation_half_life": 3.5},
    "libido": {
        "label": "身体欲望", "grow": 0.020, "satisfy": 0.15, "night": 0.4,
        "inhibited_by": {"reflection": 0.96, "curiosity": 0.95, "boredom": 0.93},
        "activation_half_life": 3.0,
    },
    "curiosity": {"label": "好奇、想探索新东西", "grow": 0.030, "satisfy": 0.45, "activation_half_life": 2.5},
    "boredom": {"label": "无聊、想找点事情做", "grow": 0.030, "satisfy": 0.25, "activation_half_life": 2.0},
    "social": {"label": "想聊天、想接触热闹", "grow": 0.025, "satisfy": 0.40, "activation_half_life": 2.0},
    "duty": {"label": "责任感、想推进未完成的事", "grow": 0.022, "satisfy": 0.50, "activation_half_life": 6.0},
    "reflection": {"label": "想沉淀、整理和理解自己", "grow": 0.013, "satisfy": 0.35, "activation_half_life": 8.0},
    "grieve": {"label": "难过与失落", "grow": 0.0, "satisfy": 0.60, "dawn_freeze": False, "kind": "affect", "decay_half_life": 36.0, "activation_half_life": 12.0},
    "anger": {"label": "生气与不满", "grow": 0.0, "satisfy": 0.40, "dawn_freeze": False, "kind": "affect", "decay_half_life": 12.0, "activation_half_life": 5.0},
}
DRIVE_KEYS = tuple(DIMENSIONS)


def _initial_pressure(key: str) -> float:
    return 0.0 if DIMENSIONS.get(key, {}).get("kind") == "affect" else 0.15


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any, fallback: datetime | None = None) -> datetime:
    try:
        text = str(value or "").replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return fallback or datetime.now(timezone.utc)


def new_thought_pool() -> dict[str, list[dict[str, Any]]]:
    return {"flash": [], "obsessions": []}


def new_state(now: datetime | None = None) -> dict[str, Any]:
    at = _iso(now)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": 0,
        "consciousness": "awake",
        "lastConversationAt": at,
        "lastHeartbeatAt": None,
        "lastSettledAt": at,
        "sleepStartedAt": None,
        "drives": {key: _initial_pressure(key) for key in DRIVE_KEYS},
        "driveActivations": {key: 0.0 for key in DRIVE_KEYS},
        "driveMeta": {
            key: {"at": at, "reason": "初始化", "source": "init", "delta": 0.0}
            for key in DRIVE_KEYS
        },
        "driveHistory": [],
        "thoughtPool": new_thought_pool(),
        "fatigue": 0.0,
        "recentDreams": [],
        "dreamUsage": {},
        "lastBarkAt": None,
        "lastDreamBarkAt": None,
        "lastAutonomousBarkAt": None,
        "barkUsage": {},
        "recentBarkMessages": [],
        "lastDreamPushMessage": None,
        "lastDreamPushDreamId": None,
        "lastAutonomousMessage": None,
        "lastDaytimeEmergenceAt": None,
        "nextDaytimeEmergenceAt": None,
        "daytimeEmergenceUsage": {},
        "lastDaytimeMessage": None,
        "recentDaytimeMemories": [],
        "pendingAwareness": None,
        "pendingOfflineAfterglow": None,
        "lastOfflineSessionId": None,
        "offlineEffectLedger": [],
        "recentEvents": [],
        "lastProactiveAt": None,
        "proactiveUsage": {},
        "recentProactiveMessages": [],
        "lastUmo": "",
    }


def normalize_state(value: Any, now: datetime | None = None) -> dict[str, Any]:
    base = new_state(now)
    if not isinstance(value, dict):
        return base
    state = copy.deepcopy(base)
    state.update({key: copy.deepcopy(val) for key, val in value.items() if key in state})
    state["schemaVersion"] = SCHEMA_VERSION
    state["revision"] = max(0, int(state.get("revision") or 0))
    state["consciousness"] = "sleeping" if state.get("consciousness") == "sleeping" else "awake"
    drives = value.get("drives") if isinstance(value.get("drives"), dict) else {}
    activations = value.get("driveActivations") if isinstance(value.get("driveActivations"), dict) else {}
    state["drives"] = {
        key: round(_clamp(_number(drives.get(key), _initial_pressure(key))), 4)
        for key in DRIVE_KEYS
    }
    state["driveActivations"] = {
        key: round(_clamp(_number(activations.get(key), 0.0)), 4) for key in DRIVE_KEYS
    }
    raw_meta = value.get("driveMeta") if isinstance(value.get("driveMeta"), dict) else {}
    state["driveMeta"] = {}
    for key in DRIVE_KEYS:
        item = raw_meta.get(key) if isinstance(raw_meta.get(key), dict) else {}
        state["driveMeta"][key] = {
            "at": str(item.get("at") or state["lastSettledAt"] or _iso(now)),
            "reason": str(item.get("reason") or "旧状态迁移")[:80],
            "source": str(item.get("source") or "migration")[:32],
            "delta": round(_clamp(_number(item.get("delta"), 0.0), -1.0, 1.0), 4),
        }
    state["driveHistory"] = [
        dict(item) for item in list(value.get("driveHistory") or [])
        if isinstance(item, dict)
    ][-80:]
    pool = value.get("thoughtPool") if isinstance(value.get("thoughtPool"), dict) else {}
    state["thoughtPool"] = {
        "flash": [
            _normalize_thought(item, now, obsession=False)
            for item in list(pool.get("flash") or []) if isinstance(item, dict)
        ][-8:],
        "obsessions": [
            _normalize_thought(item, now, obsession=True)
            for item in list(pool.get("obsessions") or []) if isinstance(item, dict)
        ][-3:],
    }
    state["fatigue"] = round(_clamp(float(state.get("fatigue") or 0), 0, 0.3), 4)
    for key, limit in (
        ("recentDreams", 20),
        ("recentEvents", 20),
        ("recentProactiveMessages", 8),
        ("recentBarkMessages", 8),
        ("recentDaytimeMemories", 32),
        ("offlineEffectLedger", 40),
    ):
        state[key] = list(state.get(key) or [])[-limit:]
    for key in ("dreamUsage", "proactiveUsage", "barkUsage", "daytimeEmergenceUsage"):
        state[key] = dict(state.get(key) or {})
    if not state["recentBarkMessages"] and state["recentProactiveMessages"]:
        state["recentBarkMessages"] = [
            {
                "at": item.get("at"),
                "kind": str(item.get("kind") or "autonomous_thought"),
                "message": item.get("message"),
            }
            for item in state["recentProactiveMessages"]
            if isinstance(item, dict) and item.get("message")
        ][-8:]
        state["lastAutonomousBarkAt"] = (
            state.get("lastAutonomousBarkAt")
            or state.get("lastProactiveAt")
            or state["recentBarkMessages"][-1].get("at")
        )
        state["lastAutonomousMessage"] = (
            state.get("lastAutonomousMessage")
            or state["recentBarkMessages"][-1].get("message")
        )
    return state


def effective_drive_value(state: dict[str, Any], key: str) -> float:
    pressure = _clamp(dict(state.get("drives") or {}).get(key, 0.0))
    activation = _clamp(dict(state.get("driveActivations") or {}).get(key, 0.0))
    activation_weight = 0.65 if DIMENSIONS.get(key, {}).get("kind") == "affect" else 0.55
    return round(_clamp(pressure + activation * (1.0 - pressure) * activation_weight), 4)


def _record_drive_change(
    state: dict[str, Any], key: str, before_pressure: float, before_activation: float,
    reason: str, source: str, now: datetime, force_history: bool = False,
) -> None:
    after_pressure = _clamp(state["drives"].get(key, 0.0))
    after_activation = _clamp(state["driveActivations"].get(key, 0.0))
    delta = (after_pressure + after_activation) - (before_pressure + before_activation)
    if abs(delta) < 0.00005:
        return
    if source == "settle" and abs(delta) < 0.02:
        return
    item = {
        "at": _iso(now),
        "reason": str(reason or "状态变化")[:80],
        "source": str(source or "engine")[:32],
        "delta": round(delta, 4),
    }
    state.setdefault("driveMeta", {})[key] = item
    if force_history or abs(delta) >= 0.02:
        state.setdefault("driveHistory", []).append({
            "key": key,
            "pressureBefore": round(before_pressure, 4),
            "pressureAfter": round(after_pressure, 4),
            "activationBefore": round(before_activation, 4),
            "activationAfter": round(after_activation, 4),
            **item,
        })
        state["driveHistory"] = state["driveHistory"][-80:]


def _normalize_thought(item: dict[str, Any], now: datetime | None, obsession: bool) -> dict[str, Any]:
    at = _iso(now)
    result = {
        "key": str(item.get("key") or ""),
        "text": str(item.get("text") or "")[:180],
        "intensity": round(_clamp(item.get("intensity"), 0.0, 1.0), 4),
        "ageHours": max(0.0, _number(item.get("ageHours"), _number(item.get("age"), 0.0) * 0.25)),
        "reinforcements": max(1, int(_number(item.get("reinforcements"), 1))),
        "createdAt": str(item.get("createdAt") or at),
        "updatedAt": str(item.get("updatedAt") or at),
    }
    if obsession:
        result["feedbacks"] = max(0, int(_number(item.get("feedbacks"), 0)))
    return result


def add_flash_thought(
    pool: dict[str, Any], key: str, text: str, intensity: float = 0.70,
    now: datetime | None = None,
) -> None:
    if key not in DRIVE_KEYS:
        return
    clean = " ".join(str(text or "").split())[:180]
    if not clean:
        return
    current = now or datetime.now(timezone.utc)
    at = _iso(current)
    obsessions = list(pool.get("obsessions") or [])
    for item in obsessions:
        if item.get("key") == key and message_similarity(clean, str(item.get("text") or "")) >= 0.56:
            item["text"] = clean
            item["intensity"] = round(_clamp(max(_number(item.get("intensity")), intensity) + 0.08), 4)
            item["reinforcements"] = int(item.get("reinforcements") or 1) + 1
            item["updatedAt"] = at
            pool["obsessions"] = obsessions[-3:]
            return
    flash = list(pool.get("flash") or [])
    for item in flash:
        if item.get("key") == key and message_similarity(clean, str(item.get("text") or "")) >= 0.56:
            item["text"] = clean
            item["intensity"] = round(_clamp(max(_number(item.get("intensity")), intensity) + 0.10), 4)
            item["reinforcements"] = int(item.get("reinforcements") or 1) + 1
            item["updatedAt"] = at
            pool["flash"] = flash
            return
    if len(flash) >= 8:
        flash.sort(key=lambda item: float(item.get("intensity") or 0))
        flash.pop(0)
    flash.append({
        "key": key, "text": clean, "intensity": round(_clamp(intensity), 4),
        "ageHours": 0.0, "reinforcements": 1, "createdAt": at, "updatedAt": at,
    })
    pool["flash"] = flash


def tick_thought_pool(
    pool: dict[str, Any], elapsed_hours: float = 0.25,
    now: datetime | None = None,
) -> dict[str, float]:
    elapsed = max(0.0, float(elapsed_hours or 0.0))
    current = now or datetime.now(timezone.utc)
    flash = []
    for item in list(pool.get("flash") or []):
        next_item = _normalize_thought(item, current, obsession=False)
        next_item["intensity"] = round(
            _clamp(next_item["intensity"] * (0.5 ** (elapsed / 6.0))), 4
        )
        next_item["ageHours"] = round(next_item["ageHours"] + elapsed, 4)
        if next_item["intensity"] > 0.08:
            flash.append(next_item)
    obsessions = [
        _normalize_thought(item, current, obsession=True)
        for item in list(pool.get("obsessions") or []) if isinstance(item, dict)
    ]
    kept = []
    for item in flash:
        reinforced = int(item.get("reinforcements") or 1) >= 3 and item["intensity"] >= 0.55
        persistent = item["ageHours"] >= 6.0 and item["intensity"] >= 0.68
        if (reinforced or persistent) and len(obsessions) < 3:
            obsessions.append({
                "key": item.get("key"), "text": item.get("text", ""),
                "intensity": item["intensity"], "feedbacks": 0,
                "ageHours": item["ageHours"],
                "reinforcements": item.get("reinforcements", 1),
                "createdAt": item.get("createdAt") or _iso(current),
                "updatedAt": item.get("updatedAt") or _iso(current),
            })
        else:
            kept.append(item)
    feedbacks: dict[str, float] = {}
    next_obsessions = []
    for item in obsessions:
        next_item = _normalize_thought(item, current, obsession=True)
        next_item["intensity"] = round(
            _clamp(next_item["intensity"] * (0.5 ** (elapsed / 72.0))), 4
        )
        next_item["ageHours"] = round(next_item["ageHours"] + elapsed, 4)
        key = str(next_item.get("key") or "")
        if key in DRIVE_KEYS and elapsed > 0:
            feedbacks[key] = feedbacks.get(key, 0.0) + min(
                0.03, 0.012 * next_item["intensity"] * elapsed
            )
        if next_item["intensity"] >= 0.18 and next_item["ageHours"] <= 240:
            next_obsessions.append(next_item)
    pool["flash"] = kept
    pool["obsessions"] = next_obsessions[:3]
    return feedbacks


def settle_state(
    value: dict[str, Any], now: datetime | None = None, sleep_after_minutes: int = 90,
    time_zone: str = "Asia/Shanghai", dawn_start: int = 1, dawn_end: int = 8,
) -> tuple[dict[str, Any], dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    state = normalize_state(value, now)
    previous_revision = state["revision"]
    elapsed_hours = max(0.0, (now - _parse_iso(state["lastSettledAt"], now)).total_seconds() / 3600)
    try:
        hour = now.astimezone(ZoneInfo(time_zone)).hour
    except Exception:
        hour = now.astimezone().hour
    is_dawn = dawn_start <= hour < dawn_end
    is_night = hour >= 22 or hour < 6
    fatigue_multiplier = 1 - _clamp(state["fatigue"], 0, 0.3)
    changed = elapsed_hours > 0
    for key, dimension in DIMENSIONS.items():
        current = _clamp(state["drives"].get(key, _initial_pressure(key)))
        current_activation = _clamp(state["driveActivations"].get(key, 0.0))
        activation_half_life = max(0.5, float(dimension.get("activation_half_life", 4.0)))
        next_activation = current_activation * (0.5 ** (elapsed_hours / activation_half_life))
        if dimension.get("kind") == "affect":
            half_life = max(1.0, float(dimension.get("decay_half_life", 24.0)))
            next_value = current * (0.5 ** (elapsed_hours / half_life))
            reason = "情绪余波自然消退"
        elif is_dawn and dimension.get("dawn_freeze", True):
            next_value = current
            reason = "清晨静默期，仅保留原有压力"
        elif current > SATURATE_CEIL:
            next_value = SATURATE_CEIL + (current - SATURATE_CEIL) * math.exp(-0.12 * elapsed_hours)
            reason = "高位压力缓慢回落"
        else:
            rate = float(dimension["grow"])
            if is_night:
                rate *= float(dimension.get("night", 1.0))
            # Preserve the original initial slope while approaching the ceiling
            # continuously instead of making every need hit 0.80 within hours.
            rate *= fatigue_multiplier
            curve = max(0.0001, SATURATE_CEIL - 0.15)
            next_value = SATURATE_CEIL - (SATURATE_CEIL - current) * math.exp(
                -(rate / curve) * elapsed_hours
            )
            reason = "未满足需要随时间积累"
        state["drives"][key] = round(next_value, 4)
        state["driveActivations"][key] = round(_clamp(next_activation), 4)
        _record_drive_change(
            state, key, current, current_activation, reason, "settle", now,
            force_history=False,
        )
    feedbacks = tick_thought_pool(state["thoughtPool"], elapsed_hours, now)
    for key, amount in feedbacks.items():
        if key in DRIVE_KEYS:
            before_pressure = state["drives"][key]
            before_activation = state["driveActivations"][key]
            state["driveActivations"][key] = round(_clamp(before_activation + amount), 4)
            _record_drive_change(
                state, key, before_pressure, before_activation,
                "执念维持了当前心理激活", "thought", now,
            )
    if state["consciousness"] == "sleeping":
        state["fatigue"] = round(_clamp(state["fatigue"] - 0.03 * elapsed_hours, 0, 0.3), 4)
    else:
        average = sum(effective_drive_value(state, key) for key in DRIVE_KEYS) / len(DRIVE_KEYS)
        fatigue_delta = 0.004 * elapsed_hours if average > 0.55 else -0.003 * elapsed_hours
        state["fatigue"] = round(_clamp(state["fatigue"] + fatigue_delta, 0, 0.3), 4)
    idle_minutes = max(0.0, (now - _parse_iso(state["lastConversationAt"], now)).total_seconds() / 60)
    entered_sleep = idle_minutes >= sleep_after_minutes and state["consciousness"] != "sleeping"
    if entered_sleep:
        state["consciousness"] = "sleeping"
        state["sleepStartedAt"] = _iso(now)
    state["lastSettledAt"] = _iso(now)
    if changed or entered_sleep:
        state["revision"] = previous_revision + 1
    return state, {
        "changed": state["revision"] != previous_revision,
        "elapsedHours": elapsed_hours, "idleMinutes": idle_minutes,
        "enteredSleep": entered_sleep,
    }


def apply_conversation_event(
    value: dict[str, Any], event: dict[str, Any] | None = None,
    now: datetime | None = None, umo: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    event = event or {}
    state = normalize_state(value, now)
    was_sleeping = state["consciousness"] == "sleeping"
    sleep_started = state.get("sleepStartedAt")
    state["consciousness"] = "awake"
    state["lastConversationAt"] = _iso(now)
    state["lastHeartbeatAt"] = _iso(now)
    state["lastSettledAt"] = _iso(now)
    state["sleepStartedAt"] = None
    state["lastUmo"] = str(umo or state.get("lastUmo") or "")
    if was_sleeping:
        latest = state["recentDreams"][-1] if state["recentDreams"] else None
        belongs = bool(
            latest and sleep_started
            and _parse_iso(latest.get("createdAt"), now) >= _parse_iso(sleep_started, now)
        )
        state["pendingAwareness"] = {
            "createdAt": _iso(now),
            "dreamId": latest.get("id") if belongs else None,
            "residue": latest.get("residue") if belongs else None,
            "awareness": latest.get("awareness") if belongs else None,
        }
    activation_levels = {
        str(key): _clamp(value)
        for key, value in dict(event.get("activationLevels") or {}).items()
        if str(key) in DRIVE_KEYS
    }
    for key in list(event.get("activatedDrives") or []):
        if key in DRIVE_KEYS:
            activation_levels.setdefault(key, 0.55)
    for key, level in activation_levels.items():
        before_pressure = state["drives"][key]
        before_activation = state["driveActivations"][key]
        state["driveActivations"][key] = round(
            _clamp(max(before_activation * 0.85, level)), 4
        )
        _record_drive_change(
            state, key, before_pressure, before_activation,
            "本轮事件将这一倾向推到前景", "activation", now,
            force_history=True,
        )
    for key, delta in dict(event.get("driveDeltas") or {}).items():
        if key in DRIVE_KEYS:
            try:
                before_pressure = state["drives"][key]
                before_activation = state["driveActivations"][key]
                state["drives"][key] = round(_clamp(state["drives"][key] + float(delta)), 4)
                _record_drive_change(
                    state, key, before_pressure, before_activation,
                    "本轮事件留下持续影响", "event_delta", now,
                    force_history=True,
                )
            except (TypeError, ValueError):
                pass
    satisfaction_levels = {
        str(key): _clamp(value)
        for key, value in dict(event.get("satisfactionLevels") or {}).items()
        if str(key) in DRIVE_KEYS
    }
    for key in list(event.get("satisfiedDrives") or []):
        if key in DRIVE_KEYS:
            satisfaction_levels.setdefault(key, 0.45)
    for key, strength in satisfaction_levels.items():
        dimension = DIMENSIONS[key]
        before_pressure = state["drives"][key]
        before_activation = state["driveActivations"][key]
        full_multiplier = _clamp(dimension.get("satisfy", 0.40))
        relief_multiplier = 1.0 - strength * (1.0 - full_multiplier)
        state["drives"][key] = round(_clamp(before_pressure * relief_multiplier), 4)
        state["driveActivations"][key] = round(
            _clamp(before_activation * (1.0 - 0.22 * strength)), 4
        )
        _record_drive_change(
            state, key, before_pressure, before_activation,
            f"本轮得到{round(strength * 100)}%程度的满足", "satisfaction", now,
            force_history=True,
        )
        for other_key, other in DIMENSIONS.items():
            multiplier = dict(other.get("inhibited_by") or {}).get(key)
            if multiplier is not None:
                before_other = state["drives"][other_key]
                before_other_activation = state["driveActivations"][other_key]
                scaled = 1.0 - strength * (1.0 - float(multiplier))
                state["drives"][other_key] = round(_clamp(before_other * scaled), 4)
                _record_drive_change(
                    state, other_key, before_other, before_other_activation,
                    f"受到 {key} 满足后的交叉抑制", "cross_inhibition", now,
                )
    fatigue_delta = _clamp(_number(event.get("fatigueDelta"), 0.0), -0.08, 0.08)
    if abs(fatigue_delta) >= 0.001:
        state["fatigue"] = round(_clamp(state["fatigue"] + fatigue_delta, 0, 0.3), 4)
    for thought in list(event.get("flashThoughts") or [])[:4]:
        if isinstance(thought, dict):
            add_flash_thought(
                state["thoughtPool"], str(thought.get("key") or ""),
                str(thought.get("text") or ""), float(thought.get("intensity") or 0.7),
                now=now,
            )
    summary = str(event.get("summary") or "").strip()[:240]
    if summary:
        state["recentEvents"] = [*state["recentEvents"], {"at": _iso(now), "summary": summary}][-20:]
    state["revision"] += 1
    return state, {"wasSleeping": was_sleeping}


def apply_memory_heartbeat(
    value: dict[str, Any], now: datetime | None = None, umo: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    state, meta = apply_conversation_event(value, {}, now=now, umo=umo)
    state["lastHeartbeatAt"] = _iso(now)
    return state, meta


def contact_idle_allowed(state: dict[str, Any], now: datetime, min_idle_hours: float) -> bool:
    heartbeat = state.get("lastHeartbeatAt") or state.get("lastConversationAt")
    if not heartbeat:
        return False
    return (now - _parse_iso(heartbeat, now)).total_seconds() >= min_idle_hours * 3600


def apply_drive_feedback(value: dict[str, Any], feedback: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    state = normalize_state(value, current)
    for key, delta in feedback.items():
        if key in DRIVE_KEYS:
            try:
                before_pressure = state["drives"][key]
                before_activation = state["driveActivations"][key]
                state["drives"][key] = round(_clamp(state["drives"][key] + float(delta)), 4)
                _record_drive_change(
                    state, key, before_pressure, before_activation,
                    "人工调整长期压力", "manual_feedback", current,
                    force_history=True,
                )
            except (TypeError, ValueError):
                pass
    state["lastSettledAt"] = _iso(current)
    state["revision"] += 1
    return state


def top_drives(state: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": DIMENSIONS[key]["label"],
            "value": effective_drive_value(state, key),
            "pressure": round(_clamp(state["drives"].get(key, 0.0)), 4),
            "activation": round(_clamp(state.get("driveActivations", {}).get(key, 0.0)), 4),
            "kind": DIMENSIONS[key].get("kind", "need"),
        }
        for key in sorted(DRIVE_KEYS, key=lambda item: effective_drive_value(state, item), reverse=True)[:limit]
    ]


def pick_intent(state: dict[str, Any], rng: Callable[[], float] = random.random) -> dict[str, Any] | None:
    entries = sorted(
        ((key, effective_drive_value(state, key)) for key in DRIVE_KEYS),
        key=lambda item: item[1], reverse=True,
    )
    if not entries:
        return None
    maximum = float(entries[0][1])
    tied = [(key, float(value)) for key, value in entries if maximum - float(value) <= 0.12]
    obsessions = {item.get("key"): float(item.get("intensity") or 0) for item in state["thoughtPool"].get("obsessions", [])}
    weighted = [(key, value, value + obsessions.get(key, 0) * 0.15) for key, value in tied]
    roll = rng() * sum(item[2] for item in weighted)
    for key, value, weight in weighted:
        roll -= weight
        if roll <= 0:
            return {"key": key, "value": value, "label": DIMENSIONS[key]["label"]}
    key, value, _ = weighted[0]
    return {"key": key, "value": value, "label": DIMENSIONS[key]["label"]}


def record_dream(
    value: dict[str, Any],
    residue: str,
    awareness: str = "",
    dream_text: str = "",
    source: str = "deterministic",
    used_memory: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state = normalize_state(value, now)
    dream = {
        "id": uuid.uuid4().hex[:12], "createdAt": _iso(now),
        "dream": str(dream_text or "")[:4000],
        "residue": str(residue or "")[:800], "awareness": str(awareness or "")[:800],
        "source": str(source or "deterministic")[:40],
        "usedMemory": bool(used_memory),
    }
    state["recentDreams"] = [*state["recentDreams"], dream][-20:]
    day = _iso(now)[:10]
    state["dreamUsage"][day] = int(state["dreamUsage"].get(day, 0)) + 1
    state["revision"] += 1
    return state


def dream_allowed(state: dict[str, Any], now: datetime, min_interval_hours: float, max_per_day: int) -> bool:
    if state.get("consciousness") != "sleeping":
        return False
    day = _iso(now)[:10]
    if int(state.get("dreamUsage", {}).get(day, 0)) >= max_per_day:
        return False
    if not state.get("recentDreams"):
        return True
    latest = _parse_iso(state["recentDreams"][-1].get("createdAt"), now)
    return (now - latest).total_seconds() >= min_interval_hours * 3600


def breath_dream_context(
    value: dict[str, Any], now: datetime | None = None,
    max_age_hours: float = 18, max_dreams: int = 3,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - max(1.0, float(max_age_hours or 18)) * 3600
    limit = max(1, min(3, int(max_dreams or 3)))
    dreams = []
    for dream in list(value.get("recentDreams") or []):
        created = _parse_iso(dream.get("createdAt"), now)
        if created.timestamp() < cutoff or created > now:
            continue
        awareness = " ".join(str(dream.get("awareness") or "").split())[:280]
        residue = " ".join(str(dream.get("residue") or "").split())[:280]
        if awareness or residue:
            dreams.append({
                "id": dream.get("id"),
                "createdAt": dream.get("createdAt"),
                "summary": awareness or residue,
                "residue": residue,
            })
    dreams = dreams[-limit:]
    return {"version": 1, "available": bool(dreams), "dreams": dreams}


def recent_bark_history(state: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    current = list(state.get("recentBarkMessages") or [])
    if current:
        return current[-max(1, int(limit)):]
    legacy = [
        {
            "at": state.get("lastDreamBarkAt"),
            "kind": "dream",
            "message": state.get("lastDreamPushMessage"),
        },
        {
            "at": state.get("lastAutonomousBarkAt") or state.get("lastProactiveAt"),
            "kind": "autonomous_thought",
            "message": state.get("lastAutonomousMessage"),
        },
        {
            "at": state.get("lastDaytimeEmergenceAt"),
            "kind": "daytime_emergence",
            "message": state.get("lastDaytimeMessage"),
        },
    ]
    return sorted(
        [item for item in legacy if item["at"] and item["message"]],
        key=lambda item: _parse_iso(item["at"]).timestamp(),
    )[-max(1, int(limit)):]


def record_bark(
    value: dict[str, Any], message: str, kind: str = "unknown",
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state = normalize_state(value, now)
    at = _iso(now)
    day = at[:10]
    clean = str(message or "")[:900]
    state["recentBarkMessages"] = [
        *recent_bark_history(state), {"at": at, "kind": kind, "message": clean},
    ][-8:]
    state["lastBarkAt"] = at
    state["barkUsage"][day] = int(state["barkUsage"].get(day, 0)) + 1
    if kind == "dream":
        state["lastDreamBarkAt"] = at
        state["lastDreamPushMessage"] = clean
    elif kind == "autonomous_thought":
        state["lastAutonomousBarkAt"] = at
        state["lastAutonomousMessage"] = clean
        state["lastProactiveAt"] = at
        state["proactiveUsage"][day] = int(state["proactiveUsage"].get(day, 0)) + 1
        state["recentProactiveMessages"] = [
            *state["recentProactiveMessages"], {"at": at, "kind": kind, "message": clean[:300]},
        ][-8:]
    state["revision"] += 1
    return state


def record_proactive(value: dict[str, Any], message: str, now: datetime | None = None) -> dict[str, Any]:
    return record_bark(value, message, "autonomous_thought", now)


def bark_allowed(
    state: dict[str, Any], now: datetime, min_interval_hours: float,
    max_per_day: int, kind: str | None = None,
) -> bool:
    day = _iso(now)[:10]
    if int(state.get("barkUsage", {}).get(day, 0)) >= int(max_per_day):
        return False
    if kind == "dream":
        last = state.get("lastDreamBarkAt")
    elif kind == "autonomous_thought":
        last = state.get("lastAutonomousBarkAt") or state.get("lastProactiveAt")
    else:
        last = state.get("lastBarkAt")
    return not last or (now - _parse_iso(last, now)).total_seconds() >= float(min_interval_hours) * 3600


def proactive_allowed(
    state: dict[str, Any], now: datetime, min_idle_hours: float,
    cooldown_hours: float, max_per_day: int, min_drive: float,
) -> bool:
    if state.get("consciousness") != "sleeping" or not state.get("lastUmo"):
        return False
    if not contact_idle_allowed(state, now, min_idle_hours):
        return False
    if max((effective_drive_value(state, key) for key in DRIVE_KEYS), default=0) < min_drive:
        return False
    return bark_allowed(state, now, cooldown_hours, max_per_day, "autonomous_thought")


def _local_day_hour(now: datetime, time_zone: str) -> tuple[str, int]:
    try:
        local = now.astimezone(ZoneInfo(time_zone))
    except Exception:
        local = now.astimezone()
    return local.strftime("%Y-%m-%d"), local.hour


def daytime_emergence_allowed(
    state: dict[str, Any], now: datetime, time_zone: str,
    start_hour: int, end_hour: int, max_per_day: int,
) -> bool:
    day, hour = _local_day_hour(now, time_zone)
    if hour < int(start_hour) or hour >= int(end_hour):
        return False
    if int(state.get("daytimeEmergenceUsage", {}).get(day, 0)) >= int(max_per_day):
        return False
    due = state.get("nextDaytimeEmergenceAt")
    return bool(due) and now >= _parse_iso(due, now)


def schedule_daytime_emergence(
    value: dict[str, Any], now: datetime | None = None,
    min_hours: float = 2, max_hours: float = 3,
    rng: Callable[[], float] = random.random,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state = normalize_state(value, now)
    low, high = sorted((float(min_hours), float(max_hours)))
    delay = low + _clamp(rng()) * (high - low)
    state["nextDaytimeEmergenceAt"] = _iso(datetime.fromtimestamp(now.timestamp() + delay * 3600, tz=timezone.utc))
    state["revision"] += 1
    return state


def record_daytime_emergence(
    value: dict[str, Any], message: str, now: datetime | None = None,
    time_zone: str = "Asia/Shanghai",
    memory_keys: list[str] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state = normalize_state(value, now)
    at = _iso(now)
    day, _ = _local_day_hour(now, time_zone)
    clean = str(message or "")[:900]
    state["recentBarkMessages"] = [
        *recent_bark_history(state), {"at": at, "kind": "daytime_emergence", "message": clean},
    ][-8:]
    state["lastDaytimeEmergenceAt"] = at
    state["lastDaytimeMessage"] = clean
    state["daytimeEmergenceUsage"][day] = int(state["daytimeEmergenceUsage"].get(day, 0)) + 1
    keys = list(dict.fromkeys(
        str(item or "").strip()[:300]
        for item in list(memory_keys or [])[:10]
        if str(item or "").strip()
    ))
    if keys:
        state["recentDaytimeMemories"] = [
            *state.get("recentDaytimeMemories", []),
            {"at": at, "memoryKeys": keys, "message": clean[:180]},
        ][-32:]
    state["revision"] += 1
    return state


def recent_daytime_memory_keys(
    value: dict[str, Any],
    now: datetime | None = None,
    cooldown_hours: float = 72,
) -> set[str]:
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - max(1.0, float(cooldown_hours or 72)) * 3600
    keys: set[str] = set()
    for item in list(value.get("recentDaytimeMemories") or []):
        if not isinstance(item, dict):
            continue
        used_at = _parse_iso(item.get("at"), now)
        if used_at.timestamp() < cutoff or used_at > now:
            continue
        for key in list(item.get("memoryKeys") or []):
            clean = str(key or "").strip()
            if clean:
                keys.add(clean)
    return keys


def message_similarity(left: str, right: str) -> float:
    def normalize(text: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or "").lower())
    def grams(text: str) -> list[str]:
        return [text[i:i + 2] for i in range(max(1, len(text) - 1))] if text else []
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ag, bg = grams(a), grams(b)
    counts: dict[str, int] = {}
    for gram in ag:
        counts[gram] = counts.get(gram, 0) + 1
    overlap = 0
    for gram in bg:
        if counts.get(gram, 0) > 0:
            overlap += 1
            counts[gram] -= 1
    dice = 2 * overlap / max(1, len(ag) + len(bg))
    containment = overlap / max(1, min(len(ag), len(bg)))
    return round(max(dice, containment * 0.9), 4)
