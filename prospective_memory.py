"""Prospective memory derived from current claims for 6.0-test4 Shadow use."""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import time
import uuid
from typing import Any

from .store_utils import extract_terms
from .temporal import timezone_or_default
from .text_overlap import longest_common_substring
from .thread_store import ThreadStore


ENGINE_VERSION = "test6-prospective-v2"
_RESOLVED = ("完成", "做到了", "兑现", "解决", "结束", "取消", "作废", "放弃", "不再")
_EXPLICIT_DATE = re.compile(r"(?:(20\d{2})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?")
_GENERIC = {
    "答应", "承诺", "约定", "计划", "打算", "准备", "一起", "下次", "以后", "然后", "事情",
    "今天", "今晚", "明天", "后天", "时候", "当时", "对方", "表达", "说过",
}
_ACTION_CUES = (
    "去", "来", "做", "写", "教", "带", "买", "看", "等", "回", "帮", "梳",
    "揉", "吃", "见", "告诉", "学习", "照顾", "联系", "完成", "不乱跑",
)
_META_ONLY = (
    "长期承诺", "日常约定", "共同期待", "自然提出约定", "具体承诺",
    "契约和情感", "达成关系", "身份确认", "承诺来反复确认",
)
_QUERY_NOISE = (
    "我们还有什么没完成", "还有什么没完成", "现在", "关于", "这件事", "那个",
    "我们", "你们", "他们", "事情", "怎么", "什么", "是否", "还成立吗",
)


def _norm(text: Any, limit: int = 500) -> str:
    return " ".join(str(text or "").split()).strip()[:limit]


class ProspectiveMemory:
    def __init__(self, store: ThreadStore, timezone_name: str = "Asia/Shanghai"):
        self.store = store
        self.timezone_name = str(timezone_name or "Asia/Shanghai")

    def _due_window(self, text: str, event_ts: float) -> tuple[float, float, str]:
        tz = timezone_or_default(self.timezone_name)
        effective_ts = float(event_ts) if float(event_ts or 0) > 0 else time.time()
        base = dt.datetime.fromtimestamp(effective_ts, tz)
        for marker, days in (("后天", 2), ("明天", 1), ("今晚", 0), ("今天", 0), ("下周", 7)):
            if marker in text and not re.search(rf"(?:不是|并非).{{0,4}}{re.escape(marker)}", text):
                start = (base + dt.timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
                span = 7 if marker == "下周" else 1
                return start.timestamp(), (start + dt.timedelta(days=span)).timestamp(), marker
        if "下个月" in text and not re.search(r"(?:不是|并非).{0,4}下个月", text):
            if base.month == 12:
                next_month = base.replace(
                    year=base.year + 1, month=1, day=1,
                    hour=0, minute=0, second=0, microsecond=0,
                )
            else:
                next_month = base.replace(
                    month=base.month + 1, day=1,
                    hour=0, minute=0, second=0, microsecond=0,
                )
            if next_month.month == 12:
                month_after = next_month.replace(
                    year=next_month.year + 1, month=1,
                )
            else:
                month_after = next_month.replace(month=next_month.month + 1)
            return next_month.timestamp(), month_after.timestamp(), "下个月"
        match = _EXPLICIT_DATE.search(text)
        if match:
            year = int(match.group(1) or base.year)
            try:
                value = base.replace(year=year, month=int(match.group(2)), day=int(match.group(3)),
                                     hour=0, minute=0, second=0, microsecond=0)
                if not match.group(1) and value.timestamp() < effective_ts - 86400:
                    value = value.replace(year=year + 1)
                return value.timestamp(), (value + dt.timedelta(days=1)).timestamp(), "explicit_date"
            except ValueError:
                pass
        return 0.0, 0.0, "semantic_only"

    @staticmethod
    def _actionable(claim: dict[str, Any], description: str, due_start: float) -> bool:
        claim_type = str(claim.get("claim_type") or "")
        field = str((claim.get("evidence") or {}).get("field") or "")
        if field == "long_effect":
            return False
        if any(marker in description for marker in _META_ONLY):
            return False
        if claim_type == "unresolved":
            return True
        if claim_type == "boundary":
            return any(marker in description for marker in ("不许", "不要", "不能", "别叫", "边界"))
        has_action = any(marker in description for marker in _ACTION_CUES)
        has_commitment = any(marker in description for marker in (
            "答应", "承诺", "约定", "保证", "说好", "计划", "打算", "要", "欠",
        ))
        return has_action and (bool(due_start) or has_commitment)

    @staticmethod
    def _entities_and_terms(claim: dict[str, Any]) -> tuple[list[str], list[str]]:
        entities = []
        evidence = claim.get("evidence") or {}
        for ref in evidence.get("entities") or []:
            value = _norm(ref, 24)
            if value and value not in entities:
                entities.append(value)
        subject = _norm(claim.get("subject"), 24)
        if subject and subject not in {"default", "relationship"} and subject not in entities:
            entities.append(subject)
        description = str(claim.get("object") or "")
        terms = []
        for cue in _ACTION_CUES:
            start = 0
            while True:
                index = description.find(cue, start)
                if index < 0:
                    break
                phrase = _norm(description[index:index + min(6, len(cue) + 2)], 12)
                phrase = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9_-]", "", phrase)
                if len(phrase) >= 2 and phrase not in _GENERIC and phrase not in terms:
                    terms.append(phrase)
                start = index + max(1, len(cue))
        raw_terms = []
        for term in extract_terms(description):
            term = _norm(term, 24)
            term = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9_-]", "", term)
            if len(term) >= 2 and term not in _GENERIC and term not in raw_terms:
                raw_terms.append(term)
        raw_terms.sort(key=lambda value: (
            -int(any(cue in value for cue in _ACTION_CUES)),
            -min(len(value), 12),
            description.find(value) if value in description else 10**6,
            value,
        ))
        for term in raw_terms:
            if term not in terms:
                terms.append(term)
        return entities[:8], terms[:16]

    def rebuild_scope(self, scope_id: str) -> dict[str, Any]:
        claims = self.store.list_claims(scope_id=scope_id, limit=100000)
        items: list[dict[str, Any]] = []
        conn = self.store._get_conn()
        for claim in claims:
            claim_type = str(claim.get("claim_type") or "")
            if claim_type not in {"commitment", "plan", "unresolved", "boundary"}:
                continue
            if str(claim.get("status")) in {"resolved", "broken", "superseded", "historical", "rejected"}:
                continue
            if (str(claim.get("status")) == "uncertain"
                    and float(claim.get("explicitness") or 0) < 0.88):
                continue
            description = _norm(claim.get("object"), 500)
            if not description or any(cue in description for cue in _RESOLVED):
                continue
            due_start, due_end, date_reason = self._due_window(description, float(claim.get("valid_from") or 0))
            if not self._actionable(claim, description, due_start):
                continue
            entities, terms = self._entities_and_terms(claim)
            source_episode_id = str(claim.get("source_episode_id") or "")
            thread_row = conn.execute(
                """SELECT thread_id FROM memory_thread_members
                   WHERE episode_id=? ORDER BY membership_confidence DESC,thread_id LIMIT 1""",
                (source_episode_id,),
            ).fetchone()
            source_thread_id = str(thread_row["thread_id"]) if thread_row else ""
            explicitness = float(claim.get("explicitness") or 0.5)
            salience = min(1.0, 0.45 + explicitness * 0.35 + (0.15 if due_start else 0.0))
            status = "uncertain" if str(claim.get("status")) == "uncertain" else "pending"
            item_id = "pro_" + hashlib.sha256(str(claim["claim_id"]).encode("utf-8")).hexdigest()[:24]
            items.append({
                "item_id": item_id,
                "scope_id": str(scope_id),
                "source_episode_id": source_episode_id,
                "source_thread_id": source_thread_id,
                "source_claim_id": str(claim["claim_id"]),
                "item_type": claim_type,
                "description": description,
                "trigger_mode": "multi_route",
                "due_start": due_start,
                "due_end": due_end,
                "status": status,
                "salience": salience,
                "explicitness": explicitness,
                "emotional_weight": 0.25 if claim_type in {"boundary", "unresolved"} else 0.1,
                "target_entities": entities,
                "trigger_terms": terms,
                "status_reason": date_reason,
                "content_hash": hashlib.sha256(description.encode("utf-8")).hexdigest(),
            })
        written = self.store.upsert_prospective_items(items)
        lifecycle = self.store.refresh_prospective_lifecycle(scope_id, now_ts=time.time())
        pruned = self.store.prune_auto_prospective(
            scope_id, {str(item["item_id"]) for item in items}
        )
        return {"claims_scanned": len(claims), "items": len(items), "written": written,
                "pruned": pruned, "lifecycle": lifecycle, "engine_version": ENGINE_VERSION}

    @staticmethod
    def _lexical_score(query: str, terms: list[str]) -> float:
        if not query or not terms:
            return 0.0
        matched = [term for term in terms if term and term in query]
        if not matched:
            query_terms = set(extract_terms(query, initialize_segmenter=False))
            matched = [term for term in terms if term in query_terms]
        base = min(1.0, len(matched) / max(2.0, math.sqrt(len(terms))))
        action_exact = any(
            len(term) >= 2 and term not in _GENERIC
            and any(cue in term for cue in _ACTION_CUES)
            for term in matched
        )
        return max(base, 0.6 if action_exact else 0.0)

    @staticmethod
    def _text_overlap_score(query: str, description: str) -> float:
        def clean(value: str) -> str:
            for noise in _QUERY_NOISE:
                value = value.replace(noise, " ")
            for noise in _GENERIC:
                value = value.replace(noise, " ")
            return "".join(re.findall(r"[\u4e00-\u9fffa-z0-9_-]", value.lower()))

        query_clean = clean(query)
        description_clean = clean(description)
        if len(query_clean) < 4 or len(description_clean) < 4:
            return 0.0
        longest = longest_common_substring(query_clean, description_clean)
        contiguous = longest / max(8.0, min(24.0, len(query_clean))) if longest >= 3 else 0.0
        query_grams = {query_clean[index:index + 2] for index in range(len(query_clean) - 1)}
        description_grams = {
            description_clean[index:index + 2] for index in range(len(description_clean) - 1)
        }
        shared = query_grams & description_grams
        distributed = (
            min(1.0, len(shared) / max(2.0, math.sqrt(len(query_grams))))
            if len(shared) >= 2 else 0.0
        )
        return min(1.0, max(contiguous, distributed))

    @staticmethod
    def _temporal_score(item: dict[str, Any], now_ts: float) -> tuple[float, str]:
        start = float(item.get("due_start") or 0)
        end = float(item.get("due_end") or 0)
        if not start:
            return 0.0, "no_due_date"
        if start <= now_ts <= max(start, end):
            return 1.0, "due"
        days = (start - now_ts) / 86400.0
        if 0 < days <= 1:
            return 0.88, "within_24h"
        if 1 < days <= 7:
            return 0.58, "within_7d"
        overdue = (now_ts - max(start, end)) / 86400.0
        if 0 < overdue <= 3:
            return 0.72, "recently_overdue"
        if 3 < overdue <= 30:
            return 0.38, "long_overdue"
        return 0.0, "outside_window"

    def trigger(self, scope_id: str, query: str, *, context_text: str = "",
                emotion_signal: float | None = None, now_ts: float | None = None,
                request_id: str = "", record: bool = True) -> dict[str, Any]:
        try:
            emotion_signal = float(emotion_signal) if emotion_signal is not None else 0.0
        except (TypeError, ValueError):
            emotion_signal = 0.0
        emotion_signal = max(0.0, min(1.0, emotion_signal)) if math.isfinite(emotion_signal) else 0.0
        now_ts = float(now_ts or time.time())
        query = _norm(query, 1000)
        context_text = _norm(context_text, 1200)
        self.store.refresh_prospective_lifecycle(scope_id, now_ts=now_ts)
        candidates = self.store.list_prospective(scope_id=scope_id, limit=1000)
        evaluated: list[dict[str, Any]] = []
        for item in candidates:
            status = str(item.get("status") or "")
            if status in {"resolved", "expired"}:
                continue
            if status == "snoozed" and float(item.get("cooldown_until") or 0) > now_ts:
                continue
            if float(item.get("cooldown_until") or 0) > now_ts:
                continue
            terms = list(item.get("trigger_terms") or [])
            entities = list(item.get("target_entities") or [])
            temporal, time_reason = self._temporal_score(item, now_ts)
            description = _norm(item.get("description"), 500)
            semantic = max(
                self._lexical_score(query, terms),
                self._text_overlap_score(query, description),
            )
            exact_cue = 1.0 if len(description) >= 12 and description[:32] in query else 0.0
            entity = max((1.0 if value in query else 0.0 for value in entities), default=0.0)
            context = max(self._lexical_score(context_text, terms),
                          max((0.8 if value in context_text else 0.0 for value in entities), default=0.0))
            emotion = max(0.0, min(1.0, float(emotion_signal))) if float(item.get("emotional_weight") or 0) >= 0.2 else 0.0
            routes = {
                "temporal": round(temporal, 4), "semantic": round(semantic, 4),
                "entity": round(entity, 4), "context": round(context, 4),
                "emotion": round(emotion, 4), "exact_cue": exact_cue,
                "time_reason": time_reason,
            }
            factual_routes = sum(value >= 0.35 for value in (temporal, semantic, entity, context))
            relevant = max(semantic, entity, context)
            high_date_exception = temporal >= 0.95 and float(item.get("salience") or 0) >= 0.85
            passed = bool(
                (factual_routes >= 2 and relevant >= 0.25)
                or (emotion >= 0.55 and factual_routes >= 1 and relevant >= 0.25)
                or high_date_exception
            )
            score = (
                temporal * 0.25 + semantic * 0.30 + entity * 0.18 + context * 0.12
                + emotion * 0.05 + float(item.get("salience") or 0) * 0.06
                + float(item.get("explicitness") or 0) * 0.04 + exact_cue * 0.12
            )
            if status == "uncertain":
                score *= 0.55
                passed = False
            evaluated.append({**item, "routes": routes, "score": round(score, 4),
                              "passed": passed, "decision_reason": "multi_route" if passed else "hard_gate"})
        evaluated.sort(key=lambda item: (-int(item["passed"]), -float(item["score"]),
                                         -float(item.get("salience") or 0), str(item["item_id"])))
        selected = next((item for item in evaluated if item["passed"]), None)
        rid = str(request_id or "pros_" + uuid.uuid4().hex[:20])
        if record:
            if selected:
                self.store.record_prospective_observation({
                    "request_id": rid, "scope_id": scope_id, "query_text": query,
                    "item_id": selected["item_id"], "decision": "would_surface",
                    "score": selected["score"], "routes": selected["routes"],
                    "reason": selected["decision_reason"], "shadow": True,
                })
            else:
                self.store.record_prospective_observation({
                    "request_id": rid, "scope_id": scope_id, "query_text": query,
                    "decision": "not_triggered", "score": 0, "routes": {},
                    "reason": "no_candidate_passed", "shadow": True,
                })
        return {"request_id": rid, "selected": selected, "candidates": evaluated[:20],
                "candidate_count": len(evaluated), "shadow": True,
                "emotion_signal": emotion_signal, "engine_version": ENGINE_VERSION}
