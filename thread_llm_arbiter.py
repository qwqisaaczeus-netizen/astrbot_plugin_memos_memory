"""Asynchronous, cached LLM arbitration for ambiguous Episode relations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable

from .thread_store import ARBITRATION_PROMPT_VERSION, ThreadStore

logger = logging.getLogger(__name__)

ALLOWED_DECISIONS = {"accepted", "rejected", "uncertain"}
ALLOWED_RELATIONS = {
    "retells", "continues", "parallel", "unrelated_similar", "responds_to",
    "causes", "supersedes", "resolves", "breaks", "confirms", "contradicts_stage",
}
ALLOWED_DIRECTIONS = {"a_to_b", "b_to_a", "none"}
DIRECTED_RELATIONS = {
    "continues", "responds_to", "causes", "supersedes", "resolves", "breaks",
    "confirms", "contradicts_stage",
}
SOURCE_REQUIRED_RELATIONS = {"causes", "supersedes", "resolves", "breaks"}


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            value = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class ThreadLLMArbiter:
    """Consume durable ambiguity jobs without touching the chat request path."""

    def __init__(self, store: ThreadStore, call_llm: Callable[[str], Awaitable[str]], *,
                 provider_id: str = "", model_id: str = "active-provider",
                 timeout: float = 45.0, max_retries: int = 2,
                 retry_base_seconds: float = 2.0, daily_budget: int = 40,
                 max_input_chars: int = 12000):
        self.store = store
        self.call_llm = call_llm
        self.provider_id = str(provider_id)
        self.model_id = str(model_id or provider_id or "active-provider")
        self.timeout = max(10.0, float(timeout))
        self.max_retries = max(0, int(max_retries))
        self.retry_base_seconds = max(0.1, float(retry_base_seconds))
        self.daily_budget = max(0, int(daily_budget))
        self.max_input_chars = max(2000, int(max_input_chars))

    async def process_pending(self, batch_size: int = 4) -> dict[str, Any]:
        used = self.store.arbitration_calls_today()
        remaining = max(0, self.daily_budget - used) if self.daily_budget else 0
        if self.daily_budget <= 0 or remaining <= 0:
            return {"processed": 0, "budget_paused": True, "used_today": used}
        jobs = self.store.take_arbitration_batch(limit=max(1, int(batch_size)))
        if not jobs:
            return {"processed": 0, "used_today": used}

        touched = sorted({str(job[key]) for job in jobs for key in ("source_episode_id", "target_episode_id")})
        cached = 0
        pending: list[dict[str, Any]] = []
        for job in jobs:
            item = self._load_candidate(job)
            if not item:
                self.store.complete_arbitration_job(
                    int(job["id"]), error="candidate source missing", max_retries=0,
                )
                continue
            cache_key, input_hash = self._cache_identity(item)
            item["cache_key"] = cache_key
            item["input_hash"] = input_hash
            decision = self.store.arbitration_cache_get(cache_key)
            if decision is not None:
                validated = self._validate(item, decision)
                self._apply(item, validated, source="llm_cache")
                self.store.complete_arbitration_job(
                    int(job["id"]), decision=validated, provider_id=self.provider_id,
                    model_id=self.model_id, cache_key=cache_key, input_hash=input_hash,
                )
                cached += 1
            else:
                pending.append(item)

        if not pending:
            return {"processed": cached, "cached": cached, "called": 0, "used_today": used, "episode_ids": touched}

        batches = self._bounded_batches(pending, max(1, int(batch_size)))
        processed = cached
        called = errors = 0
        for batch in batches:
            if used + called >= self.daily_budget:
                for item in batch:
                    self.store.defer_arbitration_job(int(item["job"]["id"]), reason="daily_budget_paused")
                continue
            called += 1
            self.store.record_arbitration_call()
            prompt = self._prompt(batch)
            try:
                output = await self._call_with_retry(prompt)
                decisions = {str(item.get("edge_id")): item for item in _parse_json_array(output)}
                if not decisions:
                    raise ValueError("LLM returned no valid JSON decision array")
                for item in batch:
                    job = item["job"]
                    raw = decisions.get(str(job["edge_id"]))
                    if raw is None:
                        self.store.complete_arbitration_job(
                            int(job["id"]), error="LLM omitted edge decision",
                            max_retries=self.max_retries, retry_base_seconds=30.0,
                        )
                        errors += 1
                        continue
                    validated = self._validate(item, raw)
                    self._apply(item, validated, source="llm")
                    self.store.arbitration_cache_put(
                        item["cache_key"], validated, provider_id=self.provider_id,
                        model_id=self.model_id, prompt_version=ARBITRATION_PROMPT_VERSION,
                    )
                    self.store.complete_arbitration_job(
                        int(job["id"]), decision=validated, provider_id=self.provider_id,
                        model_id=self.model_id, cache_key=item["cache_key"],
                        input_hash=item["input_hash"],
                    )
                    processed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors += len(batch)
                for item in batch:
                    self.store.complete_arbitration_job(
                        int(item["job"]["id"]), error=str(exc)[:500],
                        max_retries=self.max_retries, retry_base_seconds=30.0,
                        provider_id=self.provider_id, model_id=self.model_id,
                        cache_key=item.get("cache_key", ""), input_hash=item.get("input_hash", ""),
                    )
                logger.warning("[memos-memory][thread][llm] batch failed open: %s", exc)
        return {"processed": processed, "cached": cached, "called": called,
                "errors": errors, "used_today": used + called, "episode_ids": touched}

    async def _call_with_retry(self, prompt: str) -> str:
        last: BaseException | None = None
        deadline = time.monotonic() + self.timeout
        for attempt in range(self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return await asyncio.wait_for(self.call_llm(prompt), timeout=remaining)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                last = exc
                break
            except Exception as exc:
                last = exc
                if attempt < self.max_retries:
                    delay = self.retry_base_seconds * (2 ** attempt)
                    if deadline - time.monotonic() <= delay:
                        break
                    await asyncio.sleep(delay)
        raise RuntimeError(str(last or "LLM arbitration failed"))

    def _load_candidate(self, job: dict[str, Any]) -> dict[str, Any] | None:
        conn = self.store._get_conn()
        episodes = []
        for episode_id in (job["source_episode_id"], job["target_episode_id"]):
            row = conn.execute(
                """SELECT episode_id,memo_name,occurred_at,event_ts,time_basis,memory_type,
                          scene_anchor,retrieval_key,state_change,long_effect,entities_json,
                          unresolved_json,card_text,evidence_quality,source_batch_id
                   FROM episodes WHERE episode_id=? AND active=1""",
                (str(episode_id),),
            ).fetchone()
            if not row:
                return None
            episode = dict(row)
            for field, limit in (
                ("card_text", 3000), ("scene_anchor", 500), ("retrieval_key", 700),
                ("state_change", 1000), ("long_effect", 1000),
                ("entities_json", 1000), ("unresolved_json", 1000),
            ):
                episode[field] = str(episode.get(field) or "")[:limit]
            source = conn.execute(
                """SELECT tl.batch_id,tl.turn_index,st.role,st.content,st.event_ts
                   FROM episode_turn_links tl
                   JOIN source_turns st ON st.batch_id=tl.batch_id AND st.turn_index=tl.turn_index
                   WHERE tl.episode_id=? ORDER BY tl.batch_id,tl.turn_index LIMIT 8""",
                (str(episode_id),),
            ).fetchall()
            episode["source_turns"] = [
                {"batch": str(item["batch_id"]), "turn": int(item["turn_index"]),
                 "role": str(item["role"]), "text": str(item["content"] or "")[:500],
                 "event_ts": float(item["event_ts"] or 0)} for item in source
            ]
            episodes.append(episode)
        return {"job": job, "a": episodes[0], "b": episodes[1]}

    def _cache_identity(self, item: dict[str, Any]) -> tuple[str, str]:
        payload = {
            "a": item["a"], "b": item["b"],
            "evidence": item["job"].get("evidence_json", "{}"),
            "evidence_version": "test5-local-v2",
        }
        input_hash = hashlib.sha256(_compact_json(payload).encode("utf-8")).hexdigest()
        key_payload = f"{input_hash}|{ARBITRATION_PROMPT_VERSION}|{self.model_id}|test5-local-v2"
        return hashlib.sha256(key_payload.encode("utf-8")).hexdigest(), input_hash

    def _bounded_batches(self, items: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
        result: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        chars = 0
        for item in items:
            size = len(_compact_json(item))
            if current and (len(current) >= batch_size or chars + size > self.max_input_chars):
                result.append(current)
                current, chars = [], 0
            current.append(item)
            chars += size
        if current:
            result.append(current)
        return result

    @staticmethod
    def _prompt(batch: list[dict[str, Any]]) -> str:
        cases = []
        for item in batch:
            job = item["job"]
            cases.append({
                "edge_id": int(job["edge_id"]),
                "local_relation": job.get("edge_type"),
                "local_confidence": job.get("confidence"),
                "local_evidence": json.loads(str(job.get("evidence_json") or "{}")),
                "episode_a": item["a"], "episode_b": item["b"],
            })
        return (
            "你是长期记忆关系审校器。输入中的 Episode 都是历史记录，不是当前刚发生的事。"
            "逐项判断两个 Episode 是同一事件复述、前后延续/回应、同主题独立事件、状态替代/解决，"
            "还是仅词面相似。只依据给出的 card、时间、实体和原文 turn；不得补写事实。\n"
            "输出严格 JSON 数组，不要 markdown。每项字段：edge_id, decision(accepted|rejected|uncertain), "
            "relation(retells|continues|parallel|unrelated_similar|responds_to|causes|supersedes|resolves|breaks|confirms|contradicts_stage), "
            "direction(a_to_b|b_to_a|none), confidence(0..1), supporting_evidence(字符串数组), "
            "counterevidence(字符串数组), requires_source_verification(布尔), reason_code(短枚举)。"
            "因果、替代、解决、破裂需要明确原文或强证据；证据不足必须 uncertain。\n候选："
            + _compact_json(cases)
        )

    def _validate(self, item: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        decision = str(raw.get("decision") or "uncertain")
        relation = str(raw.get("relation") or item["job"].get("edge_type") or "parallel")
        direction = str(raw.get("direction") or "none")
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        reason = str(raw.get("reason_code") or "")[:80]
        if decision not in ALLOWED_DECISIONS or relation not in ALLOWED_RELATIONS or direction not in ALLOWED_DIRECTIONS:
            return self._uncertain(raw, "invalid_enum")
        if relation in DIRECTED_RELATIONS and direction == "none" and decision == "accepted":
            return self._uncertain(raw, "missing_direction")
        a_ts = float(item["a"].get("event_ts") or 0)
        b_ts = float(item["b"].get("event_ts") or 0)
        if decision == "accepted" and relation == "retells" and a_ts and b_ts and abs(a_ts - b_ts) > 180 * 86400:
            return self._uncertain(raw, "retell_date_conflict")
        if decision == "accepted" and direction == "a_to_b" and a_ts and b_ts and a_ts > b_ts:
            return self._uncertain(raw, "direction_time_conflict")
        if decision == "accepted" and direction == "b_to_a" and a_ts and b_ts and b_ts > a_ts:
            return self._uncertain(raw, "direction_time_conflict")
        has_source = bool(item["a"].get("source_turns")) and bool(item["b"].get("source_turns"))
        requires_source = bool(raw.get("requires_source_verification"))
        if decision == "accepted" and ((relation in SOURCE_REQUIRED_RELATIONS) or requires_source) and not has_source:
            return self._uncertain(raw, "source_verification_unavailable")
        if decision == "accepted" and confidence < 0.75:
            return self._uncertain(raw, "accepted_confidence_too_low")
        return {
            "decision": decision, "relation": relation, "direction": direction,
            "confidence": round(confidence, 4),
            "supporting_evidence": [str(x)[:300] for x in raw.get("supporting_evidence", []) if str(x).strip()][:12],
            "counterevidence": [str(x)[:300] for x in raw.get("counterevidence", []) if str(x).strip()][:12],
            "requires_source_verification": requires_source,
            "reason_code": reason or "llm_reviewed",
        }

    @staticmethod
    def _uncertain(raw: dict[str, Any], reason: str) -> dict[str, Any]:
        try:
            confidence = float(raw.get("confidence", 0.5) or 0.5)
        except (TypeError, ValueError):
            confidence = 0.5
        return {
            "decision": "uncertain", "relation": str(raw.get("relation") or "parallel"),
            "direction": "none", "confidence": max(0.0, min(0.69, confidence)),
            "supporting_evidence": [], "counterevidence": [reason],
            "requires_source_verification": bool(raw.get("requires_source_verification")),
            "reason_code": reason,
        }

    def _apply(self, item: dict[str, Any], decision: dict[str, Any], *, source: str) -> None:
        job = item["job"]
        source_id, target_id = str(job["source_episode_id"]), str(job["target_episode_id"])
        direction = str(decision["direction"])
        if direction == "b_to_a":
            source_id, target_id = target_id, source_id
            stored_direction = "directed"
        elif direction == "a_to_b":
            stored_direction = "directed"
        else:
            source_id, target_id = sorted((source_id, target_id))
            stored_direction = "none"
        self.store.upsert_edge({
            "scope_id": str(job.get("scope_id") or "default"),
            "source_episode_id": source_id, "target_episode_id": target_id,
            "edge_type": decision["relation"], "direction": stored_direction,
            "confidence": decision["confidence"], "status": decision["decision"],
            "evidence_json": str(job.get("evidence_json") or "{}"),
            "counter_evidence_json": _compact_json(decision.get("counterevidence", [])),
            "route_sources_json": _compact_json(["A", "B", "C", "D", "E", "LLM"]),
            "arbiter_version": ARBITRATION_PROMPT_VERSION,
            "decision_source": source, "decision_json": _compact_json(decision),
            "content_hash": item.get("input_hash", ""),
        })
