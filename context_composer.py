"""Bounded, auditable context composition for the 6.0 Canary path.

The composer is deliberately pure: it never reads the database, calls a model,
or mutates an AstrBot request.  Retrieval stays in ``thread_retrieval`` and the
integration layer decides whether the validated result may be injected.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from .temporal import timezone_or_default
from typing import Any, Iterable


_INTERNAL_ID = re.compile(r"\b(?:ep|clm|thr|pro|thread_lab)_[A-Za-z0-9_-]+\b", re.I)
_SPACE = re.compile(r"\s+")
_RELATION_LABELS = {
    "continues": "延续为",
    "causes": "促成了",
    "responds_to": "回应了",
    "confirms": "再次确认",
    "supersedes": "后来被更新为",
    "resolves": "后来得到解决",
    "breaks": "后来被打破",
    "contradicts_stage": "在不同阶段呈现变化",
    "retells": "是同一经历的复述",
    "parallel": "属于同一长期主题下的不同经历",
}


def _clean(value: Any, limit: int = 240) -> str:
    text = _SPACE.sub(" ", str(value or "")).strip()
    text = _INTERNAL_ID.sub("", text)
    return text[: max(0, int(limit))].strip(" -|；;")


def _list_values(value: Any) -> list[str]:
    if isinstance(value, str):
        raw: Iterable[Any] = re.split(r"[,，;；\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = []
    return list(dict.fromkeys(_clean(item, 300) for item in raw if _clean(item, 300)))


def stable_canary_decision(
    *,
    scope_id: str,
    session_id: str,
    seed: str,
    percent: int,
    mode: str,
    allowlist: Any = None,
    denylist: Any = None,
) -> dict[str, Any]:
    """Return a restart-stable Canary decision with explicit overrides."""
    scope = _clean(scope_id, 500) or "default"
    session = _clean(session_id, 500) or scope
    keys = {session, scope, f"{scope}:{session}"}
    denied = set(_list_values(denylist))
    allowed = set(_list_values(allowlist))
    digest = hashlib.sha256(f"{scope}\0{session}\0{seed}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") % 100
    normalized_mode = str(mode or "shadow").strip().lower()
    bounded_percent = max(0, min(100, int(percent or 0)))
    if keys & denied:
        selected, reason = False, "denylist"
    elif keys & allowed:
        selected, reason = normalized_mode == "canary", "allowlist"
    elif normalized_mode != "canary":
        selected, reason = False, "shadow_mode"
    else:
        selected, reason = bucket < bounded_percent, "stable_bucket"
    return {
        "selected": bool(selected),
        "reason": reason,
        "bucket": bucket,
        "percent": bounded_percent,
        "mode": normalized_mode,
        "scope_id": scope,
        "session_id": session,
    }


@dataclass(frozen=True)
class ComposeResult:
    text: str
    metrics: dict[str, Any]
    evidence: tuple[dict[str, Any], ...] = ()


class ThreadContextComposer:
    """Compose the smallest useful thread block without repeating diary text."""

    def __init__(self, *, max_chars: int = 1200, growth_percent: float = 10.0,
                 timezone_name: str = "Asia/Shanghai"):
        self.max_chars = max(320, min(6000, int(max_chars or 1200)))
        self.growth_percent = max(1.0, min(30.0, float(growth_percent or 10.0)))
        self.timezone_name = str(timezone_name or "Asia/Shanghai")

    def _event_time(self, node: dict[str, Any]) -> str:
        occurred = _clean(node.get("occurred_at"), 40)
        if occurred:
            return occurred
        event_ts = float(node.get("event_ts") or 0)
        if event_ts > 0:
            return datetime.fromtimestamp(
                event_ts, timezone_or_default(self.timezone_name)
            ).strftime("%Y-%m-%d %H:%M")
        return "事件时间未确认"

    @staticmethod
    def _event_body(node: dict[str, Any], limit: int = 170) -> str:
        for key in ("state_change", "retrieval_key", "scene_anchor", "card_text"):
            text = _clean(node.get(key), limit)
            if text:
                return text
        return ""

    def _prospective_time(self, item: dict[str, Any]) -> str:
        start = float(item.get("due_start") or 0)
        end = float(item.get("due_end") or 0)
        tz = timezone_or_default(self.timezone_name)
        if start > 0 and end > start:
            return (
                datetime.fromtimestamp(start, tz).strftime("%Y-%m-%d %H:%M")
                + " 至 "
                + datetime.fromtimestamp(end, tz).strftime("%Y-%m-%d %H:%M")
            )
        if start > 0:
            return datetime.fromtimestamp(start, tz).strftime("%Y-%m-%d %H:%M")
        return "未来时间未确认"

    @staticmethod
    def _claim_conflict(claims: list[dict[str, Any]]) -> bool:
        slots: dict[str, set[str]] = {}
        for claim in claims:
            if claim.get("counterevidence") or str(claim.get("status") or "") != "active":
                continue
            slot = _clean(claim.get("slot_key") or claim.get("claim_type"), 160)
            value = _clean(claim.get("object"), 300)
            if slot and value:
                slots.setdefault(slot, set()).add(value)
        return any(len(values) > 1 for values in slots.values())

    @staticmethod
    def _plan_relevant(plan: dict[str, Any], result: dict[str, Any]) -> bool:
        routes = result.get("routes") or {}
        transition = routes.get("transitions") or {}
        prospective = (routes.get("prospective") or {}).get("selected")
        return bool(
            plan.get("thread_intent")
            or plan.get("current_state_intent")
            or plan.get("evolution_intent")
            or plan.get("prospective_intent")
            or routes.get("current_claims")
            or transition.get("transitions")
            or prospective
        )

    def compose(
        self,
        result: dict[str, Any],
        *,
        plan: dict[str, Any],
        base_memory_chars: int,
        preexisting_chars: int,
    ) -> ComposeResult:
        routes = result.get("routes") or {}
        claims = list(routes.get("current_claims") or [])
        if self._claim_conflict(claims):
            return ComposeResult("", {"outcome": "fail_open", "reason": "claim_conflict"})
        if not self._plan_relevant(plan, result):
            return ComposeResult("", {"outcome": "skipped", "reason": "not_thread_relevant"})

        dedup = result.get("dedup") or {}
        nodes = [dict(item) for item in (dedup.get("new_nodes") or [])]
        nodes.sort(key=lambda item: (
            float(item.get("event_ts") or 0) if float(item.get("event_ts") or 0) > 0 else float("inf"),
            str(item.get("episode_id") or ""),
        ))
        nodes = nodes[:5]
        transition = routes.get("transitions") or {}
        transitions = list(transition.get("transitions") or [])[:2]
        prospective = (routes.get("prospective") or {}).get("selected") or {}

        current = next((item for item in claims if not item.get("counterevidence") and str(item.get("status") or "") == "active"), None)
        counter = next((item for item in claims if item.get("counterevidence")), None)
        if not any((current, counter, nodes, transitions, prospective)):
            return ComposeResult("", {"outcome": "skipped", "reason": "empty_context"})
        if plan.get("evolution_intent") and len(nodes) < 2 and not transitions:
            return ComposeResult("", {"outcome": "skipped", "reason": "insufficient_evolution_evidence"})

        baseline = max(0, int(base_memory_chars or 0)) + max(0, int(preexisting_chars or 0))
        prospective_only = bool(
            prospective
            and not any((current, counter, nodes, transitions))
        )
        growth_floor = 320 if prospective_only else 240
        growth_limit = max(
            growth_floor,
            int(baseline * self.growth_percent / 100.0),
        )
        allowed = min(self.max_chars, growth_limit)
        protected: list[str] = []
        trimmed: list[str] = []

        def render(active_nodes: list[dict[str, Any]], body_limit: int) -> tuple[str, int]:
            lines = [
                '<MemoryContinuityContext temporal_role="historical_context">',
                "以下是与本轮有关的历史脉络，不是刚刚发生；当前现实时间只以 CurrentTimeContext 为准。",
            ]
            payload_chars = 0
            if current:
                value = _clean(current.get("object"), 260)
                if value:
                    lines.extend(("[当前仍有效的事实]", f"- {value}"))
                    payload_chars += len(value)
            if counter:
                value = _clean(counter.get("object"), 220)
                if value:
                    lines.extend(("[需要保留的反证]", f"- 旧阶段曾是：{value}；不要用它覆盖当前有效事实。"))
                    payload_chars += len(value)
            if active_nodes:
                lines.append("[相关经历脉络，按事件发生时间] ")
                for node in active_nodes:
                    body = self._event_body(node, body_limit)
                    if body:
                        memory_type = _clean(node.get("memory_type"), 24) or "经历"
                        line = f"- {self._event_time(node)} | {memory_type} | {body}"
                        lines.append(line)
                        payload_chars += len(body)
            if transitions:
                lines.append("[状态变化]")
                for item in transitions:
                    before = _clean(item.get("from_object"), 110)
                    after = _clean(item.get("to_object"), 110)
                    relation = _RELATION_LABELS.get(str(item.get("transition_type") or ""), "后来变化为")
                    if before and after:
                        lines.append(f"- {before}，{relation}：{after}。")
                        payload_chars += len(before) + len(after)
            if prospective:
                desc = _clean(prospective.get("description"), 220)
                if desc:
                    lines.extend(("[尚待发生或解决的事项]", f"- 未来时间：{self._prospective_time(prospective)} | {desc}"))
                    payload_chars += len(desc)
            lines.extend((
                "[回答边界] 历史日期、当前时间、身体节律时段和未来时间窗互不替代；来源没有给出日期时保持未知。",
                "</MemoryContinuityContext>",
            ))
            return "\n".join(lines), payload_chars

        if current:
            protected.append("current_fact")
        if counter:
            protected.append("counterevidence")
        if transitions:
            protected.append("state_transition")
        if prospective:
            protected.append("prospective")

        text, payload_chars = render(nodes, 170)
        if len(text) > allowed:
            text, payload_chars = render(nodes, 110)
            trimmed.append("node_bodies_compacted")
        while len(text) > allowed and len(nodes) > 2:
            nodes.pop()
            trimmed.append("supporting_node")
            text, payload_chars = render(nodes, 110)
        if len(text) > allowed:
            return ComposeResult("", {
                "outcome": "fail_open", "reason": "protected_context_exceeds_budget",
                "allowed_chars": allowed, "protected": protected, "trimmed": trimmed,
            })
        if _INTERNAL_ID.search(text):
            return ComposeResult("", {"outcome": "fail_open", "reason": "internal_id_leak"})

        evidence: list[dict[str, Any]] = []
        if current:
            value = _clean(current.get("object"), 260)
            if value:
                evidence.append({
                    **dict(current), "text": value, "category": "claim",
                    "source": "current_claim", "temporal_role": "current",
                })
        if counter:
            value = _clean(counter.get("object"), 220)
            if value:
                evidence.append({
                    **dict(counter), "text": value, "category": "claim",
                    "source": "claim_counterevidence", "temporal_role": "historical",
                })
        for node in nodes:
            body = self._event_body(node, 110 if "node_bodies_compacted" in trimmed else 170)
            if body:
                evidence.append({
                    **dict(node), "text": body, "category": "episode",
                    "source": "episode", "temporal_role": "historical",
                })
        for item in transitions:
            before = _clean(item.get("from_object"), 110)
            after = _clean(item.get("to_object"), 110)
            if before and after:
                relation = _RELATION_LABELS.get(
                    str(item.get("transition_type") or ""), "后来变化为"
                )
                evidence.append({
                    **dict(item),
                    "text": f"{before}，{relation}：{after}。",
                    "category": "transition",
                    "source": "claim_transition",
                    "temporal_role": "historical",
                })
        if prospective:
            desc = _clean(prospective.get("description"), 220)
            if desc:
                evidence.append({
                    **dict(prospective), "text": desc, "category": "prospective",
                    "source": "prospective_memory", "temporal_role": "future",
                })

        duplicate_count = int(dedup.get("duplicate_count") or 0)
        metrics = {
            "outcome": "composed",
            "reason": "relevant_context",
            "chars": len(text),
            "estimated_tokens": max(1, (len(text) + 1) // 2),
            "allowed_chars": allowed,
            "baseline_chars": baseline,
            "growth_ratio": round(len(text) / max(1, baseline), 6),
            "fact_density": round(payload_chars / max(1, len(text)), 6),
            "current_fact_count": int(bool(current)),
            "counterevidence_count": int(bool(counter)),
            "node_count": len(nodes),
            "transition_count": len(transitions),
            "prospective_count": int(bool(prospective)),
            "cross_layer_duplicates_removed": duplicate_count,
            "protected": protected,
            "trimmed": trimmed,
        }
        return ComposeResult(text, metrics, tuple(evidence))
