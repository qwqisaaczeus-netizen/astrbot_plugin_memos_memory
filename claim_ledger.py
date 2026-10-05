"""Evidence-aware Claim Ledger for the 6.0 Shadow memory pipeline.

The ledger is derived from Episodes. It never rewrites diaries, source turns,
Episode cards, semantic state, profile, or any other source record.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from typing import Any

from .store_utils import extract_terms
from .thread_store import ThreadStore


EXTRACTOR_VERSION = "test6-claim-v3"

_CLAIM_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("boundary", ("边界", "禁区", "不许", "不要叫", "不能叫", "别叫", "不接受", "不可以")),
    ("commitment", ("答应", "承诺", "约定", "保证", "说好了", "发誓")),
    ("plan", ("计划", "打算", "准备", "下次", "明天", "后天", "下周", "下个月", "一起去", "等你")),
    ("relationship", ("关系", "信任", "亲近", "疏远", "和好", "分手", "离开", "重逢", "依赖", "陌生")),
    ("preference", ("喜欢", "讨厌", "害怕", "偏爱", "习惯", "不喜欢", "在意")),
    ("identity", ("名字是", "叫做", "身份是", "真实身份", "昵称", "称呼")),
)
_TERMINAL_CUES = ("完成", "做到了", "兑现", "解决", "结束", "取消", "作废", "放弃", "拒绝", "不再")
_BROKEN_CUES = ("食言", "违背", "打破", "没做到", "没有做到", "爽约")
_HYPOTHETICAL_CUES = ("如果", "假如", "也许", "可能", "或许", "要是", "仿佛")
_DREAM_CUES = ("梦里", "梦见", "做梦", "梦境")
_REPORTING_CUES = ("他说", "她说", "对我说", "听他说", "听她说", "转述", "据说")
_QUOTE_RE = re.compile(r"[\"'“”‘’「」『』]")
_SLOT_STOP = {
    "我", "我们", "你", "你们", "他", "她", "他们", "她们", "自己", "彼此",
    "答应", "承诺", "约定", "保证", "说好", "说好了", "发誓", "计划", "打算",
    "准备", "以后", "未来", "下次", "今天", "明天", "后天", "今晚", "昨日",
    "后来", "现在", "已经", "还是", "仍然", "一起", "没有", "不要", "不能",
    "关系", "身份", "名字", "昵称", "称呼", "边界", "喜欢", "讨厌", "害怕",
    "变得", "更加", "开始", "继续", "事情", "一个", "这个", "那个", "可能",
}
_TRANSITION_STOP = {
    *_TERMINAL_CUES, *_BROKEN_CUES,
    "取消了", "完成了", "做到了", "解决了", "放弃了", "拒绝了", "违背了",
    "后来", "终于", "已经", "仍然", "依旧", "改口", "撤回", "恢复",
}
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n+")
_QUALITY_WEIGHT = {
    "source_exact": 1.0,
    "source_grounded": 0.96,
    "source_supported": 0.88,
    "batch_supported": 0.82,
    "diary_derived": 0.62,
    "legacy": 0.55,
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value)) if value else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _norm(text: Any, limit: int = 220) -> str:
    return " ".join(str(text or "").split()).strip()[:limit]


class ClaimLedger:
    """Builds atomic claims, transitions, current slots, and thread views."""

    def __init__(self, store: ThreadStore):
        self.store = store

    @staticmethod
    def _claim_type(text: str, *, unresolved: bool = False) -> str:
        if unresolved:
            return "unresolved"
        for claim_type, cues in _CLAIM_CUES:
            if any(cue in text for cue in cues):
                return claim_type
        return ""

    @staticmethod
    def _slot_key(claim_type: str, text: str, entities: list[str]) -> str:
        if claim_type == "relationship":
            anchor = "|".join(sorted(entities[:2])) or "primary"
            return f"relationship:{anchor}"
        if claim_type == "identity":
            anchor = entities[0] if entities else "primary"
            if any(cue in text for cue in ("名字", "昵称", "称呼", "叫做")):
                facet = "name"
            elif any(cue in text for cue in ("身份", "真实身份", "是谁")):
                facet = "role"
            else:
                facet = "general"
            return f"identity:{anchor}:{facet}"
        # `extract_terms` is a set. Re-sort by source position so a slot key is
        # stable across Python processes, then prefer concise content anchors.
        # Entity names identify scope/subject; allowing them to consume every
        # anchor would collapse unrelated promises into one giant slot.
        entity_set = {value for value in entities if value}
        anchor_text = text
        strip_values = set(_SLOT_STOP) | _TRANSITION_STOP | entity_set
        for _, cues in _CLAIM_CUES:
            strip_values.update(cues)
        for value in sorted(strip_values, key=len, reverse=True):
            if value:
                anchor_text = anchor_text.replace(value, " ")
        anchor_text = re.sub(r"[的了着过地得]", " ", anchor_text)
        candidates = []
        for term in extract_terms(anchor_text):
            value = _norm(term, 24)
            if not 2 <= len(value) <= 6 or value in _SLOT_STOP or value in entity_set:
                continue
            if any(value == cue for _, cues in _CLAIM_CUES for cue in cues):
                continue
            candidates.append(value)
        terms = sorted(set(candidates), key=lambda value: (
            anchor_text.find(value) if value in anchor_text else 10**6, len(value), value
        ))
        anchors: list[str] = []
        for value in terms:
            value = _norm(value, 20)
            if value and value not in anchors and not any(
                value in existing or existing in value for existing in anchors
            ):
                anchors.append(value)
            if len(anchors) >= 4:
                break
        digest_source = "|".join(anchors) or re.sub(r"\W+", "", text)[:32] or "general"
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]
        return f"{claim_type}:{digest}"

    @staticmethod
    def _reported_flags(text: str, field: str) -> tuple[bool, bool]:
        quoted = bool(_QUOTE_RE.search(text))
        reported = any(cue in text for cue in _REPORTING_CUES)
        # Structured Episode fields are already the memory producer's factual
        # interpretation. Only downgrade attributed quotations found through
        # the literary card fallback; ordinary roleplay dialogue remains valid.
        return reported, bool(field == "card_text" and quoted and reported)

    @staticmethod
    def _quality(episode: dict[str, Any], field: str) -> tuple[str, float]:
        value = str(episode.get("evidence_quality") or "diary_derived")
        weight = _QUALITY_WEIGHT.get(value, 0.58)
        # The extractor reads Episode fields, not verbatim source turns. Exact
        # links support the derived statement but do not make its wording an
        # exact quote. Keep that distinction visible in the ledger.
        if episode.get("_grounded_turns"):
            value = "source_supported"
            weight = 0.90 if field in {"state_change", "unresolved"} else 0.86
        elif episode.get("source_batch_id") and weight < 0.82:
            value = "batch_supported"
            weight = 0.82
        return value, weight

    def _episode_rows(self, scope_id: str, episode_ids: list[str] | None = None) -> list[dict[str, Any]]:
        conn = self.store._get_conn()
        params: list[Any] = [str(scope_id)]
        clause = ""
        if episode_ids:
            ids = list(dict.fromkeys(str(item) for item in episode_ids if str(item)))
            clause = f" AND e.episode_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        rows = conn.execute(
            f"""SELECT e.* FROM episodes e
                 JOIN thread_episode_scopes s ON s.episode_id=e.episode_id
                 WHERE s.scope_id=? AND e.active=1 {clause}
                 ORDER BY e.event_ts,e.created_ts,e.episode_id""",
            tuple(params),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["entities"] = _loads(item.get("entities_json"), [])
            item["unresolved"] = _loads(item.get("unresolved_json"), [])
            grounded = conn.execute(
                """SELECT tl.batch_id,tl.turn_index FROM episode_turn_links tl
                   JOIN source_turns st ON st.batch_id=tl.batch_id AND st.turn_index=tl.turn_index
                   WHERE tl.episode_id=? ORDER BY tl.batch_id,tl.turn_index LIMIT 24""",
                (str(item["episode_id"]),),
            ).fetchall()
            item["_grounded_turns"] = [
                {"batch_id": str(value["batch_id"]), "turn_index": int(value["turn_index"])}
                for value in grounded
            ]
            output.append(item)
        return output

    def extract_episode(self, scope_id: str, episode: dict[str, Any]) -> list[dict[str, Any]]:
        sources: list[tuple[str, str, float, bool]] = []
        for field, weight in (("state_change", 0.94), ("long_effect", 0.72), ("retrieval_key", 0.62)):
            value = _norm(episode.get(field), 700)
            if value:
                sources.append((field, value, weight, False))
        for value in episode.get("unresolved") or []:
            value = _norm(value, 360)
            if value:
                sources.append(("unresolved", value, 0.9, True))
        # Card text is a fallback only. A sentence still needs an explicit cue.
        card = _norm(episode.get("card_text"), 2400)
        if card:
            sources.append(("card_text", card, 0.52, False))

        entities = [_norm(item, 24) for item in (episode.get("entities") or []) if _norm(item, 24)]
        candidates: list[dict[str, Any]] = []
        for field, content, field_weight, unresolved in sources:
            sentences = [part.strip(" ，。！？!?；;") for part in _SENTENCE_SPLIT.split(content)]
            for sentence in sentences:
                sentence = _norm(sentence, 320)
                if len(sentence) < 4:
                    continue
                # Card text is only a fallback. Long unpunctuated cards often
                # concatenate title, entities, state and trigger fields; using
                # them as one claim creates a second summary instead of an
                # atomic fact.
                if field == "card_text" and len(sentence) > 180:
                    continue
                claim_type = self._claim_type(sentence, unresolved=unresolved)
                if not claim_type:
                    continue
                slot_key = self._slot_key(claim_type, sentence, entities)
                fingerprint = hashlib.sha256(re.sub(r"\s+", "", sentence).encode("utf-8")).hexdigest()[:20]
                dream = any(cue in sentence for cue in _DREAM_CUES)
                hypothetical = any(cue in sentence for cue in _HYPOTHETICAL_CUES)
                reported, quoted_report = self._reported_flags(sentence, field)
                explicitness = min(1.0, field_weight + (0.06 if claim_type in {"boundary", "commitment"} else 0.0))
                source_quality, quality_weight = self._quality(episode, field)
                confidence = min(0.99, quality_weight * 0.62 + explicitness * 0.38)
                # A diary-only statement is useful as a candidate but is not a
                # safe current fact by itself. Promotion requires source
                # support, later multi-source confirmation, or a manual lock.
                status = "uncertain" if dream or hypothetical or quoted_report or quality_weight < 0.75 else "active"
                source_refs = list(episode.get("_grounded_turns") or [])
                claim_id = "clm_" + hashlib.sha256(
                    f"{episode['episode_id']}|{slot_key}|{fingerprint}".encode("utf-8")
                ).hexdigest()[:24]
                candidates.append({
                    "claim_id": claim_id,
                    "scope_id": str(scope_id),
                    "slot_key": slot_key,
                    "subject": entities[0] if entities else str(scope_id),
                    "predicate": claim_type,
                    "object": sentence,
                    "claim_type": claim_type,
                    "valid_from": float(episode.get("event_ts") or 0),
                    "valid_to": 0.0,
                    "status": status,
                    "confidence": confidence,
                    "explicitness": explicitness,
                    "source_episode_id": str(episode["episode_id"]),
                    "source_turn_refs": source_refs,
                    "source_quality": source_quality,
                    "is_hypothetical": hypothetical,
                    "is_dream": dream,
                    "evidence": {
                        "field": field,
                        "text": sentence,
                        "grounded_turns": source_refs,
                        "entities": entities,
                        "is_reported": reported,
                        "is_quoted_report": quoted_report,
                    },
                    "content_hash": fingerprint,
                    "extractor_version": EXTRACTOR_VERSION,
                    "decision_source": "deterministic_extractor",
                    "_field_weight": field_weight,
                    "_field": field,
                })
        # One semantic slot inside one Episode should yield one atomic claim.
        # Structured fields outrank the literary card fallback; concise wording
        # wins ties. Different slots remain separate even when their type is the
        # same, so one scene may retain multiple real promises or boundaries.
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in candidates:
            by_type[str(claim["claim_type"])].append(claim)
        output: list[dict[str, Any]] = []
        for claim_type, typed in by_type.items():
            structured = [claim for claim in typed if claim.get("_field") != "card_text"]
            pool = structured or typed
            best: dict[str, tuple[tuple[float, int], dict[str, Any]]] = {}
            for claim in pool:
                rank = (float(claim.get("_field_weight") or 0), -len(str(claim.get("object") or "")))
                slot_key = str(claim["slot_key"])
                if slot_key not in best or rank > best[slot_key][0]:
                    best[slot_key] = (rank, claim)
            cap = 2 if claim_type in {"commitment", "boundary", "plan", "unresolved"} else 1
            selected = sorted(best.values(), key=lambda value: value[0], reverse=True)[:cap]
            for _, claim in selected:
                claim.pop("_field_weight", None)
                claim.pop("_field", None)
                output.append(claim)
        return output

    def rebuild_scope(self, scope_id: str, episode_ids: list[str] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        episodes = self._episode_rows(scope_id, episode_ids)
        extracted: list[dict[str, Any]] = []
        for episode in episodes:
            extracted.extend(self.extract_episode(scope_id, episode))
        written = self.store.upsert_claims(extracted)
        if episode_ids is None:
            self.store.prune_auto_claims(
                scope_id, {str(item["claim_id"]) for item in extracted}
            )
        reconciliation = self.reconcile_scope(scope_id)
        views = self.materialize_thread_views(scope_id)
        return {
            "episodes": len(episodes),
            "extracted": len(extracted),
            "written": written,
            "slots": reconciliation["slots"],
            "transitions": reconciliation["transitions"],
            "views": views,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "extractor_version": EXTRACTOR_VERSION,
        }

    @staticmethod
    def _terminal_type(text: str) -> str:
        if any(cue in text for cue in _BROKEN_CUES):
            return "broken"
        if any(cue in text for cue in _TERMINAL_CUES):
            return "resolved"
        return ""

    def reconcile_scope(self, scope_id: str) -> dict[str, int]:
        claims = self.store.list_claims(scope_id=scope_id, limit=100000)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in claims:
            grouped[str(claim.get("slot_key") or claim["claim_id"])].append(claim)
        transition_count = 0
        for slot_key, items in grouped.items():
            items.sort(key=lambda item: (
                float(item.get("valid_from") or 0), float(item.get("created_ts") or 0), str(item["claim_id"])
            ))
            locked_active = [
                item for item in items
                if int(item.get("manual_lock") or 0) and str(item.get("status")) == "active"
            ]
            eligible = [
                item for item in items
                if not int(item.get("manual_lock") or 0)
                and str(item.get("status")) not in {
                    "uncertain", "rejected", "resolved", "broken", "historical", "superseded"
                }
            ]
            confirmations: dict[str, int] = defaultdict(int)
            for item in eligible:
                confirmations[str(item.get("content_hash") or item.get("object") or "")] += 1

            def evidence_rank(item: dict[str, Any]) -> tuple[float, float, str]:
                quality = _QUALITY_WEIGHT.get(str(item.get("source_quality") or ""), 0.55)
                terminal_bonus = 0.10 if self._terminal_type(str(item.get("object") or "")) else 0.0
                confirmation_bonus = min(
                    0.12,
                    max(0, confirmations.get(str(item.get("content_hash") or item.get("object") or ""), 1) - 1) * 0.04,
                )
                score = (
                    quality * 0.40
                    + float(item.get("confidence") or 0.0) * 0.28
                    + float(item.get("explicitness") or 0.0) * 0.20
                    + terminal_bonus
                    + confirmation_bonus
                )
                return score, float(item.get("valid_from") or 0), str(item.get("claim_id") or "")

            selected = (
                locked_active[-1]
                if locked_active
                else (max(eligible, key=evidence_rank) if eligible else items[-1])
            )
            terminal = self._terminal_type(str(selected.get("object") or ""))
            current_ids: list[str] = []
            conflict_count = 0
            for item in items:
                if int(item.get("manual_lock") or 0):
                    if str(item.get("status")) == "active":
                        current_ids.append(str(item["claim_id"]))
                    continue
                old_status = str(item.get("status") or "uncertain")
                new_status = old_status
                transition_type = ""
                if old_status == "uncertain":
                    new_status = "uncertain"
                elif item["claim_id"] == selected["claim_id"]:
                    new_status = "historical" if terminal else "active"
                    if not terminal:
                        current_ids.append(str(item["claim_id"]))
                elif terminal and str(item.get("claim_type")) in {"commitment", "plan", "unresolved"}:
                    new_status = terminal
                    transition_type = terminal
                elif str(item.get("object") or "") == str(selected.get("object") or ""):
                    new_status = "historical"
                    transition_type = "confirms"
                else:
                    new_status = "superseded"
                    transition_type = "supersedes"
                    conflict_count += 1
                if new_status != old_status or (
                    new_status == "active" and float(item.get("valid_to") or 0) > 0
                ):
                    valid_to = 0.0 if new_status == "active" else float(selected.get("valid_from") or 0)
                    self.store.update_claim_status(str(item["claim_id"]), new_status, valid_to=valid_to)
                if transition_type:
                    transition_count += int(self.store.add_claim_transition(
                        from_claim_id=str(item["claim_id"]),
                        to_claim_id=str(selected["claim_id"]),
                        transition_type=transition_type,
                        reason="slot_reconciliation",
                        evidence={"slot_key": slot_key, "selected": selected["claim_id"]},
                        decided_by="deterministic_ledger",
                        confidence=min(float(item.get("confidence") or 0.5), float(selected.get("confidence") or 0.5)),
                    ))
            slot_status = "uncertain" if not current_ids else ("conflicted" if conflict_count else "active")
            self.store.upsert_claim_slot(
                scope_id, slot_key, current_ids=current_ids, status=slot_status,
                conflict_count=conflict_count,
                view_text="；".join(
                    str(item.get("object") or "") for item in items if str(item["claim_id"]) in current_ids
                )[:800],
            )
        return {"slots": len(grouped), "transitions": transition_count}

    def materialize_thread_views(self, scope_id: str) -> int:
        conn = self.store._get_conn()
        threads = self.store.list_threads(scope_id=scope_id, status="active", limit=2000)
        changed = 0
        for thread in threads:
            thread_id = str(thread["thread_id"])
            rows = conn.execute(
                """SELECT c.* FROM memory_claims c
                   JOIN memory_thread_members m ON m.episode_id=c.source_episode_id
                   WHERE m.thread_id=? AND c.scope_id=?
                   ORDER BY c.valid_from,c.created_ts,c.claim_id""",
                (thread_id, str(scope_id)),
            ).fetchall()
            claims = [dict(row) for row in rows]
            active = [self.store.decode_claim(row) for row in claims if str(row["status"]) == "active"]
            transitions = self.store.list_claim_transitions(thread_id=thread_id, limit=30)
            open_loops = [row for row in active if row.get("claim_type") in {"unresolved", "plan"}]
            promises = [row for row in active if row.get("claim_type") == "commitment"]
            uncertain = [self.store.decode_claim(row) for row in claims if str(row["status"]) == "uncertain"][:4]
            source_ids = list(dict.fromkeys(str(row["source_episode_id"]) for row in claims))
            payload = {
                "active_facts": active[:12],
                "recent_transitions": transitions[:8],
                "open_loops": open_loops[:5],
                "active_promises": promises[:5],
                "counter_evidence": uncertain,
                "source_episode_ids": source_ids,
                "coverage": 1.0 if claims else 0.0,
                "conflict_count": sum(1 for row in claims if str(row["status"]) == "uncertain"),
                "generator_version": EXTRACTOR_VERSION,
            }
            changed += int(self.store.materialize_thread_view(thread_id, payload))
        return changed
