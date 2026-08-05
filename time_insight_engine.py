from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


PERSONA_TYPES = {
    "relationship_shift",
    "emotional_anchor",
    "behavior_bias",
    "promise_or_rule",
}

TYPE_LABELS = {
    "relationship_shift": "关系变化",
    "emotional_anchor": "情绪锚点",
    "behavior_bias": "行为倾向",
    "promise_or_rule": "承诺或规则",
    "plot_fact": "经历",
    "daily_texture": "日常片段",
}

POSITIVE_MARKERS = {
    "靠近", "和好", "安心", "信任", "告白", "答应", "承诺", "温柔", "拥抱", "理解", "接受",
}
NEGATIVE_MARKERS = {
    "分开", "离开", "冲突", "拒绝", "失望", "伤心", "不安", "争吵", "决裂", "失去", "害怕",
}
TEMPORAL_QUERY_MARKERS = {
    "以前", "过去", "那天", "当时", "去年", "前年", "往年", "今天", "纪念日", "周年",
    "最近", "这段时间", "一直", "每年", "还记得", "记不记得", "什么时候",
}


@dataclass(frozen=True)
class EngineConfig:
    timezone: str = "Asia/Shanghai"
    recent_window_days: int = 21
    anniversary_window_days: int = 2
    min_importance: int = 3
    min_evidence_score: float = 0.68
    exact_anniversary_limit: int = 3
    nearby_anniversary_limit: int = 2
    trend_min_distinct_days: int = 3
    trend_min_evidence: int = 3
    seasonal_min_years: int = 3
    static_max_insights: int = 2
    injection_max_chars: int = 800
    query_max_insights: int = 2
    query_min_score: float = 0.42
    query_injection_max_chars: int = 900


def clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def clean_text(value: Any, limit: int = 280) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_jsonish_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            raw = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            raw = re.split(r"[,，、|;；\n]+", text)
    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name") or item.get("text") or item.get("value") or ""
        normalized = clean_text(item, 60)
        if normalized and normalized not in result:
            result.append(normalized)
    return result[:24]


def _date_from_text(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def resolve_event_date(memory: dict[str, Any], timezone_name: str) -> tuple[date | None, float, str]:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    basis = str(memory.get("time_basis") or "unknown").strip()
    confidence_by_basis = {
        "explicit": 1.0,
        "conversation_now": 0.96,
        "explicit_dialogue": 0.94,
        "inferred_from_source": 0.84,
        "source_time_fallback": 0.66,
        "inferred_from_current": 0.55,
        "unknown": 0.35,
    }
    confidence = confidence_by_basis.get(basis, 0.72)
    for key in ("occurred_at", "ts_text"):
        parsed = _date_from_text(memory.get(key))
        if parsed:
            return parsed, confidence, basis
    try:
        event_ts = float(memory.get("event_ts") or 0)
    except (TypeError, ValueError):
        event_ts = 0.0
    if event_ts > 0:
        return datetime.fromtimestamp(event_ts, tz).date(), confidence, basis
    try:
        source_ts = float(memory.get("source_created_ts") or memory.get("created_ts") or 0)
    except (TypeError, ValueError):
        source_ts = 0.0
    if source_ts > 0:
        return datetime.fromtimestamp(source_ts, tz).date(), min(confidence, 0.52), "source_only"
    return None, 0.0, "unknown"


def terms(value: Any) -> set[str]:
    text = clean_text(value, 2000).lower()
    words = {
        word for word in re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text)
        if len(word) <= 24
    }
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    words.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    return {word for word in words if word}


def _fingerprint(value: Any) -> str:
    normalized = re.sub(r"\W+", "", clean_text(value, 600).lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def _text_similarity(left: Any, right: Any) -> float:
    a = terms(left)
    b = terms(right)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _polarity(texts: list[str]) -> str:
    joined = " ".join(texts)
    positive = sum(1 for marker in POSITIVE_MARKERS if marker in joined)
    negative = sum(1 for marker in NEGATIVE_MARKERS if marker in joined)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "neutral"


def _memory_text(memory: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("state_change", "long_effect", "trigger_hint", "scene_anchor", "chunk_text"):
        item = clean_text(memory.get(key), 360)
        if item and all(_text_similarity(item, existing) < 0.78 for existing in pieces):
            pieces.append(item)
    return clean_text("；".join(pieces), 520)


def prepare_memories(
    memories: list[dict[str, Any]],
    now: datetime,
    cfg: EngineConfig,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    today = now.date()
    for raw in memories:
        memo_name = str(raw.get("memo_name") or "").strip()
        if not memo_name:
            continue
        event_date, date_confidence, basis = resolve_event_date(raw, cfg.timezone)
        if event_date is None or event_date > today + timedelta(days=1):
            continue
        actions = set(parse_jsonish_list(raw.get("feedback_actions")))
        if actions & {"incorrect", "outdated"}:
            continue
        text = _memory_text(raw)
        if not text:
            continue
        importance = int(clamp(raw.get("importance"), 1, 5, 3))
        manual = bool(int(raw.get("manual") or 0))
        feedback = clamp(raw.get("feedback_effect"), -0.5, 0.5, 0.0)
        if importance < cfg.min_importance and not manual and feedback <= 0:
            continue
        quality = (
            date_confidence * 0.42
            + ((importance - 1) / 4) * 0.26
            + min(1.0, len(text) / 220) * 0.14
            + (0.08 if manual else 0.0)
            + max(-0.12, min(0.10, feedback * 0.25))
            + (0.04 if str(raw.get("memory_type") or "") in PERSONA_TYPES else 0.0)
            - (0.10 if "too_frequent" in actions else 0.0)
        )
        prepared.append({
            **raw,
            "memo_name": memo_name,
            "event_date": event_date,
            "date": event_date.isoformat(),
            "date_confidence": round(date_confidence, 4),
            "time_basis": basis,
            "importance": importance,
            "manual": manual,
            "feedback_effect": feedback,
            "feedback_actions": sorted(actions),
            "memory_type": str(raw.get("memory_type") or "plot_fact"),
            "entities_list": parse_jsonish_list(raw.get("entities")),
            "tags_list": parse_jsonish_list(raw.get("tags")),
            "text": text,
            "quality": round(max(0.0, min(1.0, quality)), 4),
        })
    prepared.sort(key=lambda item: (item["event_date"], item["importance"]), reverse=True)
    return prepared


def _evidence(memory: dict[str, Any], reason: str, score: float) -> dict[str, Any]:
    return {
        "id": "ev-" + hashlib.sha1(
            f"{memory['memo_name']}|{memory['date']}".encode("utf-8")
        ).hexdigest()[:12],
        "memo_name": memory["memo_name"],
        "date": memory["date"],
        "title": clean_text(memory.get("ts_text") or memory["date"], 100),
        "type": memory["memory_type"],
        "reason": reason,
        "text": clean_text(memory["text"], 300),
        "importance": memory["importance"],
        "time_basis": memory["time_basis"],
        "date_confidence": memory["date_confidence"],
        "score": round(score, 4),
        "entities": memory["entities_list"],
    }


def _candidate(
    kind: str,
    title: str,
    claim: str,
    score: float,
    evidence: list[dict[str, Any]],
    *,
    mixed: bool = False,
    static_eligible: bool = True,
) -> dict[str, Any]:
    identity = kind + "|" + "|".join(item["memo_name"] for item in evidence)
    candidate_id = kind + "-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    material = " ".join([title, claim, *[item["text"] for item in evidence]])
    return {
        "candidate_id": candidate_id,
        "kind": kind,
        "title": clean_text(title, 100),
        "claim": clean_text(claim, 260),
        "score": round(max(0.0, min(1.0, score)), 4),
        "confidence": round(max(0.0, min(1.0, min(
            [item.get("date_confidence", 0.0) for item in evidence] or [0.0]
        ) * 0.55 + score * 0.45)), 4),
        "mixed": mixed,
        "static_eligible": static_eligible,
        "evidence": evidence,
        "evidence_ids": [item["id"] for item in evidence],
        "terms": sorted(terms(material))[:240],
        "fingerprint": _fingerprint(material),
        "synthesis": "deterministic",
    }


def _anniversary_date(today: date, event_date: date) -> date | None:
    try:
        return date(today.year, event_date.month, event_date.day)
    except ValueError:
        if event_date.month == 2 and event_date.day == 29:
            return date(today.year, 2, 28)
        return None


def _anniversary_candidates(
    memories: list[dict[str, Any]], today: date, cfg: EngineConfig,
) -> list[dict[str, Any]]:
    exact: list[dict[str, Any]] = []
    nearby: list[dict[str, Any]] = []
    for memory in memories:
        event_date = memory["event_date"]
        years = today.year - event_date.year
        if years < 1:
            continue
        anniversary = _anniversary_date(today, event_date)
        if anniversary is None:
            continue
        distance = (anniversary - today).days
        if distance == 0:
            score = 0.54 + memory["quality"] * 0.46
            if memory["date_confidence"] < 0.78 or score < cfg.min_evidence_score:
                continue
            evidence = [_evidence(memory, f"{years} 年前的同月同日", score)]
            claim = (
                f"{years} 年前的今天曾发生过“{clean_text(memory['text'], 130)}”。"
                "日期可能唤起余韵，但不表示旧事正在重演。"
            )
            exact.append(_candidate("anniversary_exact", "同日周年回声", claim, score, evidence))
        elif abs(distance) <= cfg.anniversary_window_days:
            if not (
                memory["importance"] >= 4 or memory["manual"]
                or memory["memory_type"] in PERSONA_TYPES
            ):
                continue
            relation = 1.0 - abs(distance) / (cfg.anniversary_window_days + 1)
            score = relation * 0.42 + memory["quality"] * 0.58
            if memory["date_confidence"] < 0.82 or score < cfg.min_evidence_score + 0.04:
                continue
            direction = f"还有 {distance} 天" if distance > 0 else f"已经过去 {abs(distance)} 天"
            evidence = [_evidence(memory, f"周年日期临近，{direction}", score)]
            claim = (
                f"{event_date.isoformat()} 的周年节点{direction}，相关旧事是“{clean_text(memory['text'], 120)}”。"
                "只能作为临近日历节点的潜在联想。"
            )
            nearby.append(_candidate("anniversary_nearby", "临近周年节点", claim, score, evidence))
    exact.sort(key=lambda item: item["score"], reverse=True)
    nearby.sort(key=lambda item: item["score"], reverse=True)
    return exact[:cfg.exact_anniversary_limit] + nearby[:cfg.nearby_anniversary_limit]


def _trend_candidates(
    memories: list[dict[str, Any]], today: date, cfg: EngineConfig,
) -> list[dict[str, Any]]:
    recent_start = today - timedelta(days=cfg.recent_window_days)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for memory in memories:
        if not (recent_start <= memory["event_date"] <= today):
            continue
        if memory["memory_type"] not in PERSONA_TYPES:
            continue
        entity = memory["entities_list"][0] if memory["entities_list"] else ""
        groups.setdefault((memory["memory_type"], entity), []).append(memory)
    candidates: list[dict[str, Any]] = []
    for (memory_type, entity), members in groups.items():
        unique_by_memo = {item["memo_name"]: item for item in members}
        members = sorted(unique_by_memo.values(), key=lambda item: item["quality"], reverse=True)
        distinct_days = sorted({item["date"] for item in members})
        if len(members) < cfg.trend_min_evidence or len(distinct_days) < cfg.trend_min_distinct_days:
            continue
        selected: list[dict[str, Any]] = []
        seen_dates: set[str] = set()
        for item in members:
            if item["date"] in seen_dates and len(seen_dates) < cfg.trend_min_distinct_days:
                continue
            if all(_text_similarity(item["text"], old["text"]) < 0.86 for old in selected):
                selected.append(item)
                seen_dates.add(item["date"])
            if len(selected) >= 5:
                break
        if len({item["date"] for item in selected}) < cfg.trend_min_distinct_days:
            continue
        average = sum(item["quality"] for item in selected) / len(selected)
        coverage = min(1.0, len(distinct_days) / max(3, cfg.recent_window_days / 4))
        score = (
            average * 0.72
            + coverage * 0.18
            + min(0.10, math.log2(len(members) + 1) * 0.025)
            + 0.06
        )
        if score < cfg.min_evidence_score:
            continue
        polarity = _polarity([item["text"] for item in selected])
        label = TYPE_LABELS.get(memory_type, memory_type)
        subject = f"“{entity}”相关的" if entity else ""
        if polarity == "mixed":
            claim = (
                f"近 {cfg.recent_window_days} 天，{subject}{label}在 {len(distinct_days)} 个不同日期反复出现，"
                "且方向并不一致；应理解为仍在变化，而不是单向结论。"
            )
        else:
            claim = (
                f"近 {cfg.recent_window_days} 天，{subject}{label}在 {len(distinct_days)} 个不同日期持续出现，"
                "可视为近期心理背景，但不能替代当前对话证据。"
            )
        evidence = [_evidence(item, f"近期不同日期的{label}证据", score) for item in selected]
        candidates.append(_candidate(
            "recent_trend", "近期连续趋势", claim, score, evidence,
            mixed=polarity == "mixed",
        ))
    candidates.sort(key=lambda item: item["score"], reverse=True)
    deduped: list[dict[str, Any]] = []
    for item in candidates:
        if all(_text_similarity(item["claim"], old["claim"]) < 0.72 for old in deduped):
            deduped.append(item)
    return deduped[:4]


def _seasonal_candidates(
    memories: list[dict[str, Any]], today: date, cfg: EngineConfig,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for memory in memories:
        if memory["event_date"].month != today.month or memory["event_date"].year >= today.year:
            continue
        if memory["memory_type"] not in PERSONA_TYPES:
            continue
        entity = memory["entities_list"][0] if memory["entities_list"] else ""
        groups.setdefault((memory["memory_type"], entity), []).append(memory)
    candidates: list[dict[str, Any]] = []
    for (memory_type, entity), members in groups.items():
        years = sorted({item["event_date"].year for item in members})
        if len(years) < cfg.seasonal_min_years:
            continue
        selected: list[dict[str, Any]] = []
        for year in years:
            choices = [item for item in members if item["event_date"].year == year]
            selected.append(max(choices, key=lambda item: item["quality"]))
        average = sum(item["quality"] for item in selected) / len(selected)
        # Independent years are the defining evidence for a seasonal pattern.
        # Reward that recurrence explicitly while retaining source quality as
        # the largest component; three weak notes still cannot pass the gate.
        recurrence = min(1.0, len(years) / max(3, cfg.seasonal_min_years))
        score = average * 0.68 + recurrence * 0.22 + min(0.10, len(years) * 0.025)
        if score < max(0.76, cfg.min_evidence_score + 0.06):
            continue
        polarity = _polarity([item["text"] for item in selected])
        label = TYPE_LABELS.get(memory_type, memory_type)
        subject = f"“{entity}”相关" if entity else ""
        if polarity == "mixed":
            claim = (
                f"过去 {len(years)} 个年份的 {today.month} 月都出现过{subject}{label}，但方向彼此冲突；"
                "它至多说明这个月份容易承载相关联想，不能推断今年会重复。"
            )
        else:
            claim = (
                f"过去 {len(years)} 个年份的 {today.month} 月都出现过{subject}{label}；"
                "这是一条跨年季节性线索，不是因果规律，也不能推断今年必然重演。"
            )
        evidence = [_evidence(item, f"跨 {len(years)} 个年份的同月证据", score) for item in selected[:5]]
        candidates.append(_candidate(
            "seasonal_pattern", "跨年季节模式", claim, score, evidence,
            mixed=polarity == "mixed",
        ))
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates[:2]


def build_candidates(
    memories: list[dict[str, Any]],
    now: datetime,
    cfg: EngineConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared = prepare_memories(memories, now, cfg)
    candidates = [
        *_anniversary_candidates(prepared, now.date(), cfg),
        *_trend_candidates(prepared, now.date(), cfg),
        *_seasonal_candidates(prepared, now.date(), cfg),
    ]
    priority = {
        "anniversary_exact": 4,
        "anniversary_nearby": 3,
        "recent_trend": 2,
        "seasonal_pattern": 1,
    }
    candidates.sort(
        key=lambda item: (priority.get(item["kind"], 0), item["score"]),
        reverse=True,
    )
    stats = {
        "engine_version": 3,
        "generated_date": now.date().isoformat(),
        "source_memories": len(memories),
        "dated_memories": len(prepared),
        "candidate_count": len(candidates),
        "candidate_types": {
            kind: sum(1 for item in candidates if item["kind"] == kind)
            for kind in priority
        },
        "excluded_undated_or_invalid": max(0, len(memories) - len(prepared)),
    }
    return candidates, stats


def select_static_candidates(candidates: list[dict[str, Any]], cfg: EngineConfig) -> list[dict[str, Any]]:
    """Select only safe ambient signals; other candidates remain query-scoped.

    Exact anniversaries are calendar facts. Recent trends need a higher margin
    because repeating them on unrelated turns is much easier to overstate.
    Nearby anniversaries and seasonal patterns are never ambient.
    """
    selected: list[dict[str, Any]] = []
    kind_counts: dict[str, int] = {}
    for candidate in candidates:
        if not candidate.get("static_eligible"):
            continue
        kind = str(candidate.get("kind") or "")
        if kind not in {"anniversary_exact", "recent_trend"}:
            continue
        confidence = float(candidate.get("confidence") or 0)
        score = float(candidate.get("score") or 0)
        margin = 0.08 if kind == "recent_trend" else 0.0
        if confidence < cfg.min_evidence_score + margin or score < cfg.min_evidence_score + margin:
            continue
        max_for_kind = 1
        if kind_counts.get(kind, 0) >= max_for_kind:
            continue
        if any(_text_similarity(candidate["claim"], old["claim"]) >= 0.76 for old in selected):
            continue
        selected.append(candidate)
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if len(selected) >= cfg.static_max_insights:
            break
    selected_ids = {item["candidate_id"] for item in selected}
    for candidate in candidates:
        candidate["static_selected"] = candidate["candidate_id"] in selected_ids
    return selected


def _render_candidate(candidate: dict[str, Any]) -> str:
    evidence = list(candidate.get("evidence") or [])
    evidence_text = "；".join(
        f"{item.get('date')} {clean_text(item.get('text'), 92)}"
        for item in evidence[:3]
    )
    return (
        f"- {candidate.get('title')}（置信 {float(candidate.get('confidence') or 0):.2f}）："
        f"{clean_text(candidate.get('claim'), 240)}"
        + (f" 证据：{evidence_text}" if evidence_text else "")
    )


def render_static_block(
    candidates: list[dict[str, Any]],
    now: datetime,
    cfg: EngineConfig,
) -> str:
    if not candidates:
        return ""
    lines = [
        f"当前日历基准：{now.date().isoformat()}（{cfg.timezone}）。以下只是在今天成立的高置信历史时间回声。",
        "严格边界：它们不是当前事件，也不表示旧事正在重演；只可形成轻微余韵。除非本轮明确谈到相关旧事，否则不要主动复述日期或事件。",
    ]
    for candidate in candidates:
        line = _render_candidate(candidate)
        if len("\n".join([*lines, line])) > cfg.injection_max_chars:
            break
        lines.append(line)
    return "\n".join(lines) if len(lines) > 2 else ""


def _query_date_tokens(query: str) -> tuple[set[str], bool]:
    found: set[str] = set()
    for match in re.finditer(r"((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", query):
        found.add(f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}")
    for match in re.finditer(r"(?<!\d)(\d{1,2})月(\d{1,2})日", query):
        found.add(f"{int(match.group(1)):02d}-{int(match.group(2)):02d}")
    temporal = bool(found) or any(marker in query for marker in TEMPORAL_QUERY_MARKERS)
    return found, temporal


def select_query_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    cfg: EngineConfig,
) -> list[dict[str, Any]]:
    query = clean_text(query, 2400)
    if not query:
        return []
    query_terms = terms(query)
    date_tokens, temporal_intent = _query_date_tokens(query)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        candidate_terms = set(candidate.get("terms") or [])
        overlap = len(query_terms & candidate_terms) / max(1, min(len(query_terms), 16))
        evidence = list(candidate.get("evidence") or [])
        exact_date = any(
            item.get("date") in date_tokens or str(item.get("date") or "")[5:] in date_tokens
            for item in evidence
        )
        entity_hit = any(
            entity and entity in query
            for item in evidence
            for entity in list(item.get("entities") or [])
        )
        temporal_bonus = (
            0.18
            if temporal_intent and str(candidate.get("kind", "")).startswith("anniversary")
            else 0.0
        )
        score = (
            float(candidate.get("score") or 0) * 0.34
            + min(1.0, overlap * 2.2) * 0.38
            + (0.34 if exact_date else 0.0)
            + (0.20 if entity_hit else 0.0)
            + temporal_bonus
        )
        if candidate.get("static_selected") and not (exact_date or overlap >= 0.22 or entity_hit):
            continue
        if score < cfg.query_min_score:
            continue
        enriched = dict(candidate)
        enriched["query_score"] = round(min(1.0, score), 4)
        enriched["query_reasons"] = [
            reason for reason, matched in (
                ("explicit_date", exact_date),
                ("entity", entity_hit),
                ("temporal_intent", temporal_intent),
                ("lexical_overlap", overlap >= 0.08),
            ) if matched
        ]
        ranked.append((score, enriched))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in ranked[:cfg.query_max_insights]]


def render_query_block(
    selected: list[dict[str, Any]],
    now: datetime,
    cfg: EngineConfig,
) -> str:
    if not selected:
        return ""
    lines = [
        f'<QueryScopedHistoricalTimeInsight current_date="{now.date().isoformat()}" temporal_role="historical_evidence">',
        "这些历史时间线索由本轮问题触发。它们不是当前发生的事件；只回答与问题相关的部分，不要把日期相近误写成命运、因果或必然重演。",
    ]
    for candidate in selected:
        line = _render_candidate(candidate)
        if len("\n".join([*lines, line, "</QueryScopedHistoricalTimeInsight>"])) > cfg.query_injection_max_chars:
            break
        lines.append(line)
    if len(lines) == 2:
        return ""
    lines.append("</QueryScopedHistoricalTimeInsight>")
    return "\n".join(lines)


def flatten_evidence(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        for item in list(candidate.get("evidence") or []):
            key = (str(item.get("memo_name") or ""), str(item.get("date") or ""))
            if key in seen:
                continue
            seen.add(key)
            flattened.append({
                **item,
                "candidate_id": candidate.get("candidate_id"),
                "candidate_kind": candidate.get("kind"),
                "candidate_title": candidate.get("title"),
                "candidate_score": candidate.get("score"),
                "candidate_confidence": candidate.get("confidence"),
                "synthesis": candidate.get("synthesis", "deterministic"),
            })
    return flattened
