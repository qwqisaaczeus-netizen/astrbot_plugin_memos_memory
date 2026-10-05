"""Project reliable relation edges into browsable Shadow memory threads."""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from typing import Any

from .thread_store import ThreadStore

logger = logging.getLogger(__name__)

_RELATION_FAMILY = {
    "retells": "event_identity",
    "continues": "narrative_arc",
    "responds_to": "narrative_arc",
    "causes": "narrative_arc",
    "supersedes": "state_transition",
    "resolves": "state_transition",
    "breaks": "state_transition",
    "contradicts_stage": "state_transition",
    "confirms": "theme_evidence",
    "parallel": "theme_evidence",
}


class ThreadProjector:
    """Build local relation-family components, not one global graph component."""

    def __init__(self, store: ThreadStore, *, min_confidence: float = 0.84,
                 max_component_size: int = 80):
        self.store = store
        self.min_confidence = max(0.5, min(0.99, float(min_confidence)))
        self.max_component_size = max(4, min(500, int(max_component_size)))

    def project(self, *, scope_id: str = "", episode_ids: list[str] | None = None, edge_limit: int = 2000) -> dict[str, Any]:
        conn = self.store._get_conn()
        clauses = ["status='accepted'", "confidence>=?", "manual_lock IN (0,1)"]
        params: list[Any] = [self.min_confidence]
        if scope_id:
            clauses.append("scope_id=?")
            params.append(str(scope_id))
        if episode_ids is not None:
            seeds = sorted(set(map(str, episode_ids)))[:200]
            if not seeds:
                return {"threads_projected": 0, "edges_considered": 0, "version_conflicts": 0}
            placeholders = ','.join('?' for _ in seeds)
            clauses.append(f"(source_episode_id IN ({placeholders}) OR target_episode_id IN ({placeholders}))")
            params.extend(seeds + seeds)
        rows = conn.execute(
            f"""SELECT * FROM memory_episode_edges WHERE {' AND '.join(clauses)}
                 ORDER BY confidence DESC,updated_ts DESC""" + (" LIMIT ?" if episode_ids is not None else ""),
            tuple(params) + ((max(1, min(10000, int(edge_limit))) + 1,) if episode_ids is not None else ()),
        ).fetchall()
        if episode_ids is not None and len(rows) > max(1, min(10000, int(edge_limit))):
            return {"threads_projected": 0, "edges_considered": len(rows), "deferred": "edge_limit"}
        edges = [dict(row) for row in rows if str(row["edge_type"]) in _RELATION_FAMILY]
        separator_sql = "SELECT scope_id,source_episode_id,target_episode_id FROM memory_episode_edges WHERE status='accepted' AND edge_type='unrelated_similar'"
        separator_params: list[Any] = []
        if scope_id:
            separator_sql += " AND scope_id=?"
            separator_params.append(scope_id)
        if episode_ids is not None:
            separator_sql += f" AND (source_episode_id IN ({placeholders}) OR target_episode_id IN ({placeholders})) LIMIT ?"
            separator_params.extend(seeds + seeds + [max(1, min(10000, int(edge_limit))) + 1])
        separator_rows = conn.execute(separator_sql, tuple(separator_params)).fetchall()
        if episode_ids is not None and len(separator_rows) > max(1, min(10000, int(edge_limit))):
            return {"threads_projected": 0, "edges_considered": len(rows), "deferred": "separator_limit"}
        separators = {(str(row['scope_id']), frozenset((str(row['source_episode_id']), str(row['target_episode_id'])))) for row in separator_rows}
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            if edge["edge_type"] == "parallel" and float(edge["confidence"] or 0) < max(0.90, self.min_confidence):
                continue
            groups[(str(edge["scope_id"] or "default"), _RELATION_FAMILY[str(edge["edge_type"])])].append(edge)

        projected = conflicts = 0
        for (edge_scope, family), family_edges in groups.items():
            for component_edges, member_ids in self._components(family_edges, {pair for scope, pair in separators if scope == edge_scope}):
                if len(member_ids) < 2:
                    continue
                episodes = self._load_episodes(member_ids)
                if len(episodes) < 2:
                    continue
                thread_id, expected_version = self._choose_thread(edge_scope, family, member_ids)
                if episode_ids is not None:
                    # A partial neighborhood must never delete the remainder of an
                    # existing projection. Defer its full recompute to idle work.
                    existing = {str(r[0]) for r in conn.execute("SELECT episode_id FROM memory_thread_members WHERE thread_id=?", (thread_id,))}
                    if existing - member_ids:
                        conflicts += 1
                        continue
                thread, members = self._projection_payload(
                    thread_id, edge_scope, family, component_edges, episodes,
                )
                try:
                    result = self.store.replace_thread_projection(
                        thread, members, expected_version=expected_version
                    )
                    projected += int(result.get("changed", True))
                except RuntimeError as exc:
                    if "version conflict" not in str(exc):
                        raise
                    conflicts += 1
        return {"threads_projected": projected, "edges_considered": len(edges),
                "version_conflicts": conflicts}

    def _components(self, edges: list[dict[str, Any]], separators: set[frozenset[str]]):
        parent: dict[str, str] = {}
        members: dict[str, set[str]] = {}

        def find(value: str) -> str:
            parent.setdefault(value, value)
            if parent[value] != value:
                parent[value] = find(parent[value])
            return parent[value]

        def union(left: str, right: str) -> None:
            a, b = find(left), find(right)
            if a == b:
                return
            a_members = members.setdefault(a, {a})
            b_members = members.setdefault(b, {b})
            if len(a_members | b_members) > self.max_component_size:
                return
            if any(frozenset((x, y)) in separators for x in a_members for y in b_members):
                return
            parent[b] = a
            members[a] = a_members | b_members
            members.pop(b, None)

        for edge in edges:
            left, right = str(edge["source_episode_id"]), str(edge["target_episode_id"])
            if frozenset((left, right)) in separators:
                continue
            union(left, right)
        by_root: dict[str, set[str]] = defaultdict(set)
        for episode_id in parent:
            by_root[find(episode_id)].add(episode_id)
        edges_by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            left, right = str(edge['source_episode_id']), str(edge['target_episode_id'])
            if left in parent and right in parent and find(left) == find(right):
                edges_by_root[find(left)].append(edge)
        for root, member_ids in by_root.items():
            yield edges_by_root[root], member_ids

    def _load_episodes(self, member_ids: set[str]) -> list[dict[str, Any]]:
        conn = self.store._get_conn()
        placeholders = ",".join("?" for _ in member_ids)
        rows = conn.execute(
            f"""SELECT episode_id,memo_name,occurred_at,event_ts,memory_type,scene_anchor,
                       retrieval_key,entities_json,evidence_quality,source_batch_id
                 FROM episodes WHERE active=1 AND episode_id IN ({placeholders})""",
            tuple(sorted(member_ids)),
        ).fetchall()
        return [dict(row) for row in rows]

    def _choose_thread(self, scope_id: str, family: str,
                       member_ids: set[str]) -> tuple[str, int]:
        conn = self.store._get_conn()
        rows = conn.execute(
            f"""SELECT t.thread_id,t.materialized_version,m.episode_id
               FROM memory_threads t JOIN memory_thread_members m ON m.thread_id=t.thread_id
               WHERE t.scope_id=? AND t.thread_type=? AND t.status!='closed'
                 AND m.episode_id IN ({','.join('?' for _ in member_ids)})""",
            (scope_id, family, *sorted(member_ids)),
        ).fetchall()
        overlap: dict[str, set[str]] = defaultdict(set)
        versions: dict[str, int] = {}
        for row in rows:
            overlap[str(row["thread_id"])].add(str(row["episode_id"]))
            versions[str(row["thread_id"])] = int(row["materialized_version"] or 0)
        ranked = sorted(
            ((len(existing & member_ids), thread_id) for thread_id, existing in overlap.items()),
            reverse=True,
        )
        if ranked and ranked[0][0] >= max(1, len(member_ids) // 2):
            return ranked[0][1], versions[ranked[0][1]]
        seed = "|".join(sorted(member_ids)[:2])
        digest = hashlib.sha256(f"{scope_id}|{family}|{seed}".encode("utf-8")).hexdigest()[:20]
        return f"thr_{digest}", 0

    @staticmethod
    def _projection_payload(thread_id: str, scope_id: str, family: str,
                            edges: list[dict[str, Any]], episodes: list[dict[str, Any]]):
        by_id = {str(ep["episode_id"]): ep for ep in episodes}
        known_times = sorted({float(ep["event_ts"] or 0) for ep in episodes if float(ep["event_ts"] or 0) > 0})
        sequence = {ts: (index + 1) * 10 for index, ts in enumerate(known_times)}
        incoming: dict[str, list[str]] = defaultdict(list)
        edge_confidence: dict[str, list[float]] = defaultdict(list)
        for edge in edges:
            incoming[str(edge["target_episode_id"])].append(str(edge["edge_type"]))
            for episode_id in (str(edge["source_episode_id"]), str(edge["target_episode_id"])):
                edge_confidence[episode_id].append(float(edge["confidence"] or 0.5))

        def role(episode_id: str) -> str:
            relations = incoming.get(episode_id, [])
            if "resolves" in relations:
                return "resolution"
            if any(item in relations for item in ("supersedes", "continues", "responds_to", "breaks")):
                return "transition"
            if any(item == "contradicts_stage" for item in relations):
                return "counterevidence"
            earliest = min((float(ep["event_ts"] or 0) for ep in episodes if float(ep["event_ts"] or 0) > 0), default=0)
            if earliest and float(by_id[episode_id]["event_ts"] or 0) == earliest:
                return "primary"
            return "supporting"

        members = []
        for episode_id, ep in by_id.items():
            ts = float(ep["event_ts"] or 0)
            members.append({
                "episode_id": episode_id, "role": role(episode_id),
                "sequence_no": sequence.get(ts, 0),
                "membership_confidence": sum(edge_confidence[episode_id]) / max(1, len(edge_confidence[episode_id])),
                "evidence": {"order_basis": "event_ts" if ts else "uncertain",
                             "relation_family": family},
                "decision_source": "projection",
            })
        entities: dict[str, int] = defaultdict(int)
        for ep in episodes:
            try:
                values = json.loads(str(ep.get("entities_json") or "[]"))
            except json.JSONDecodeError:
                values = []
            for value in values if isinstance(values, list) else []:
                name = str(value).strip()
                if name:
                    entities[name] += 1
        common = [name for name, count in sorted(entities.items(), key=lambda item: (-item[1], item[0])) if count >= 2][:2]
        fallback = str(episodes[0].get("scene_anchor") or episodes[0].get("retrieval_key") or family)[:40]
        title = " · ".join(common) if common else fallback
        quality_rank = {"source_grounded": 3, "mixed_user_edited": 2, "diary_derived": 1}
        floor = min((str(ep.get("evidence_quality") or "diary_derived") for ep in episodes),
                    key=lambda value: quality_rank.get(value, 0))
        thread = {
            "thread_id": thread_id, "scope_id": scope_id, "thread_type": family,
            "title": title, "status": "active",
            "confidence": sum(float(edge["confidence"] or 0.5) for edge in edges) / max(1, len(edges)),
            "first_event_ts": min(known_times, default=0), "last_event_ts": max(known_times, default=0),
            "source_quality_floor": floor,
        }
        return thread, members
