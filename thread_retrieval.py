"""Parallel retrieval laboratory for the 6.0-test5 Shadow pipeline."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections import defaultdict
from typing import Any

from .prospective_memory import ProspectiveMemory
from .store_utils import extract_terms
from .thread_store import ThreadStore


ALGORITHM_VERSION = "test8-context-retrieval-v5-integrated"
_STATE_TYPES = {"relationship", "commitment", "boundary", "preference", "identity", "plan", "unresolved"}
_RELATION_PRIORITY = {
    "supersedes": 1.0, "resolves": 1.0, "breaks": 1.0, "contradicts_stage": 0.95,
    "causes": 0.9, "continues": 0.82, "responds_to": 0.82, "confirms": 0.72,
    "retells": 0.68, "parallel": 0.45,
}
_CUE_ACTIONS = (
    "外出", "探查", "等待", "等他", "回家", "见面", "出门", "梳头", "教字", "写字",
    "约定", "答应", "不许", "不要", "取消", "完成", "离开", "回来", "照顾", "寻找",
)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value)) if value else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _norm(text: Any, limit: int = 1000) -> str:
    return " ".join(str(text or "").split()).strip()[:limit]


def _emotion_signal(value: Any, default: float = 0.0) -> float:
    """Coerce the optional signal to the bounded range used by prospective memory."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, min(1.0, value)) if math.isfinite(value) else float(default)


def _nonnegative_int(value: Any) -> int | None:
    """Return a real non-negative integer, preserving zero as a valid value."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class ThreadRetrievalLab:
    """Retrieves thread context without mutating or injecting ProviderRequest."""

    def __init__(self, store: ThreadStore, timezone_name: str = "Asia/Shanghai", *, max_nodes: int = 5, evolution_nodes: int = 7):
        self.max_nodes = max(3, min(7, int(max_nodes)))
        self.evolution_nodes = max(3, min(7, int(evolution_nodes)))
        self.store = store
        self.timezone_name = str(timezone_name or "Asia/Shanghai")
        self.prospective = ProspectiveMemory(store, self.timezone_name)

    def _episode(self, episode_id: str) -> dict[str, Any] | None:
        row = self.store._get_conn().execute(
            """SELECT episode_id,memo_name,source_batch_id,occurred_at,event_ts,time_basis,
                      memory_type,scene_anchor,retrieval_key,state_change,card_text,
                      evidence_quality,scene_start_turn,scene_end_turn,diary_content_hash
               FROM episodes WHERE episode_id=? AND active=1""",
            (str(episode_id),),
        ).fetchone()
        return dict(row) if row else None

    def _seed_episodes(self, scope_id: str, base_hits: list[dict[str, Any]],
                       query: str, entities: list[str]) -> list[dict[str, Any]]:
        conn = self.store._get_conn()
        seeds: dict[str, dict[str, Any]] = {}
        for rank, hit in enumerate(base_hits):
            memo_name = str(hit.get("memo_name") or "")
            if not memo_name:
                continue
            row = conn.execute(
                """SELECT e.episode_id,e.memo_name,e.event_ts,e.retrieval_key,e.scene_anchor,e.card_text
                   FROM episodes e JOIN thread_episode_scopes s ON s.episode_id=e.episode_id
                   WHERE e.memo_name=? AND s.scope_id=? AND e.active=1""",
                (memo_name, str(scope_id)),
            ).fetchone()
            if row:
                item = dict(row)
                item["seed_reason"] = "base_recall"
                item["seed_score"] = max(0.2, 1.0 - rank * 0.08)
                seeds[str(row["episode_id"])] = item
        # The online Canary path must not pay Jieba's dictionary cold start.
        # The deterministic fallback remains compatible with indexed terms.
        terms = list(dict.fromkeys([
            *entities,
            *extract_terms(query, initialize_segmenter=False),
        ]))[:20]
        if terms:
            placeholders = ",".join("?" for _ in terms)
            rows = conn.execute(
                f"""SELECT DISTINCT e.episode_id,e.memo_name,e.event_ts,e.retrieval_key,e.scene_anchor,e.card_text
                    FROM thread_episode_terms t INDEXED BY idx_thread_episode_terms_term
                    CROSS JOIN episodes e ON e.episode_id=t.episode_id
                    CROSS JOIN thread_episode_scopes s ON s.episode_id=e.episode_id
                    WHERE s.scope_id=? AND t.term IN ({placeholders}) AND e.active=1
                    ORDER BY e.event_ts DESC LIMIT 30""",
                (str(scope_id), *terms),
            ).fetchall()
            for row in rows:
                episode_id = str(row["episode_id"])
                if episode_id not in seeds:
                    item = dict(row)
                    item["seed_reason"] = "entity_or_term"
                    item["seed_score"] = 0.58
                    seeds[episode_id] = item
        return sorted(seeds.values(), key=lambda item: (-float(item["seed_score"]), -float(item["event_ts"] or 0)))[:24]

    def _thread_route(self, scope_id: str, seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not seeds:
            return []
        conn = self.store._get_conn()
        ids = [str(item["episode_id"]) for item in seeds]
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""SELECT t.*,
                       (SELECT COUNT(*) FROM memory_thread_members m2 WHERE m2.thread_id=t.thread_id) AS member_count,
                       MAX(m.membership_confidence) AS seed_strength
                FROM memory_thread_members m
                JOIN memory_threads t ON t.thread_id=m.thread_id
                WHERE m.episode_id IN ({placeholders}) AND t.scope_id=? AND t.status='active'
                GROUP BY t.thread_id
                ORDER BY seed_strength DESC,t.confidence DESC,t.last_event_ts DESC LIMIT 20""",
            (*ids, str(scope_id)),
        ).fetchall()
        return [dict(row) for row in rows]

    def _claim_route(self, scope_id: str, plan: dict[str, Any], query: str) -> list[dict[str, Any]]:
        if not bool(plan.get("current_state_intent")):
            return []
        slots = {str(value) for value in (plan.get("target_claim_slots") or []) if str(value)}
        terms = extract_terms(query, initialize_segmenter=False)
        claims = self.store.list_claims(scope_id=scope_id, status="active", limit=2000)
        output = []
        for claim in claims:
            claim_type = str(claim.get("claim_type") or "")
            text = " ".join(str(claim.get(key) or "") for key in ("slot_key", "subject", "object"))
            slot_match = not slots or claim_type in slots or any(value in str(claim.get("slot_key") or "") for value in slots)
            lexical = sum(1 for term in terms if term and term in text)
            entity_match = any(entity in text for entity in plan.get("target_entities") or [])
            object_text = str(claim.get("object") or "")
            strong_lexical = lexical >= 2 or (len(object_text) >= 12 and object_text[:24] in query)
            if (slot_match or strong_lexical) and (lexical or entity_match or claim_type in slots):
                item = dict(claim)
                item["route_score"] = round(
                    min(1.0, 0.45 + lexical * 0.12 + (0.18 if entity_match else 0)
                        + float(claim.get("confidence") or 0) * 0.2), 4
                )
                output.append(item)
        output.sort(key=lambda item: (-float(item["route_score"]), -float(item.get("valid_from") or 0)))
        # Reserve one counter-evidence record when the query asks whether a state still holds.
        if plan.get("requires_counterevidence") and output:
            slot_keys = {str(item.get("slot_key") or "") for item in output[:4]}
            history = [
                item for item in self.store.list_claims(scope_id=scope_id, limit=3000)
                if str(item.get("slot_key") or "") in slot_keys
                and str(item.get("status") or "") in {"superseded", "resolved", "broken", "uncertain"}
            ]
            if history:
                counter = history[0]
                counter["counterevidence"] = True
                counter["route_score"] = 0.52
                output.append(counter)
        return output[:8]

    def _transition_route(self, scope_id: str, plan: dict[str, Any], query: str) -> dict[str, Any]:
        if not bool(plan.get("evolution_intent")):
            return {"transitions": [], "nodes": [], "edges": []}
        conn = self.store._get_conn()
        rows = conn.execute(
            """SELECT t.*,f.object AS from_object,f.claim_type AS from_type,
                      f.source_episode_id AS from_episode_id,f.valid_from AS from_valid_from,
                      n.object AS to_object,n.claim_type AS to_type,
                      n.source_episode_id AS to_episode_id,n.valid_from AS to_valid_from
               FROM memory_claim_transitions t
               JOIN memory_claims f ON f.claim_id=t.from_claim_id
               JOIN memory_claims n ON n.claim_id=t.to_claim_id
               WHERE f.scope_id=? AND n.scope_id=?
               ORDER BY t.created_ts DESC,t.id DESC LIMIT 1200""",
            (str(scope_id), str(scope_id)),
        ).fetchall()
        query_terms = extract_terms(query, initialize_segmenter=False)
        target_slots = {str(value) for value in plan.get("target_claim_slots") or [] if str(value)}
        ranked: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            text = " ".join((str(item.get("from_object") or ""), str(item.get("to_object") or "")))
            lexical = sum(1 for term in query_terms if term and term in text)
            slot_match = bool(target_slots & {str(item.get("from_type") or ""), str(item.get("to_type") or "")})
            if not lexical and not slot_match:
                continue
            relation = str(item.get("transition_type") or "supersedes")
            item["route_score"] = round(
                min(1.0, lexical * 0.14 + (0.28 if slot_match else 0.0)
                    + _RELATION_PRIORITY.get(relation, 0.55) * 0.45
                    + float(item.get("confidence") or 0.0) * 0.18),
                4,
            )
            ranked.append(item)
        ranked.sort(key=lambda item: (
            -float(item["route_score"]),
            -max(float(item.get("from_valid_from") or 0), float(item.get("to_valid_from") or 0)),
        ))
        selected = ranked[:4]
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        for transition in selected:
            from_episode = str(transition.get("from_episode_id") or "")
            to_episode = str(transition.get("to_episode_id") or "")
            for episode_id, role in ((from_episode, "transition_before"), (to_episode, "transition_after")):
                if not episode_id or episode_id in nodes:
                    continue
                episode = self._episode(episode_id)
                if episode:
                    episode["selection_score"] = float(transition["route_score"]) + 1.0
                    episode["selection_reasons"] = [role, "claim_transition"]
                    nodes[episode_id] = episode
            if from_episode and to_episode:
                edges.append({
                    "source_episode_id": from_episode,
                    "target_episode_id": to_episode,
                    "edge_type": str(transition.get("transition_type") or "supersedes"),
                    "confidence": float(transition.get("confidence") or 0.5),
                    "relation_score": _RELATION_PRIORITY.get(
                        str(transition.get("transition_type") or ""), 0.7
                    ),
                    "decision_source": "claim_ledger",
                })
        ordered_nodes = sorted(nodes.values(), key=lambda item: (
            float(item.get("event_ts") or 0) if float(item.get("event_ts") or 0) > 0 else float("inf"),
            str(item.get("episode_id") or ""),
        ))
        return {"transitions": selected, "nodes": ordered_nodes[:7], "edges": edges[:6]}

    @staticmethod
    def _merge_transition_subgraph(subgraph: dict[str, Any], transition_route: dict[str, Any],
                                   max_nodes: int) -> dict[str, Any]:
        transition_nodes = list(transition_route.get("nodes") or [])
        if not transition_nodes:
            return subgraph
        selected: dict[str, dict[str, Any]] = {
            str(item.get("episode_id") or ""): dict(item)
            for item in transition_nodes if item.get("episode_id")
        }
        for item in subgraph.get("nodes") or []:
            episode_id = str(item.get("episode_id") or "")
            if episode_id and episode_id not in selected and len(selected) < max_nodes:
                selected[episode_id] = dict(item)
        nodes = sorted(selected.values(), key=lambda item: (
            float(item.get("event_ts") or 0) if float(item.get("event_ts") or 0) > 0 else float("inf"),
            str(item.get("episode_id") or ""),
        ))[:max_nodes]
        node_ids = {str(item.get("episode_id") or "") for item in nodes}
        edges = [
            dict(item) for item in [*(subgraph.get("edges") or []), *(transition_route.get("edges") or [])]
            if str(item.get("source_episode_id") or "") in node_ids
            and str(item.get("target_episode_id") or "") in node_ids
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "node_limit": max_nodes,
            "coverage": max(float(subgraph.get("coverage") or 0), 1.0 if transition_nodes else 0.0),
        }

    def _minimal_subgraph(self, threads: list[dict[str, Any]], seeds: list[dict[str, Any]],
                          claims: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
        conn = self.store._get_conn()
        seed_ids = {str(item["episode_id"]) for item in seeds}
        claim_ids = {str(item.get("source_episode_id") or "") for item in claims}
        max_nodes = self.evolution_nodes if bool(plan.get("evolution_intent")) else self.max_nodes
        candidates: dict[str, dict[str, Any]] = {}
        selected_edges: list[dict[str, Any]] = []
        for thread in threads[:6]:
            thread_id = str(thread["thread_id"])
            rows = conn.execute(
                """SELECT m.role,m.sequence_no,m.membership_confidence,e.*
                   FROM memory_thread_members m JOIN episodes e ON e.episode_id=m.episode_id
                   WHERE m.thread_id=? ORDER BY m.sequence_no,e.event_ts,e.episode_id""",
                (thread_id,),
            ).fetchall()
            member_ids = [str(row["episode_id"]) for row in rows]
            if not member_ids:
                continue
            placeholders = ",".join("?" for _ in member_ids)
            edges = conn.execute(
                f"""SELECT * FROM memory_episode_edges WHERE status='accepted'
                    AND source_episode_id IN ({placeholders}) AND target_episode_id IN ({placeholders})
                    ORDER BY confidence DESC,id""",
                (*member_ids, *member_ids),
            ).fetchall()
            for row in rows:
                item = dict(row)
                episode_id = str(item["episode_id"])
                score = float(item.get("membership_confidence") or 0.5) * 0.25
                reasons = []
                if episode_id in seed_ids:
                    score += 1.0
                    reasons.append("query_seed")
                if episode_id in claim_ids:
                    score += 0.9
                    reasons.append("claim_source")
                if _norm(item.get("state_change")):
                    score += 0.35
                    reasons.append("state_transition")
                item["selection_score"] = score
                item["selection_reasons"] = reasons
                item["thread_id"] = thread_id
                previous = candidates.get(episode_id)
                if previous is None or score > float(previous["selection_score"]):
                    candidates[episode_id] = item
            for edge_row in edges:
                edge = dict(edge_row)
                edge["relation_score"] = _RELATION_PRIORITY.get(str(edge["edge_type"]), 0.4)
                selected_edges.append(edge)
                if str(edge["edge_type"]) in {"supersedes", "resolves", "breaks", "contradicts_stage", "causes"}:
                    for endpoint in (str(edge["source_episode_id"]), str(edge["target_episode_id"])):
                        if endpoint in candidates:
                            candidates[endpoint]["selection_score"] += 0.75
                            candidates[endpoint]["selection_reasons"].append("paired_transition")
        ranked = sorted(candidates.values(), key=lambda item: (
            -float(item["selection_score"]), -float(item.get("event_ts") or 0), str(item["episode_id"])
        ))
        selected: dict[str, dict[str, Any]] = {str(item["episode_id"]): item for item in ranked[:max_nodes]}
        # A relation is useful only when both endpoints survive. Complete strong
        # before/after pairs by replacing the weakest background node.
        for edge in sorted(selected_edges, key=lambda item: -float(item["relation_score"])):
            if str(edge["edge_type"]) not in {"supersedes", "resolves", "breaks", "contradicts_stage", "causes"}:
                continue
            endpoints = [str(edge["source_episode_id"]), str(edge["target_episode_id"])]
            present = [value in selected for value in endpoints]
            if any(present) and not all(present):
                missing = endpoints[present.index(False)]
                if missing in candidates:
                    if len(selected) >= max_nodes:
                        removable = min(
                            (value for value in selected.values() if str(value["episode_id"]) not in seed_ids),
                            key=lambda item: float(item["selection_score"]), default=None,
                        )
                        if removable:
                            selected.pop(str(removable["episode_id"]), None)
                    if len(selected) < max_nodes:
                        selected[missing] = candidates[missing]
        nodes = sorted(selected.values(), key=lambda item: (
            float(item.get("event_ts") or 0) if float(item.get("event_ts") or 0) > 0 else float("inf"),
            str(item["episode_id"]),
        ))
        node_ids = {str(item["episode_id"]) for item in nodes}
        edges = [
            item for item in selected_edges
            if str(item["source_episode_id"]) in node_ids and str(item["target_episode_id"]) in node_ids
        ]
        return {"nodes": nodes, "edges": edges, "node_limit": max_nodes,
                "coverage": round(len(node_ids & (seed_ids | claim_ids)) / max(1, len(seed_ids | claim_ids)), 4)}

    def _deduplicate(self, base_hits: list[dict[str, Any]], subgraph: dict[str, Any]) -> dict[str, Any]:
        conn = self.store._get_conn()
        base_memos = {str(item.get("memo_name") or "") for item in base_hits if item.get("memo_name")}
        accepted: list[dict[str, Any]] = []
        relation_only: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        seen_episodes = {
            str(item.get("episode_id")) for item in base_hits if item.get("episode_id")
        }
        seen_sources: set[tuple[str, int, int]] = set()

        def source_range(item: dict[str, Any]) -> tuple[str, int, int] | None:
            batch_id = str(item.get("source_batch_id") or "")
            start = _nonnegative_int(item.get("scene_start_turn"))
            end = _nonnegative_int(item.get("scene_end_turn"))
            if batch_id and start is not None and end is not None and end >= start:
                return batch_id, start, end
            return None

        # Seed identity from every 5.x hit and its linked episode. A different
        # memo name can still point to the same source turn range.
        base_episodes: list[dict[str, Any]] = []
        if base_memos:
            placeholders = ",".join("?" for _ in base_memos)
            rows = conn.execute(
                f"""SELECT episode_id,source_batch_id,scene_start_turn,scene_end_turn
                    FROM episodes WHERE memo_name IN ({placeholders})""",
                tuple(base_memos),
            ).fetchall()
            base_episodes = [dict(row) for row in rows]
        for base in [*base_hits, *base_episodes]:
            if base.get("episode_id"):
                seen_episodes.add(str(base["episode_id"]))
            source_key = source_range(base)
            if source_key is not None:
                seen_sources.add(source_key)

        # Only provenance proves duplication. Similar or identical prose can
        # describe a genuinely recurring event and must remain retrievable.
        for node in subgraph.get("nodes") or []:
            item = dict(node)
            episode_id = str(item.get("episode_id") or "")
            memo_name = str(item.get("memo_name") or "")
            source_key = source_range(item)
            reason = ""
            if memo_name in base_memos:
                reason = "same_memo_as_5x"
            elif episode_id in seen_episodes:
                reason = "same_episode"
            elif source_key is not None and source_key in seen_sources:
                reason = "same_source_turn_range"
            else:
                retell = conn.execute(
                    """SELECT 1 FROM memory_episode_edges WHERE status='accepted' AND edge_type='retells'
                       AND ((source_episode_id=? AND target_episode_id IN
                         (SELECT episode_id FROM episodes WHERE memo_name IN ({placeholders})))
                        OR (target_episode_id=? AND source_episode_id IN
                         (SELECT episode_id FROM episodes WHERE memo_name IN ({placeholders})))) LIMIT 1""".replace(
                        "{placeholders}", ",".join("?" for _ in base_memos) or "''"
                    ),
                    (episode_id, *base_memos, episode_id, *base_memos),
                ).fetchone() if base_memos else None
                if retell:
                    reason = "accepted_retell_of_5x"
            if reason:
                item["dedup_reason"] = reason
                item["relation_only"] = True
                relation_only.append(item)
                removed.append({"episode_id": episode_id, "memo_name": memo_name, "reason": reason})
            else:
                item["relation_only"] = False
                accepted.append(item)
            seen_episodes.add(episode_id)
            if source_key is not None:
                seen_sources.add(source_key)
        return {"new_nodes": accepted, "relation_only_nodes": relation_only,
                "removed": removed, "duplicate_count": len(removed)}

    @staticmethod
    def _render_preview(claims: list[dict[str, Any]], dedup: dict[str, Any],
                        prospective: dict[str, Any] | None, edges: list[dict[str, Any]]) -> str:
        lines = ["<MemoryContextShadow>", "以下仅为6.0实验室预览，不会进入本轮回答。"]
        if claims:
            lines.append("[当前有效事实]")
            for claim in claims[:4]:
                prefix = "可能存在争议：" if claim.get("counterevidence") else ""
                lines.append(f"- {prefix}{claim.get('object', '')}")
        nodes = dedup.get("new_nodes") or []
        if nodes:
            lines.append("[相关经历脉络，按事件时间]")
            for node in nodes:
                lines.append(f"- {node.get('occurred_at') or '时间未知'} | {node.get('scene_anchor') or node.get('retrieval_key') or node.get('card_text', '')[:180]}")
        if edges:
            lines.append("[必要关系]")
            for edge in edges[:6]:
                lines.append(f"- {edge.get('edge_type')}: {edge.get('source_episode_id')} -> {edge.get('target_episode_id')}")
        if prospective:
            lines.append("[可能需要自然浮现的未决事项]")
            lines.append(f"- {prospective.get('description', '')}")
        lines.append("</MemoryContextShadow>")
        return "\n".join(lines)

    def run(self, *, scope_id: str, query: str, plan: dict[str, Any],
            base_result: dict[str, Any], context_text: str = "", emotion_signal: float | None = None,
            mode: str = "full", now_ts: float | None = None, record: bool = True) -> dict[str, Any]:
        started = time.perf_counter()
        # Keep old callers at zero while allowing a plan-carried signal to flow
        # through without deriving it from a new online-model request.
        if emotion_signal is None:
            emotion_signal = plan.get("emotion_signal", 0.0) if isinstance(plan, dict) else 0.0
        emotion_signal = _emotion_signal(emotion_signal)
        query = _norm(query)
        base_hits = [item for item in (base_result.get("hits") or []) if bool(item.get("selected"))]
        seeds = (
            []
            if mode == "prospective_only"
            else self._seed_episodes(
                scope_id,
                base_hits,
                query,
                list(plan.get("target_entities") or []),
            )
        )
        threads = (
            self._thread_route(scope_id, seeds)
            if mode not in {"base", "no_threads", "prospective_only"}
            else []
        )
        claims = (
            self._claim_route(scope_id, plan, query)
            if mode not in {"base", "no_claims", "prospective_only"}
            else []
        )
        subgraph = self._minimal_subgraph(threads, seeds, claims, plan) if threads else {"nodes": [], "edges": [], "node_limit": 0, "coverage": 0.0}
        transition_route = (
            self._transition_route(scope_id, plan, query)
            if mode not in {"base", "no_threads", "no_claims", "prospective_only"}
            else {"transitions": [], "nodes": [], "edges": []}
        )
        subgraph = self._merge_transition_subgraph(
            subgraph, transition_route, self.evolution_nodes if bool(plan.get("evolution_intent")) else self.max_nodes,
        )
        prospective_result = {"selected": None, "candidates": [], "candidate_count": 0, "shadow": True}
        if mode not in {"base", "no_prospective"} and bool(plan.get("prospective_intent")):
            prospective_result = self.prospective.trigger(
                scope_id, query, context_text=context_text, emotion_signal=emotion_signal,
                now_ts=now_ts, record=record,
            )
        dedup = self._deduplicate(base_hits, subgraph)
        selected_prospective = prospective_result.get("selected")
        preview = self._render_preview(claims, dedup, selected_prospective, subgraph.get("edges") or [])
        thread_ids = [str(item["thread_id"]) for item in threads]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        observation_id = "thread_lab_" + uuid.uuid4().hex[:20]
        result = {
            "algorithm_version": ALGORITHM_VERSION,
            "mode": mode,
            "shadow": True,
            "query": query,
            "query_plan": plan,
            "emotion_signal": emotion_signal,
            "routes": {
                "thread_seeds": seeds, "threads": threads, "current_claims": claims,
                "transitions": transition_route, "prospective": prospective_result,
            },
            "subgraph": subgraph,
            "dedup": dedup,
            "injection_preview": preview,
            "base_selected_count": len(base_hits),
            "base_selected_memos": [str(item.get("memo_name") or "") for item in base_hits],
            "additional_episode_count": len(dedup.get("new_nodes") or []),
            "relation_only_count": len(dedup.get("relation_only_nodes") or []),
            "claims_count": len(claims),
            "prospective_count": int(bool(selected_prospective)),
            "estimated_additional_chars": len(preview),
            "elapsed_ms": elapsed_ms,
            "observation_id": observation_id,
        }
        if record:
            self.store.record_thread_query_observation({
                "request_id": observation_id, "scope_id": scope_id,
                "query_text": query, "thread_ids": thread_ids, "subgraph": subgraph,
                "claims": claims, "prospective": [selected_prospective] if selected_prospective else [],
                "query_plan": plan, "routes": result["routes"], "dedup": dedup,
                "injection_preview": preview,
                "metrics": {"base_count": len(base_hits), "additional": result["additional_episode_count"]},
                "latency_ms": elapsed_ms,
            })
        return result

    @staticmethod
    def _evaluation_cue(text: str, fallback: str) -> str:
        source = _norm(text, 320)
        blocked = {
            "现在", "关于", "还是", "已经", "以前", "后来", "关系", "承诺", "约定",
            "计划", "边界", "喜欢", "身份", "答应", "取消", "完成", "解决", "事情",
        }
        terms = [
            term for term in extract_terms(source)
            if 2 <= len(term) <= 8 and term not in blocked
        ]
        terms.sort(key=lambda value: (
            -int(any(action in value for action in _CUE_ACTIONS)),
            -min(len(value), 12),
            source.find(value) if value in source else 10**6,
            value,
        ))
        selected: list[str] = []
        for term in terms:
            if any(term in old or old in term for old in selected):
                continue
            selected.append(term)
            if len(selected) >= 2:
                break
        return "、".join(selected) or fallback

    @staticmethod
    def _prospective_evaluation_cue(item: dict[str, Any]) -> str:
        description = _norm(item.get("description"), 500)
        clauses = re.findall(
            r"[\u4e00-\u9fff]{0,8}(?:明天|后天|下次|约定|答应|计划|等待|不许|欠)[\u4e00-\u9fff]{1,14}",
            description,
        )
        if clauses:
            clauses.sort(key=lambda value: (-len(value), description.find(value)))
            return clauses[0][:22]
        noisy = {"只是", "后来", "没有", "时候", "好好", "一次", "新的", "才想起来"}
        terms = []
        for raw in item.get("trigger_terms") or []:
            value = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9_-]", "", str(raw or ""))
            if not 3 <= len(value) <= 16 or value in noisy:
                continue
            if any(value in old or old in value for old in terms):
                continue
            terms.append(value)
        terms.sort(key=lambda value: (
            -int(any(cue in value for cue in ("写", "教", "梳", "等", "去", "回", "揉", "学", "名字", "晚饭"))),
            -len(value),
            description.find(value) if value in description else 10**6,
        ))
        entities = [
            str(value) for value in item.get("target_entities") or []
            if 2 <= len(str(value)) <= 10 and not str(value).startswith("real-copy-")
        ]
        selected = [*entities[:1], *terms[:1]]
        return "、".join(selected) or ThreadRetrievalLab._evaluation_cue(description, "未决事项")

    def score_eval_case(self, case: dict[str, Any], base_result: dict[str, Any],
                        result: dict[str, Any]) -> dict[str, Any]:
        """Score a Shadow result without treating self-generated labels as human truth."""
        expected_claims = {str(value) for value in case.get("expected_claim_ids") or [] if str(value)}
        expected_eps = [str(value) for value in case.get("expected_episode_ids") or [] if str(value)]
        expected_future = {str(value) for value in case.get("expected_prospective_ids") or [] if str(value)}
        expected_order = [str(value) for value in case.get("expected_order") or [] if str(value)]

        base_hits = [item for item in (base_result.get("hits") or []) if bool(item.get("selected"))]
        base_names = [str(item.get("memo_name") or "") for item in base_hits if item.get("memo_name")]
        conn = self.store._get_conn()
        memo_by_episode: dict[str, str] = {}
        if expected_eps:
            placeholders = ",".join("?" for _ in expected_eps)
            rows = conn.execute(
                f"SELECT episode_id,memo_name FROM episodes WHERE episode_id IN ({placeholders})",
                tuple(expected_eps),
            ).fetchall()
            memo_by_episode = {str(row["episode_id"]): str(row["memo_name"] or "") for row in rows}
        expected_memos = {value for value in memo_by_episode.values() if value}

        nodes = list((result.get("subgraph") or {}).get("nodes") or [])
        node_ids = [str(item.get("episode_id") or "") for item in nodes if item.get("episode_id")]
        new_nodes = list((result.get("dedup") or {}).get("new_nodes") or [])
        new_names = [str(item.get("memo_name") or "") for item in new_nodes if item.get("memo_name")]
        combined_names = list(dict.fromkeys([*base_names, *new_names]))
        claims = list((result.get("routes") or {}).get("current_claims") or [])
        actual_claims = {str(item.get("claim_id") or "") for item in claims if item.get("claim_id")}
        claim_episode_ids = {str(item.get("source_episode_id") or "") for item in claims if item.get("source_episode_id")}
        selected_future = ((result.get("routes") or {}).get("prospective") or {}).get("selected") or {}
        actual_future = {str(selected_future.get("item_id") or "")} - {""}

        def first_rank(names: list[str]) -> int:
            ranks = [names.index(name) + 1 for name in expected_memos if name in names]
            return min(ranks, default=-1)

        baseline_rank = first_rank(base_names)
        combined_rank = first_rank(combined_names)
        baseline_hit = baseline_rank > 0
        episode_hit = bool(set(expected_eps) & (set(node_ids) | claim_episode_ids)) if expected_eps else True
        all_episodes_hit = set(expected_eps) <= (set(node_ids) | claim_episode_ids) if expected_eps else True
        claim_hit = bool(expected_claims & actual_claims) if expected_claims else True
        prospective_hit = bool(expected_future & actual_future) if expected_future else True

        if combined_rank < 0 and episode_hit:
            shadow_episode_order = list(dict.fromkeys([
                *[str(item.get("episode_id") or "") for item in new_nodes],
                *[str(item.get("source_episode_id") or "") for item in claims],
                *node_ids,
            ]))
            shadow_ranks = [
                len(base_names) + shadow_episode_order.index(episode_id) + 1
                for episode_id in expected_eps if episode_id in shadow_episode_order
            ]
            combined_rank = min(shadow_ranks, default=-1)

        order_ok = True
        if expected_order:
            positions = {episode_id: node_ids.index(episode_id) for episode_id in expected_order if episode_id in node_ids}
            order_ok = len(positions) == len(expected_order) and all(
                positions[left] < positions[right]
                for left, right in zip(expected_order, expected_order[1:])
            )
        case_type = str(case.get("case_type") or "")
        if case_type == "current_state":
            passed = claim_hit and episode_hit
        elif case_type == "prospective":
            passed = prospective_hit and episode_hit
        elif case_type in {"evolution", "temporal_order"}:
            passed = all_episodes_hit and order_ok
        else:
            passed = episode_hit and claim_hit and prospective_hit and order_ok

        duplicates = max(0, len([*base_names, *new_names]) - len(set([*base_names, *new_names])))
        return {
            "passed": bool(passed),
            "baseline_hit": baseline_hit,
            "combined_hit": bool(baseline_hit or episode_hit),
            "baseline_rank": baseline_rank,
            "combined_rank": combined_rank,
            "baseline_rr": round(1.0 / baseline_rank, 6) if baseline_rank > 0 else 0.0,
            "combined_rr": round(1.0 / combined_rank, 6) if combined_rank > 0 else 0.0,
            "claim_hit": claim_hit,
            "prospective_hit": prospective_hit,
            "episode_hit": episode_hit,
            "all_episodes_hit": all_episodes_hit,
            "order_ok": order_ok,
            "duplicate_count": duplicates,
            "injected_object_count": len(base_names) + len(new_names),
            "expected_memos": sorted(expected_memos),
            "actual": {
                "claims": sorted(actual_claims),
                "episodes": node_ids,
                "prospective": sorted(actual_future),
                "base_memos": base_names,
                "combined_memos": combined_names,
            },
        }

    def generate_eval_cases(self, scope_id: str, limit: int = 120) -> dict[str, Any]:
        generated = 0
        generated_by_type: dict[str, int] = defaultdict(int)
        seen: set[str] = set()
        case_ids: set[str] = set()
        for claim in self.store.list_claims(scope_id=scope_id, status="active", limit=3000):
            if generated >= limit:
                break
            claim_type = str(claim.get("claim_type") or "")
            if claim_type not in _STATE_TYPES:
                continue
            cue = self._evaluation_cue(str(claim.get("object") or ""), claim_type)
            if len(cue) < 4:
                continue
            query = f"现在关于{cue}，还成立吗？"
            key = hashlib.sha256(query.encode("utf-8")).hexdigest()[:20]
            if key in seen:
                continue
            seen.add(key)
            case_id = "auto_claim_" + key
            self.store.upsert_thread_eval_case({
                "case_id": case_id, "scope_id": scope_id,
                "case_type": "current_state", "query": query,
                "expected_claim_ids": [str(claim["claim_id"])],
                "expected_episode_ids": [str(claim.get("source_episode_id") or "")],
                "source": "deterministic_source_holdout", "confidence": 0.72,
                "notes": "由派生 claim 生成，只用于自动回归，不冒充人工金标。",
            })
            case_ids.add(case_id)
            generated += 1
            generated_by_type["current_state"] += 1
        transitions = self.store.list_claim_transitions(limit=3000)
        for transition in transitions:
            if generated >= limit:
                break
            from_id = str(transition.get("from_claim_id") or "")
            to_id = str(transition.get("to_claim_id") or "")
            if not from_id or not to_id:
                continue
            rows = self.store._get_conn().execute(
                "SELECT claim_id,object,source_episode_id,valid_from FROM memory_claims WHERE claim_id IN (?,?)",
                (from_id, to_id),
            ).fetchall()
            by_id = {str(row["claim_id"]): dict(row) for row in rows}
            if from_id not in by_id or to_id not in by_id:
                continue
            ordered = sorted(
                [by_id[from_id], by_id[to_id]],
                key=lambda item: (float(item.get("valid_from") or 0), str(item.get("claim_id") or "")),
            )
            expected = [str(item.get("source_episode_id") or "") for item in ordered]
            if len(set(expected)) != 2 or not all(expected):
                continue
            cue = self._evaluation_cue(str(by_id[to_id].get("object") or ""), "这件事")
            query = f"关于{cue}，以前到后来发生了什么变化？"
            key = hashlib.sha256(query.encode("utf-8")).hexdigest()[:20]
            if key in seen:
                continue
            seen.add(key)
            case_id = "auto_evolution_" + key
            self.store.upsert_thread_eval_case({
                "case_id": case_id, "scope_id": scope_id,
                "case_type": "evolution", "query": query,
                "expected_episode_ids": expected, "expected_order": expected,
                "source": "deterministic_source_holdout", "confidence": 0.78,
                "notes": "由显式 claim transition 生成，用于状态前后与时间顺序回归。",
            })
            case_ids.add(case_id)
            generated += 1
            generated_by_type["evolution"] += 1
        for item in self.store.list_prospective(scope_id=scope_id, status="pending", limit=1000):
            if generated >= limit:
                break
            cue = self._prospective_evaluation_cue(item)
            if len(cue) < 4:
                continue
            query = f"关于{cue}，我们还有什么没完成？"
            key = hashlib.sha256(query.encode("utf-8")).hexdigest()[:20]
            if key in seen:
                continue
            seen.add(key)
            case_id = "auto_pro_" + key
            self.store.upsert_thread_eval_case({
                "case_id": case_id, "scope_id": scope_id,
                "case_type": "prospective", "query": query,
                "expected_prospective_ids": [str(item["item_id"])],
                "expected_episode_ids": [str(item.get("source_episode_id") or "")],
                "source": "deterministic_source_holdout", "confidence": 0.7,
                "notes": "由前瞻事项生成，只用于自动回归，不冒充人工金标。",
            })
            case_ids.add(case_id)
            generated += 1
            generated_by_type["prospective"] += 1
        pruned = self.store.prune_auto_eval_cases(scope_id, case_ids)
        return {"generated": generated, "generated_by_type": dict(generated_by_type), "pruned": pruned,
                "total": len(self.store.list_thread_eval_cases(scope_id=scope_id)),
                "source": "deterministic_source_holdout", "human_labels": 0}
