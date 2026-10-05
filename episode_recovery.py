"""Deterministic recovery for evidence-first memory generation.

The normal extractor remains authoritative. This module is used only after the
structured LLM extraction and its compact retry both fail, so a transient model
timeout cannot discard source traceability for the whole compression batch.
"""
from __future__ import annotations

import math
from itertools import combinations
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


_MUST_WORDS = (
    "答应", "承诺", "约定", "发誓", "保证", "一定", "不会离开", "不要离开",
    "不许", "边界", "底线", "拒绝", "分开", "在一起", "关系", "原谅",
    "告白", "秘密", "真相", "决定", "选择", "以后", "永远",
)
_RELATION_WORDS = (
    "关系", "信任", "依赖", "喜欢", "爱", "告白", "吃醋", "原谅", "和好",
    "分开", "离开", "陪伴", "害怕失去", "安全感", "昵称", "称呼",
)
_EMOTION_WORDS = (
    "难过", "害怕", "开心", "安心", "委屈", "生气", "愧疚", "心疼", "想念",
    "不安", "绝望", "哭", "笑", "沉默", "犹豫", "期待", "失落",
)
_BACKCHANNELS = {
    "嗯", "嗯嗯", "哦", "好", "好的", "行", "知道了", "哈哈", "嘿嘿", "晚安",
    "早安", "在吗", "你好", "谢谢", "没事", "没关系",
}

_STRONG_MUST_WORDS = (
    "答应", "承诺", "约定", "发誓", "保证", "不会离开", "不要离开",
    "不许", "边界", "底线", "拒绝", "分开", "在一起", "原谅",
    "告白", "秘密", "真相", "决定", "选择", "永远",
)


def _is_must_write(text: str) -> bool:
    if any(word in text for word in _STRONG_MUST_WORDS):
        return True
    # "以后"、"一定"在角色扮演日常里非常常见，单独出现并不等于长期承诺。
    return bool(re.search(
        r"(?:以后|将来).{0,14}(?:要|会|别|不再|一起|记得|陪|等)|"
        r"一定.{0,12}(?:做到|回来|陪|记住|不离开|告诉|兑现)",
        text,
    ))


def _clean(value: Any, limit: int = 800) -> str:
    return " ".join(str(value or "").split()).strip()[: max(1, int(limit))]


def _message_time(message: dict[str, Any], timezone_name: str) -> tuple[str, str]:
    try:
        timestamp = float(
            message.get("event_ts")
            or message.get("recorded_ts")
            or message.get("created_ts")
            or 0.0
        )
    except (TypeError, ValueError, OverflowError):
        timestamp = 0.0
    if timestamp <= 0:
        return "", ""
    zone_name = str(message.get("event_timezone") or message.get("timezone") or timezone_name)
    try:
        local = datetime.fromtimestamp(timestamp, ZoneInfo(zone_name))
    except Exception:
        try:
            local = datetime.fromtimestamp(timestamp, ZoneInfo(timezone_name))
        except Exception:
            local = datetime.fromtimestamp(timestamp)
    hour = local.hour
    if hour < 5:
        label = "凌晨"
    elif hour < 8:
        label = "清晨"
    elif hour < 12:
        label = "上午"
    elif hour < 14:
        label = "正午"
    elif hour < 17:
        label = "下午"
    elif hour < 19:
        label = "傍晚"
    elif hour < 23:
        label = "晚上"
    else:
        label = "深夜"
    return local.strftime("%Y-%m-%d"), label


def _candidate_value(candidate: Any, name: str, default: Any) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _scene_ranges(
    messages: list[dict[str, Any]], candidates: list[Any], max_episodes: int,
) -> list[tuple[int, int, list[str]]]:
    count = len(messages)
    if count <= 0:
        return []
    usable: list[tuple[int, int, list[str]]] = []
    for candidate in candidates or []:
        try:
            start = max(0, min(count - 1, int(_candidate_value(candidate, "start_turn", 0))))
            end = max(start, min(count - 1, int(_candidate_value(candidate, "end_turn", count - 1))))
        except (TypeError, ValueError, OverflowError):
            continue
        reasons = [
            _clean(item, 80) for item in (_candidate_value(candidate, "reasons", []) or [])
            if _clean(item, 80)
        ]
        usable.append((start, end, reasons))
    usable.sort(key=lambda item: (item[0], item[1]))
    if not usable:
        usable = [(0, count - 1, ["local_recovery"])]

    # Max episodes is the planned target, not a reason to materialize every
    # lexical candidate. Preserve recorded date/gap boundaries even if a
    # caller undershot them, then choose balanced cuts on complete exchanges.
    hard = {"date_change", "time_gap"}
    mandatory = {
        start for start, _end, reasons in usable[1:] if hard.intersection(reasons)
    }
    limit = max(1, min(len(usable), int(max_episodes or 1)))
    limit = max(limit, len(mandatory) + 1)
    if limit == 1:
        return [(0, count - 1, list(dict.fromkeys(
            reason for _start, _end, reasons in usable for reason in reasons
        )))]

    options: dict[int, list[str]] = {}
    for start, _end, reasons in usable[1:]:
        aligned = start
        if (start + 1 < count and messages[start].get("role") == "assistant"
                and messages[start + 1].get("role") == "user"
                and not hard.intersection(reasons)):
            aligned += 1
        options.setdefault(aligned, []).extend(reasons)
    mandatory = {
        start for start in mandatory if start in options
    }
    lengths = [len(str(message.get("content") or "")) for message in messages]
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)
    total = max(1, prefix[-1])
    best: tuple[float, tuple[int, ...]] | None = None
    optional_cuts = sorted(set(options) - mandatory)
    for extra in combinations(optional_cuts, max(0, limit - 1 - len(mandatory))):
        cuts = tuple(sorted(mandatory | set(extra)))
        if len(cuts) != limit - 1:
            continue
        bounds = (0, *cuts, count)
        spans = [prefix[right] - prefix[left]
                 for left, right in zip(bounds, bounds[1:])]
        ideal = total / limit
        score = sum(abs(span - ideal) for span in spans)
        score += sum(ideal for span in spans if span < ideal * 0.35)
        if best is None or (score, cuts) < best:
            best = (score, cuts)
    cuts = best[1] if best is not None else tuple(sorted(mandatory))
    bounds = (0, *cuts, count)
    grouped: list[tuple[int, int, list[str]]] = []
    for left, right in zip(bounds, bounds[1:]):
        reasons = list(dict.fromkeys(
            reason for start, _end, values in usable
            if left < start < right for reason in values
        ))
        if left in options:
            reasons = list(dict.fromkeys(options[left] + reasons))
        grouped.append((left, right - 1, reasons + ["merged_for_recovery"]))
    return grouped


def _salient_long_batch_evidence(
    evidence: list[dict[str, Any]], *, minimum_budget: int = 6,
) -> list[dict[str, Any]]:
    """Project a long archived scene into diary evidence, not a transcript.

    Every source turn remains in the raw archive and episode scene range.
    Explicit commitments are always retained; supporting moments are spread
    across the scene so a late development is not lost to an early excerpt.
    """
    must = [item for item in evidence if item["tier"] == "must_write"]
    support = [item for item in evidence if item["tier"] == "supporting"]
    budget = max(
        minimum_budget,
        min(10, math.ceil(len(evidence) / 6)),
        len(must) + min(4, len(support)),
    )
    slots = max(0, budget - len(must))
    selected = list(must)
    if slots and support:
        for slot in range(min(slots, len(support))):
            left = math.floor(slot * len(support) / min(slots, len(support)))
            right = math.floor((slot + 1) * len(support) / min(slots, len(support)))
            segment = support[left:right]
            selected.append(max(segment, key=lambda item: (
                sum(word in item["detail"] for word in _EMOTION_WORDS) * 2
                + sum(word in item["detail"] for word in _RELATION_WORDS)
                + min(len(item["detail"]), 320) / 320,
                -item["turn_indexes"][0],
            )))
    selected.sort(key=lambda item: item["turn_indexes"][0])
    return selected


def project_grounded_evidence_for_render(
    evidence: list[dict[str, Any]], *, minimum_budget: int = 6,
) -> list[dict[str, Any]]:
    """Build a short render view without changing the persisted Episode evidence."""
    excerpts = []
    for item in evidence:
        if not isinstance(item, dict) or not item.get("turn_indexes"):
            continue
        tier = str(item.get("tier") or "supporting")
        if tier == "archive_only":
            continue
        projected = dict(item)
        projected["detail"] = _evidence_excerpt(str(item.get("detail") or ""), tier)
        excerpts.append(projected)
    return _salient_long_batch_evidence(excerpts, minimum_budget=minimum_budget)


def _evidence_kind(text: str) -> str:
    if any(word in text for word in ("答应", "承诺", "约定", "发誓", "保证")):
        return "commitment"
    if any(word in text for word in ("不许", "边界", "底线", "拒绝", "不能")):
        return "boundary"
    if any(word in text for word in ("拿", "放", "递", "抱", "吻", "走", "坐", "站", "看")):
        return "action"
    if any(word in text for word in ("钥匙", "戒指", "礼物", "照片", "信", "物件")):
        return "object"
    return "dialogue"


def _evidence_excerpt(raw_text: str, tier: str) -> str:
    """Keep a grounded focal span, leaving the full turn in the raw archive."""
    limit = 160
    if len(raw_text) <= limit:
        return raw_text
    start = 0
    if tier == "must_write":
        hits = [raw_text.find(word) for word in _MUST_WORDS if word in raw_text]
        if hits:
            start = max(0, min(hits) - 70)
    excerpt = raw_text[start:start + limit].strip()
    return ("…" if start else "") + excerpt + (
        "…" if start + limit < len(raw_text) else ""
    )


def build_local_grounded_episodes(
    messages: list[dict[str, Any]],
    candidates: list[Any] | None,
    max_episodes: int,
    timezone_name: str = "Asia/Shanghai",
) -> list[dict[str, Any]]:
    """Build conservative source-grounded Episodes without inferring psychology."""
    episodes: list[dict[str, Any]] = []
    for episode_index, (start, end, reasons) in enumerate(
        _scene_ranges(messages, list(candidates or []), max_episodes), start=1,
    ):
        evidence: list[dict[str, Any]] = []
        all_text: list[str] = []
        for turn_index in range(start, end + 1):
            message = messages[turn_index]
            raw_text = _clean(message.get("content"), 6000 if len(messages) >= 32 else 1200)
            if not raw_text:
                continue
            all_text.append(raw_text)
            role = str(message.get("role") or "").strip().lower()
            actor = "assistant" if role == "assistant" else "user" if role == "user" else "双方"
            compact = re.sub(r"[\s，。！？!?、；;：:]", "", raw_text)
            if compact in _BACKCHANNELS or (len(compact) <= 4 and not any(
                word in raw_text for word in _MUST_WORDS
            )):
                tier = "archive_only"
            elif _is_must_write(raw_text):
                tier = "must_write"
            else:
                tier = "supporting"
            fact_text = _evidence_excerpt(raw_text, tier)
            if actor == "assistant":
                detail = "我当时说或表达：" + fact_text
            elif actor == "user":
                detail = "对方当时说或表达：" + fact_text
            else:
                detail = "对话中记录：" + fact_text
            evidence.append({
                "kind": _evidence_kind(raw_text),
                "actor": actor,
                "detail": detail[:1200],
                "quote": raw_text[:180],
                "turn_indexes": [turn_index],
                "confidence": 1.0,
                "grounded": True,
                "tier": tier,
            })
        if not evidence:
            continue
        if not any(item["tier"] != "archive_only" for item in evidence):
            evidence[0]["tier"] = "supporting"

        if len(evidence) >= 8:
            evidence = _salient_long_batch_evidence(evidence)

        joined = " ".join(all_text)
        first_message = next((messages[index] for index in range(start, end + 1)
                              if _clean(messages[index].get("content"))), messages[start])
        event_date, time_label = _message_time(first_message, timezone_name)
        if any(word in joined for word in _MUST_WORDS):
            memory_type = "promise_or_rule"
            importance = 5 if any(word in joined for word in _RELATION_WORDS) else 4
        elif any(word in joined for word in _RELATION_WORDS):
            memory_type = "relationship_shift"
            importance = 4
        elif any(word in joined for word in _EMOTION_WORDS):
            memory_type = "emotional_anchor"
            importance = 4
        else:
            memory_type = "daily_texture"
            importance = 3
        anchor_source = next(
            (item["detail"] for item in evidence if item["tier"] == "must_write"),
            next((item["detail"] for item in evidence if item["tier"] == "supporting"), evidence[0]["detail"]),
        )
        retrieval_parts = [
            item["detail"][:120] for item in evidence if item["tier"] != "archive_only"
        ][:5]
        episodes.append({
            "episode_key": f"recovery_e{episode_index}",
            "event_date": event_date,
            "time_label": time_label,
            "time_basis": "conversation_now" if event_date else "unknown",
            "scene_anchor": anchor_source[:80],
            "scene_start_turn": start,
            "scene_end_turn": end,
            "scene_boundary_reasons": list(dict.fromkeys(reasons + ["local_grounded_recovery"])),
            "memory_type": memory_type,
            "evidence": evidence,
            "affect_before": "",
            "affect_after": "",
            "state_change": "",
            "long_effect": "",
            "trigger_hint": "",
            "retrieval_key": " ".join(retrieval_parts)[:600],
            "entities": [],
            "unresolved": [],
            "tags": [],
            "importance": importance,
            "_episode_extraction_mode": "local_grounded_recovery",
        })
    return episodes
