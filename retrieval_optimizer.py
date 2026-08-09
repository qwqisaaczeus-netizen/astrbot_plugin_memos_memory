"""Effect-oriented recall optimization for 4.6.0.

The optimizer is deliberately provider-free. It receives the fused candidate
pool after vector/BM25/source retrieval and adds explainable ranking signals:

- independent cross-layer support (diary passage / Episode / source turn),
- temporal range and ordinal constraints,
- intent-specific representation weights.

Missing layers never receive a negative score. This keeps old diary-derived
memories competitive while allowing newer source-grounded memories to earn a
small confidence bonus when independent representations agree.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .store_utils import extract_terms


CURRENT_STATE_CUES = (
    "现在是什么关系", "如今是什么关系", "现在怎么看", "现在怎么想", "现在还",
    "如今还", "目前", "现在的关系", "我们的关系", "对我的态度", "对他的态度",
)
EVENT_CUES = (
    "那天", "那次", "发生", "说过", "做过", "答应", "约定", "为什么会", "怎么回事",
)
EMOTION_CUES = (
    "感受", "心情", "难过", "害怕", "安心", "信任", "依赖", "喜欢", "爱", "想念",
    "委屈", "生气", "嫉妒", "后悔", "不安", "情绪", "心里",
)
NARRATIVE_CUES = ("讲讲", "回忆", "一路", "后来都", "经历", "故事", "从头", "详细说")


LAYER_PROFILES: dict[str, dict[str, float]] = {
    "specific_event": {"passage": 1.00, "episode": 1.03, "source": 1.08, "state": 0.82},
    "temporal_event": {"passage": 0.96, "episode": 1.10, "source": 1.06, "state": 0.78},
    "current_state": {"passage": 0.88, "episode": 1.00, "source": 0.84, "state": 1.12},
    "emotional_continuity": {"passage": 1.00, "episode": 1.06, "source": 0.98, "state": 1.08},
    "narrative": {"passage": 1.04, "episode": 1.02, "source": 0.98, "state": 0.94},
    "contextual": {"passage": 1.00, "episode": 1.02, "source": 1.02, "state": 0.96},
}


@dataclass
class TemporalConstraint:
    ranges: list[tuple[date, date, str]] = field(default_factory=list)
    ordinal: str = ""  # first | latest | previous
    markers: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.ranges or self.ordinal)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "ordinal": self.ordinal,
            "markers": list(self.markers),
            "ranges": [
                {"start": start.isoformat(), "end": end.isoformat(), "reason": reason}
                for start, end, reason in self.ranges
            ],
        }


def _month_range(year: int, month: int) -> tuple[date, date]:
    month = max(1, min(12, month))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _season_range(year: int, season: str) -> tuple[date, date]:
    if season == "春天":
        return date(year, 3, 1), date(year, 5, 31)
    if season == "夏天":
        return date(year, 6, 1), date(year, 8, 31)
    if season == "秋天":
        return date(year, 9, 1), date(year, 11, 30)
    # Winter spans the year boundary.
    return date(year, 12, 1), date(year + 1, 2, calendar.monthrange(year + 1, 2)[1])


def parse_temporal_constraint(query: str, reference_now: datetime | None = None) -> TemporalConstraint:
    text = " ".join(str(query or "").split())
    now = reference_now or datetime.now()
    result = TemporalConstraint()

    def add(start: date, end: date, reason: str) -> None:
        item = (start, end, reason)
        if item not in result.ranges:
            result.ranges.append(item)
            result.markers.append(reason)

    for year, month, day in re.findall(
        r"((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?", text
    ):
        try:
            exact = date(int(year), int(month), int(day))
            add(exact, exact, "explicit_day")
        except ValueError:
            pass
    if not result.ranges:
        for year, month in re.findall(r"((?:19|20)\d{2})[-/.年](\d{1,2})(?:月)?", text):
            add(*_month_range(int(year), int(month)), "explicit_month")
    for marker, offset in (("今天", 0), ("昨日", -1), ("昨天", -1), ("前天", -2), ("明天", 1)):
        if marker in text:
            target = (now + timedelta(days=offset)).date()
            add(target, target, marker)
    if "上个月" in text:
        year, month = now.year, now.month - 1
        if month == 0:
            year, month = year - 1, 12
        add(*_month_range(year, month), "上个月")
    elif "这个月" in text or "本月" in text:
        add(*_month_range(now.year, now.month), "本月")

    year_aliases = {"今年": now.year, "去年": now.year - 1, "前年": now.year - 2}
    for marker, year in year_aliases.items():
        if marker not in text:
            continue
        season = next((name for name in ("春天", "夏天", "秋天", "冬天") if name in text), "")
        if season:
            add(*_season_range(year, season), marker + season)
        else:
            month_match = re.search(re.escape(marker) + r"\s*(\d{1,2})月", text)
            if month_match:
                add(*_month_range(year, int(month_match.group(1))), marker + "月份")
            elif not result.ranges:
                add(date(year, 1, 1), date(year, 12, 31), marker)

    if any(marker in text for marker in ("第一次", "最早", "起初", "最开始")):
        result.ordinal = "first"
        result.markers.append("first")
    elif any(marker in text for marker in ("最后一次", "最近一次", "最新一次")):
        result.ordinal = "latest"
        result.markers.append("latest")
    elif any(marker in text for marker in ("上一次", "上次", "之前那次")):
        result.ordinal = "previous"
        result.markers.append("previous")
    return result


def classify_memory_intent(query: str, planner_intent: str = "") -> str:
    text = str(query or "")
    if planner_intent == "temporal" or parse_temporal_constraint(text).active:
        return "temporal_event"
    if any(cue in text for cue in CURRENT_STATE_CUES):
        return "current_state"
    if any(cue in text for cue in NARRATIVE_CUES) or planner_intent == "narrative":
        return "narrative"
    if any(cue in text for cue in EMOTION_CUES):
        return "emotional_continuity"
    if any(cue in text for cue in EVENT_CUES):
        return "specific_event"
    return "contextual" if planner_intent == "contextual" else "specific_event"


def intent_target(intent: str, normal: int, maximum: int, minimum: int) -> int:
    preferred = {
        "current_state": max(minimum, min(4, normal)),
        "specific_event": normal,
        "emotional_continuity": min(maximum, normal + 1),
        "temporal_event": min(maximum, normal + 1),
        "narrative": maximum,
        "contextual": normal,
    }.get(intent, normal)
    return max(minimum, min(maximum, preferred))


def _candidate_date(hit: dict[str, Any]) -> date | None:
    timestamp = float(hit.get("event_ts") or 0)
    if timestamp > 0:
        try:
            return datetime.fromtimestamp(timestamp).date()
        except (OSError, OverflowError, ValueError):
            pass
    text = str(hit.get("occurred_at") or hit.get("ts_text") or "")
    match = re.search(r"((?:19|20)\d{2})\D+(\d{1,2})\D+(\d{1,2})", text)
    if match:
        try:
            return date(*(int(value) for value in match.groups()))
        except ValueError:
            return None
    return None


def _route_strength(hit: dict[str, Any], route_names: set[str]) -> float:
    values = []
    for route in hit.get("_route_evidence") or []:
        if not isinstance(route, dict) or str(route.get("route") or "") not in route_names:
            continue
        values.append(float(route.get("relevance") or route.get("semantic") or 0.0))
    return max(values, default=0.0)


def _text_support(query_terms: set[str], text: str) -> float:
    terms = set(extract_terms(str(text or "")))
    if not query_terms or not terms:
        return 0.0
    return len(query_terms & terms) / max(1, min(len(query_terms), 6))


def optimize_fused_hits(
    query: str,
    hits: list[dict[str, Any]],
    plan: dict[str, Any],
    *,
    reference_now: datetime | None = None,
    cross_layer: bool = True,
    temporal: bool = True,
    intent_weights: bool = True,
) -> dict[str, Any]:
    """Annotate a fused pool and return diagnostics; no candidate is removed."""
    planner_intent = str(plan.get("intent") or "")
    memory_intent = classify_memory_intent(query, planner_intent)
    profile = dict(LAYER_PROFILES.get(memory_intent, LAYER_PROFILES["specific_event"]))
    constraint = parse_temporal_constraint(query, reference_now) if temporal else TemporalConstraint()
    query_terms = set(extract_terms(query))
    diagnostics: dict[str, Any] = {
        "enabled": bool(cross_layer or temporal or intent_weights),
        "memory_intent": memory_intent,
        "layer_weights": profile,
        "temporal_constraint": constraint.as_dict(),
        "cross_layer_candidates": 0,
        "temporal_matches": 0,
    }
    dated: list[tuple[date, dict[str, Any]]] = []
    range_matches: list[dict[str, Any]] = []
    for hit in hits:
        route_names = {
            str(item.get("route") or "")
            for item in (hit.get("_route_evidence") or [])
            if isinstance(item, dict)
        }
        passage_available = bool(hit.get("_passage_hit") or route_names & {"passage_hybrid", "keyword"})
        episode_available = bool(hit.get("_event_card_hit") or route_names & {"event_card"})
        source_available = bool(hit.get("_source_evidence_hit") or route_names & {"source_turn"})
        passage_signal = max(
            _route_strength(hit, {"passage_hybrid", "keyword"}),
            _text_support(query_terms, str(hit.get("chunk_text") or "")),
        ) if passage_available else 0.0
        episode_signal = max(
            _route_strength(hit, {"event_card"}),
            _text_support(query_terms, " ".join(str(hit.get(key) or "") for key in (
                "scene_anchor", "retrieval_key", "state_change", "trigger_hint",
            ))),
        ) if episode_available else 0.0
        source_signal = max(
            _route_strength(hit, {"source_turn"}),
            max((
                max(float(item.get("relevance") or 0.0),
                    _text_support(query_terms, str(item.get("content") or "")))
                for item in (hit.get("_source_turn_hits") or []) if isinstance(item, dict)
            ), default=0.0),
        ) if source_available else 0.0
        signals = {
            "passage": passage_signal,
            "episode": episode_signal,
            "source": source_signal,
        }
        available = [name for name, present in (
            ("passage", passage_available), ("episode", episode_available), ("source", source_available)
        ) if present]
        supported = [name for name in available if signals[name] >= 0.20]
        bonus = 0.0
        reasons: list[str] = []
        if cross_layer and len(supported) >= 2:
            bonus += 0.035 + min(0.025, (len(supported) - 2) * 0.025)
            reasons.append("cross_layer_consensus:" + "+".join(supported))
            diagnostics["cross_layer_candidates"] += 1
        if cross_layer and source_available and source_signal >= 0.32 and any(
            bool(item.get("exact_link")) for item in (hit.get("_source_turn_hits") or [])
            if isinstance(item, dict)
        ):
            bonus += 0.025
            reasons.append("exact_source_support")
        if intent_weights:
            weighted = sum(signals[name] * profile[name] for name in signals)
            baseline = sum(signals.values())
            layer_adjustment = max(-0.025, min(0.035, (weighted - baseline) * 0.08))
            if memory_intent == "current_state" and (
                str(hit.get("state_change") or "").strip() or str(hit.get("long_effect") or "").strip()
            ):
                layer_adjustment += 0.035
                reasons.append("current_state_evidence")
            if abs(layer_adjustment) >= 0.001:
                bonus += layer_adjustment
                reasons.append("intent_layer_weight")
        candidate_day = _candidate_date(hit)
        if candidate_day:
            dated.append((candidate_day, hit))
        if constraint.ranges and candidate_day and any(start <= candidate_day <= end for start, end, _ in constraint.ranges):
            bonus += 0.12
            hit["_temporal_constraint_match"] = True
            hit["_temporal_rescue"] = True
            range_matches.append(hit)
            diagnostics["temporal_matches"] += 1
            reasons.append("temporal_range_match")
        hit["_cross_layer_available"] = available
        hit["_cross_layer_supported"] = supported
        hit["_cross_layer_consistency"] = round(len(supported) / max(1, len(available)), 4)
        hit["_layer_signals"] = {key: round(value, 4) for key, value in signals.items()}
        hit["_retrieval_bonus"] = round(max(-0.03, min(0.18, bonus)), 4)
        hit["_retrieval_bonus_reasons"] = reasons

    if constraint.ordinal and dated:
        ordered = sorted(dated, key=lambda item: item[0])
        target: dict[str, Any] | None = None
        if constraint.ordinal == "first":
            target = ordered[0][1]
        elif constraint.ordinal == "latest":
            target = ordered[-1][1]
        elif constraint.ordinal == "previous":
            target = ordered[-2][1] if len(ordered) >= 2 else ordered[-1][1]
        if target is not None:
            target["_retrieval_bonus"] = round(min(0.20, float(target.get("_retrieval_bonus") or 0.0) + 0.11), 4)
            target["_temporal_constraint_match"] = True
            target["_temporal_rescue"] = True
            target.setdefault("_retrieval_bonus_reasons", []).append("temporal_ordinal:" + constraint.ordinal)
            diagnostics["temporal_matches"] += 1
    plan["memory_intent"] = memory_intent
    plan["layer_weights"] = profile
    plan["temporal_constraint"] = constraint.as_dict()
    diagnostics["range_fallback"] = bool(constraint.ranges and not range_matches)
    return diagnostics
