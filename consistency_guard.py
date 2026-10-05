"""Conservative checks against evidence actually injected for one response.

The guard is observational only. It records findings for WebUI review and never
rewrites a request or answer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from typing import Any

from .text_overlap import longest_common_substring


_SKIP = ("/memos-", "插件管理", "plugin management", "chunk", "分块")
_NEG = re.compile(r"(?:不再|不是|没有|并非|不能|不会|不可以|不要|禁止|不想|未曾|never|not|no)", re.I)
_HIST = re.compile(r"历史|过去|曾经|以前|当时|那时候|截至|梦里|梦中|假设|转述|说过|台词|historical|formerly|previously", re.I)
_NOW = re.compile(r"现在|当前|目前|如今|此刻|仍然|依然|还是|今天|刚刚|today|now|currently", re.I)
_SOFT = re.compile(r"如果|假如|也许|可能|或许|不确定|仿佛|好像|难道|是不是|是否|假设|据说|有人说|[?？]", re.I)
_REALITY = re.compile(r"梦境|梦里|梦中|dream|假设|hypothesis|设想|虚构|想象", re.I)
_REAL_ASSERT = re.compile(r"现实中|现实里|真的发生|实际发生|确实发生|真实经历|in reality|actually happened", re.I)
_CAUSAL = re.compile(r"因为|因此|导致|所以|先于|之后|before|after|caused|therefore", re.I)
_DATE = re.compile(r"(?:(\d{4})\s*[年/-])?(\d{1,2})\s*[月/-](\d{1,2})(?:日|号)?")
_YEAR = re.compile(r"(?<!\d)(\d{4})\s*年?(?!\d)")
_NON_ASSERTION = re.compile(r'“[^”]*”|「[^」]*」|"[^"\n]*"|（[^）]*）|\([^)]*\)')


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text", value.get("content", value.get("value", value.get("object", ""))))
    return " ".join(str(value or "").split()).strip()


def _references(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    raw = snapshot.get("references")
    if raw is None:
        raw = snapshot.get("evidence")
    if raw is None:
        raw = snapshot.get("history", snapshot.get("contexts", []))
    if isinstance(raw, dict):
        flattened: list[dict[str, Any]] = []
        for category, values in raw.items():
            if not isinstance(values, list):
                continue
            flattened.extend(
                {**dict(item), "category": str(item.get("category") or category)}
                for item in values if isinstance(item, dict)
            )
        raw = flattened
    return [dict(item) for item in raw if isinstance(item, dict)][:64] if isinstance(raw, list) else []


def _reference_text(ref: dict[str, Any]) -> str:
    return _text(ref.get("text", ref.get("object", ref.get("content", ref.get("description", "")))))


def _assertions(text: str) -> list[str]:
    text = _NON_ASSERTION.sub("", text)
    return [part.strip() for part in re.split(r"(?<=[。！？!?\n])", text) if len(part.strip()) >= 3]


def _top_level_date_contexts(text: str) -> list[tuple[tuple[int, int, int], str, bool]]:
    masked = _NON_ASSERTION.sub(lambda match: " " * len(match.group(0)), text)
    output: list[tuple[tuple[int, int, int], str, bool]] = []
    for match in _DATE.finditer(masked):
        year, month, day = match.groups()
        value = (int(year or 0), int(month), int(day))
        if 1 <= value[1] <= 12 and 1 <= value[2] <= 31:
            clause_start = max(
                masked.rfind(mark, 0, match.start()) for mark in "。！？!?\n"
            ) + 1
            prefix = masked[clause_start:match.start()]
            output.append((
                value,
                text[max(0, match.start() - 120):match.end() + 700],
                bool(_SOFT.search(prefix)),
            ))
    return output


def _dates(text: str) -> list[tuple[int, int, int]]:
    return [
        (int(year or 0), int(month), int(day))
        for year, month, day in _DATE.findall(str(text or ""))
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31
    ]


def _compatible(left: tuple[int, int, int], right: tuple[int, int, int]) -> bool:
    return left[1:] == right[1:] and (not left[0] or not right[0] or left[0] == right[0])


def _date_compatible(answer: str, evidence: str) -> bool:
    answer_dates, evidence_dates = _dates(answer), _dates(evidence)
    if not answer_dates or not evidence_dates:
        years = [int(value) for value in _YEAR.findall(answer)]
        evidence_years = [value[0] for value in evidence_dates if value[0]]
        return not years or not evidence_years or any(
            year == expected for year in years for expected in evidence_years
        )
    return any(_compatible(actual, expected) for actual in answer_dates for expected in evidence_dates)


def _anchor(value: str) -> str:
    value = _DATE.sub("", str(value or ""))
    value = re.sub(
        r"我们|你们|他们|现在|目前|已经|仍然|依然|后来|以前|最初|今天|刚刚|曾经|当时|答应|完成|解决|不再|不是|没有|并非|不能|不会|不要|禁止|不想|可以|关系|事情|事项|未完成",
        "",
        value,
    )
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value.lower())


def _align(value: str, target_text: str) -> bool:
    anchor = _anchor(value)
    target = _anchor(target_text)
    if len(anchor) < 2:
        return False
    grams = {anchor[index:index + 3] for index in range(len(anchor) - 2)}
    matched = sum(gram in target for gram in grams)
    return (
        anchor in target
        or (len(anchor) == 2 and anchor in target)
        or longest_common_substring(anchor, target) >= 8
        or matched >= max(2, int(len(grams) * 0.65))
    )


def _visible_reference(ref: dict[str, Any], injected: str) -> bool:
    text = _reference_text(ref)
    injected = str(injected or "").strip()
    return bool(text and injected and (text in injected or _align(text, injected)))


def _is_nonfactual_reference(ref: dict[str, Any]) -> bool:
    """Return whether a reference is unsuitable as factual proof.

    Dream and hypothetical material can still be reviewed for an explicit
    reality-status assertion, but must never prove dates or ordinary facts.
    """
    status = str(ref.get("status") or "").strip().lower()
    memory_type = str(ref.get("memory_type") or "").strip().lower()
    body = _reference_text(ref)
    return (
        status in {"dream", "hypothetical", "fictional", "uncertain"}
        or memory_type in {"dream", "hypothetical", "fictional", "uncertain"}
        or bool(_REALITY.search(body))
    )


def _source_id(ref: dict[str, Any]) -> str:
    return str(
        ref.get("source_id") or ref.get("id") or ref.get("episode_id")
        or ref.get("claim_id") or ref.get("item_id") or ""
    )


def _excerpt(answer: str, phrase: str = "") -> str:
    phrase = phrase or answer[:80]
    index = answer.lower().find(phrase.lower())
    return answer[max(0, index - 50):index + len(phrase) + 80] if index >= 0 else answer[:160]


def _finding(
    answer: str,
    ref: dict[str, Any],
    error_type: str,
    description: str,
    *,
    confidence: float | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    text = _reference_text(ref)
    quality = str(ref.get("source_quality") or ref.get("evidence_quality") or "unknown")
    strong = quality in {"A", "exact", "source_grounded", "source_exact", "source_turn"}
    default_confidence = 0.94 if strong else 0.68 if quality == "diary_derived" else 0.9
    confidence = float(confidence if confidence is not None else default_confidence)
    severity = str(severity or ("high" if strong or quality == "unknown" else "medium"))
    source_id = _source_id(ref)
    return {
        "answer_excerpt": _excerpt(answer, text[:80]),
        "response_excerpt": _excerpt(answer, text[:80]),
        "evidence_quote": text[:500],
        "source_id": source_id,
        "source_quality": quality,
        "description": description,
        "confidence": confidence,
        "severity": severity,
        "error_type": error_type,
        "rule_id": "CG-" + error_type,
        "decision_source": "answer_vs_injected_reference",
        "evidence": {
            "id": str(ref.get("id") or source_id),
            "source_id": source_id,
            "text": text[:500],
            "category": str(ref.get("category") or ""),
        },
    }


def evaluate(snapshot: dict[str, Any] | None, timeout: float | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    data = snapshot if isinstance(snapshot, dict) else {}
    query = _text(data.get("query", data.get("query_text", data.get("user_query", ""))))
    full_answer = str(data.get("answer", data.get("response_text", "")) or "")
    answer = full_answer[:12000]
    if not query or any(word.lower() in query.lower() for word in _SKIP) or data.get("thread_used") is False:
        return {"skipped": True, "reason": "excluded", "observations": [], "findings": [], "candidates": []}
    if not answer.strip():
        return {"skipped": True, "reason": "no_answer", "observations": [], "findings": [], "candidates": []}

    refs = _references(data)
    injected = str(data.get("thread_text") or "")
    visible_refs = [ref for ref in refs if _visible_reference(ref, injected)]
    findings: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    def over_budget() -> bool:
        return timeout is not None and time.perf_counter() - started > max(0.0, float(timeout))

    def add(ref: dict[str, Any], kind: str, sentence: str, description: str, *, certain: bool = True) -> None:
        entry = _finding(sentence, ref, kind, description)
        target = findings if certain else candidates
        key = (
            entry["error_type"], entry["source_id"], entry["response_excerpt"],
            entry["evidence_quote"],
        )
        if not any(
            (item.get("error_type"), item.get("source_id"), item.get("response_excerpt"), item.get("evidence_quote")) == key
            for item in target
        ):
            target.append(entry)

    for actual, context, soft_date in _top_level_date_contexts(answer):
        for ref in visible_refs:
            if _is_nonfactual_reference(ref):
                continue
            body = _reference_text(ref)
            occurred_at = str(ref.get("occurred_at") or ref.get("ts_text") or "")
            date_evidence = occurred_at or body
            expected = _dates(date_evidence)
            if (
                len(expected) == 1
                and date_evidence
                and date_evidence in injected
                and _align(body, context)
                and not any(_compatible(actual, value) for value in expected)
                and not soft_date
            ):
                add(ref, "date_conflict", context, "同一事件的日期与本轮实际注入证据不一致。")

    for sentence in _assertions(answer):
        if over_budget():
            return {
                "skipped": True,
                "reason": "local_budget",
                "status": "skipped",
                "observations": [],
                "findings": [],
                "candidates": [],
                "response_truncated": len(full_answer) > len(answer),
                "inspected_chars": len(answer),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        if _SOFT.search(sentence):
            continue
        sentence_historical = bool(_HIST.search(sentence))
        for ref in visible_refs:
            body = _reference_text(ref)
            aligned = _align(body, sentence)
            if not aligned and _CAUSAL.search(sentence):
                body_anchor = _anchor(body)
                sentence_anchor = _anchor(sentence)
                if len(body_anchor) >= 4 and longest_common_substring(body_anchor, sentence_anchor) >= 2:
                    add(
                        ref, "unsupported_causality", sentence,
                        "回答添加了证据未支持的明确因果或先后关系。", certain=False,
                    )
            if not aligned:
                continue
            status = str(ref.get("status") or "").lower()
            memory_type = str(ref.get("memory_type") or "").lower()
            category = str(ref.get("category") or "").lower()
            if status == "uncertain":
                continue

            old_state = bool(ref.get("counterevidence")) or status in {"superseded", "historical"}
            if old_state and not sentence_historical and _NOW.search(sentence) and not _NEG.search(sentence):
                add(ref, "historical_as_current", sentence, "回答把历史状态冒充当前状态。")

            non_real = _is_nonfactual_reference(ref)
            if (
                non_real
                and status != "uncertain"
                and not sentence_historical
                and _REAL_ASSERT.search(sentence)
                and not _NEG.search(sentence)
            ):
                add(
                    ref,
                    "reality_status_conflict",
                    sentence,
                    "回答可能把梦境或假设证据表述为现实事实。",
                    certain=False,
                )

            # Non-factual references may only create the explicit reality-status
            # review candidate above. They cannot support the factual rules below.
            if non_real:
                continue

            if status in {"resolved", "completed"} and re.search(
                r"仍|尚未|待|未完成|没完成|没解决|pending|ongoing|not done", sentence, re.I
            ):
                add(ref, "resolved_pending_conflict", sentence, "回答将已解决事项表述为仍未完成。")

            if category in {"claim", "claims", "current_claims"} and status == "active" and str(
                ref.get("claim_type") or ""
            ) in {"boundary", "promise_or_rule", "rule"}:
                if bool(_NEG.search(body)) != bool(_NEG.search(sentence)):
                    add(ref, "direct_negation_conflict", sentence, "回答对注入边界或约定作了相反断言。")
            elif status not in {"resolved", "completed"}:
                body_done = bool(re.search(r"已完成|完成了|done|resolved", body, re.I))
                answer_pending = bool(re.search(r"未完成|没有完成|没完成|not done|pending", sentence, re.I))
                body_pending = bool(re.search(r"未完成|没有完成|没完成|not done|pending", body, re.I))
                answer_done = bool(re.search(r"已完成|完成了|done|resolved", sentence, re.I))
                if (body_done and answer_pending) or (body_pending and answer_done):
                    add(ref, "direct_negation_conflict", sentence, "回答对注入证据中的明确事实作了相反断言。")

            before = str(ref.get("from_object") or "")
            after = str(ref.get("to_object") or "")
            if before and after and before in injected and after in injected:
                before_pos, after_pos = sentence.find(before), sentence.find(after)
                between = sentence[after_pos + len(after):before_pos] if after_pos >= 0 and before_pos >= 0 else ""
                reversed_order = (
                    before_pos >= 0
                    and after_pos >= 0
                    and after_pos < before_pos
                    and "先" in sentence[:after_pos + 2]
                    and bool(re.search(r"后来|然后|之后|再(?:次)?|才", between))
                    and "不是先" not in sentence[:after_pos + 3]
                ) or bool(re.search(re.escape(before) + r".{0,12}(?:发生|出现)?在" + re.escape(after) + r"之后", sentence))
                if reversed_order and str(ref.get("transition_type") or "") in {
                    "supersedes", "resolves", "breaks", "contradicts_stage", "continues",
                }:
                    add(ref, "event_order", sentence, "回答明确颠倒了有证据支持的事件先后。")

            if _CAUSAL.search(sentence) and not _CAUSAL.search(body):
                add(ref, "unsupported_causality", sentence, "回答添加了证据未支持的明确因果或先后关系。", certain=False)

    findings = findings[:32]
    candidates = candidates[:8]
    return {
        "skipped": False,
        "incomplete": False,
        "reason": "checked",
        "status": "flagged" if findings else "review" if candidates else "clean",
        "observations": findings,
        "findings": findings,
        "candidates": candidates,
        "checked_references": len(visible_refs),
        "response_truncated": len(full_answer) > len(answer),
        "inspected_chars": len(answer),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def check_response(
    snapshot: dict[str, Any], response: str, *, budget_ms: float = 25, max_chars: int = 12000
) -> dict[str, Any]:
    if budget_ms <= 0:
        return {
            "status": "skipped", "reason": "local_budget", "findings": [], "candidates": [],
            "response_truncated": len(str(response or "")) > max_chars,
            "inspected_chars": min(len(str(response or "")), max_chars), "elapsed_ms": 0.0,
        }
    data = dict(snapshot or {})
    data["answer"] = str(response or "")[:max_chars]
    result = evaluate(data, timeout=float(budget_ms) / 1000.0)
    result["response_truncated"] = len(str(response or "")) > max_chars
    result["inspected_chars"] = min(len(str(response or "")), max_chars)
    return result


def validate_arbitration(raw: str, candidates: list[dict[str, Any]], response: str) -> list[dict[str, Any]]:
    try:
        text = str(raw or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(text)
    except Exception:
        return []
    rows = parsed.get("decisions", []) if isinstance(parsed, dict) else []
    if not isinstance(rows, list):
        return []
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        index = row.get("candidate")
        quote = str(row.get("response_quote") or "")
        if type(index) is not int or index in seen or not 0 <= index < len(candidates):
            continue
        if row.get("verdict") != "conflict" or not quote or quote not in response or quote not in str(candidates[index].get("response_excerpt", "")):
            continue
        try:
            confidence = float(row.get("confidence"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(confidence):
            continue
        seen.add(index)
        output.append({
            **candidates[index], "decision_source": "llm",
            "confidence": min(0.85, max(0.0, confidence)),
            "response_excerpt": quote, "severity": "medium",
        })
    return output


class ConsistencyGuard:
    def __init__(self, llm=None, timeout: float = 1.0):
        self.llm = llm
        self.timeout = max(0.01, float(timeout))

    def check(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return evaluate(snapshot)

    async def acheck(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        base = evaluate(snapshot)
        if not self.llm:
            return base
        try:
            result = await asyncio.wait_for(self.llm(snapshot), timeout=self.timeout)
            if isinstance(result, dict) and set(result).issubset({"findings", "observations"}):
                allowed = {_source_id(ref) for ref in _references(snapshot)}
                rows = result.get("findings", result.get("observations", []))
                if isinstance(rows, list) and all(
                    _source_id(item.get("evidence", {})) in allowed
                    for item in rows if isinstance(item, dict)
                ):
                    return result
        except Exception:
            pass
        base["incomplete"] = True
        return base


check_consistency = evaluate


def anonymous_record(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") or {}
    return {
        "sample": hashlib.sha256(str(row.get("request_id", "")).encode()).hexdigest()[:16],
        "status": row.get("status"), "label": row.get("label"), "version": row.get("plugin_version"),
        "findings": [
            {key: item.get(key) for key in ("error_type", "confidence", "severity", "decision_source", "source_quality")}
            for item in (payload.get("findings") or [])
        ],
        "local_ms": payload.get("elapsed_ms"), "response_chars": payload.get("inspected_chars"),
    }
