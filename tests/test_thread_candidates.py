# -*- coding: utf-8 -*-
"""test1 候选阻塞、五路证据、仲裁与边写入测试（≥18 条）."""
import asyncio
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.thread_candidates import ThreadCandidates
from astrbot_plugin_memos_memory.thread_evidence import ThreadEvidence
from astrbot_plugin_memos_memory.thread_arbiter import ThreadArbiter
from astrbot_plugin_memos_memory.thread_builder import ThreadBuilder


def _vec(dim: int, val: float = 0.5) -> bytes:
    return struct.pack(f"{dim}f", *[val] * dim)


def _ep(name: str, **kwargs):
    base = {
        "memo_name": name,
        "occurred_at": "2026-07-01 晚上",
        "event_ts": time.mktime(time.strptime("2026-07-01", "%Y-%m-%d")),
        "memory_type": "plot_fact",
        "importance": 3,
        "scene_anchor": "天台",
        "retrieval_key": "银戒指 天台",
        "state_change": "",
        "long_effect": "",
        "trigger_hint": "",
        "entities": ["爱莉"],
        "unresolved": [],
        "card_text": "天台约定",
        "evidence_quality": "diary_derived",
        "source_updated_ts": 0.0,
    }
    base.update(kwargs)
    return base


class Test1CandidatesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "ep.db"), 3, "unit")
        await self.store.init()
        self.candidates = ThreadCandidates(self.store._connect)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, item):
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item,
            card_text=item["card_text"], embedding=[0.5, 0.5, 0.5],
            evidence_quality=item.get("evidence_quality", "diary_derived"),
        )
        ep = self.store.get_episode(item["memo_name"])
        return str(ep["episode_id"]) if ep else ""

    # 1. empty db → no candidates
    async def test_metadata_only_future_update_advances_watermark(self):
        eid = self.add(_ep("memos/watermark"))
        conn = self.store._connect()
        self.candidates._refresh_term_index(conn)
        future = time.time() + 86400
        conn.execute("UPDATE episodes SET updated_ts=? WHERE episode_id=?", (future, eid))
        conn.commit()
        self.candidates._refresh_term_index(conn)
        watermark = conn.execute("SELECT updated_ts FROM thread_episode_term_state WHERE episode_id=?", (eid,)).fetchone()[0]
        self.assertGreaterEqual(watermark, future)
        before = conn.total_changes
        self.candidates._refresh_term_index(conn)
        self.assertEqual(conn.total_changes, before)

    async def test_entity_projection_tracks_update_without_json_scan(self):
        eid = self.add(_ep("memos/entity-update", entities=["爱莉", "天台"]))
        conn = self.store._connect()
        first = self.candidates._refresh_term_index(conn)
        self.assertGreaterEqual(first["changed"], 1)
        entities = {row[0] for row in conn.execute(
            "SELECT entity FROM thread_episode_entities WHERE episode_id=?", (eid,)
        ).fetchall()}
        self.assertEqual(entities, {"爱莉", "天台"})
        conn.execute(
            "UPDATE episodes SET entities_json=?,updated_ts=? WHERE episode_id=?",
            (json.dumps(["爱莉", "花园"], ensure_ascii=False), time.time(), eid),
        )
        conn.commit()
        second = self.candidates._refresh_term_index(conn)
        self.assertEqual(second["queued"], 1)
        entities = {row[0] for row in conn.execute(
            "SELECT entity FROM thread_episode_entities WHERE episode_id=?", (eid,)
        ).fetchall()}
        self.assertEqual(entities, {"爱莉", "花园"})

    async def test_inactive_episode_projection_is_removed(self):
        eid = self.add(_ep("memos/inactive", entities=["爱莉", "旧庭院"]))
        conn = self.store._connect()
        self.candidates._refresh_term_index(conn)
        conn.execute("UPDATE episodes SET active=0,updated_ts=? WHERE episode_id=?", (time.time(), eid))
        conn.commit()
        result = self.candidates._refresh_term_index(conn)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM thread_episode_entities WHERE episode_id=?", (eid,)
        ).fetchone()[0], 0)

    async def test_empty_projection_queue_is_constant_work(self):
        self.add(_ep("memos/queue-empty"))
        conn = self.store._connect()
        self.candidates._refresh_term_index(conn)
        before = conn.total_changes
        result = self.candidates._refresh_term_index(conn)
        self.assertEqual(result, {"queued": 0, "changed": 0, "removed": 0})
        self.assertEqual(conn.total_changes, before)

    async def test_second_schema_init_does_not_requeue_all_episodes(self):
        self.add(_ep("memos/schema-repeat"))
        conn = self.store._connect()
        self.candidates._refresh_term_index(conn)
        self.store._threads.init_schema(conn)
        conn.commit()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM thread_episode_index_queue"
        ).fetchone()[0], 0)
        version = conn.execute(
            "SELECT value FROM thread_migration_state WHERE key='thread_schema_version'"
        ).fetchone()[0]
        self.assertEqual(version, "6.0.0-test10")

    async def test_empty_db_no_candidates(self):
        result = self.candidates.candidates_for("nonexistent", "")
        self.assertEqual(result, [])

    # 2. single episode → no self-pair
    async def test_single_episode_no_self_pair(self):
        eid = self.add(_ep("memos/one"))
        result = self.candidates.candidates_for(eid, "")
        names = {p["episode_id_a"] for p in result} | {p["episode_id_b"] for p in result}
        self.assertNotIn(eid, {p["episode_id_a"] for p in result} & {p["episode_id_b"] for p in result})

    # 3. shared entity → candidate pair
    async def test_shared_entity_generates_candidate(self):
        a = self.add(_ep("memos/a", entities=["爱莉"]))
        b = self.add(_ep("memos/b", entities=["爱莉"]))
        pairs = self.candidates.candidates_for(a, "")
        ids = {p["episode_id_a"] for p in pairs} | {p["episode_id_b"] for p in pairs}
        self.assertIn(b, ids)

    # 4. same source batch → always candidate
    async def test_same_source_batch_candidate(self):
        a = self.add(_ep("memos/batch-a", source_batch_id="batch1"))
        b = self.add(_ep("memos/batch-b", source_batch_id="batch1"))
        pairs = self.candidates.candidates_for(a, "")
        ids = {p["episode_id_a"] for p in pairs} | {p["episode_id_b"] for p in pairs}
        self.assertIn(b, ids)

    # 5. candidate count never exceeds hard limit
    async def test_candidate_limit_enforced(self):
        anchor = self.add(_ep("memos/anchor", entities=["爱莉"]))
        for i in range(60):
            self.add(_ep(f"memos/other-{i}", entities=["爱莉"]))
        pairs = self.candidates.candidates_for(anchor, "")
        self.assertLessEqual(len(pairs), 50)

    # 6. date proximity generates candidate
    async def test_date_proximity_candidate(self):
        ts_a = time.mktime(time.strptime("2026-07-01", "%Y-%m-%d"))
        ts_b = time.mktime(time.strptime("2026-07-15", "%Y-%m-%d"))
        a = self.add(_ep("memos/da", event_ts=ts_a, entities=[]))
        b = self.add(_ep("memos/db", event_ts=ts_b, entities=[]))
        pairs_a = self.candidates.candidates_for(a, "")
        pairs_b = self.candidates.candidates_for(b, "")
        all_ids = ({p["episode_id_a"] for p in pairs_a} | {p["episode_id_b"] for p in pairs_a} |
                   {p["episode_id_a"] for p in pairs_b} | {p["episode_id_b"] for p in pairs_b})
        # Either direction should find the other
        self.assertTrue(b in all_ids or a in all_ids)


class Test1EvidenceTests(unittest.TestCase):
    def _ep_dict(self, entities=None, event_ts=None, state_change="",
                 source_batch_id="", quality="diary_derived",
                 unresolved=None, embedding=None):
        ep = {
            "episode_id": "ep-x",
            "entities_json": json.dumps(entities or []),
            "event_ts": event_ts or time.mktime(time.strptime("2026-07-01", "%Y-%m-%d")),
            "state_change": state_change,
            "long_effect": "",
            "source_batch_id": source_batch_id,
            "evidence_quality": quality,
            "unresolved_json": json.dumps(unresolved or []),
            "scope_id": "",
            "_embedding": embedding,
        }
        return ep

    # 7. route A: identical entities → high score
    def test_route_a_identical_entities_high(self):
        ev = ThreadEvidence()
        a = self._ep_dict(entities=["爱莉", "天台"])
        b = self._ep_dict(entities=["爱莉", "天台"])
        self.assertGreater(ev.route_a(a, b), 0.5)

    # 8. route A: no shared entities → 0
    def test_route_a_no_shared_entities_zero(self):
        ev = ThreadEvidence()
        a = self._ep_dict(entities=["爱莉"])
        b = self._ep_dict(entities=["小舟"])
        self.assertEqual(ev.route_a(a, b), 0.0)

    # 9. route B: same embedding → cosine ~1
    def test_route_b_same_embedding_high(self):
        ev = ThreadEvidence()
        emb = [0.5, 0.5, 0.5]
        a = self._ep_dict(embedding=emb)
        b = self._ep_dict(embedding=emb)
        self.assertGreater(ev.route_b(a, b), 0.98)

    # 10. route B: missing embedding → 0
    def test_route_b_missing_embedding_zero(self):
        ev = ThreadEvidence()
        a = self._ep_dict(embedding=None)
        b = self._ep_dict(embedding=[0.5, 0.5, 0.5])
        self.assertEqual(ev.route_b(a, b), 0.0)

    # 11. route C: same day → high score, no conflict
    def test_route_c_same_day_high_no_conflict(self):
        ev = ThreadEvidence()
        ts = time.mktime(time.strptime("2026-07-01", "%Y-%m-%d"))
        a = self._ep_dict(event_ts=ts)
        b = self._ep_dict(event_ts=ts + 3600)  # 1 hour later
        score, conflict = ev.route_c(a, b)
        self.assertGreater(score, 0.8)
        self.assertFalse(conflict)

    # 12. route C: >180 days → date conflict
    def test_route_c_far_apart_conflict(self):
        ev = ThreadEvidence()
        ts_a = time.mktime(time.strptime("2024-01-01", "%Y-%m-%d"))
        ts_b = time.mktime(time.strptime("2026-07-01", "%Y-%m-%d"))
        score, conflict = ev.route_c(self._ep_dict(event_ts=ts_a), self._ep_dict(event_ts=ts_b))
        self.assertLess(score, 0.15)
        self.assertTrue(conflict)

    # 13. route D: shared state_change terms → positive
    def test_route_d_shared_state_change_positive(self):
        ev = ThreadEvidence()
        a = self._ep_dict(state_change="答应了不离开 约定保持")
        b = self._ep_dict(state_change="约定守诺 不离开的承诺")
        self.assertGreater(ev.route_d(a, b), 0.0)

    # 14. same compression batch is co-occurrence, not identity proof
    def test_route_e_same_batch_is_not_full_proof(self):
        ev = ThreadEvidence()
        a = self._ep_dict(source_batch_id="batch-xyz")
        b = self._ep_dict(source_batch_id="batch-xyz")
        self.assertGreater(ev.route_e(a, b), 0.5)
        self.assertLess(ev.route_e(a, b), 0.9)

    # 15. hard constraint: scope mismatch → reject
    def test_hard_constraint_scope_mismatch_reject(self):
        ev = ThreadEvidence()
        a = self._ep_dict(); a["scope_id"] = "scope-a"
        b = self._ep_dict(); b["scope_id"] = "scope-b"
        rejected, reason = ev.hard_constraints(a, b, False)
        self.assertTrue(rejected)
        self.assertEqual(reason, "scope_mismatch")

    # 16. date gap blocks retells only; it may still be a continuation
    def test_hard_constraint_date_conflict_is_not_global_reject(self):
        ev = ThreadEvidence()
        a = self._ep_dict()
        b = self._ep_dict()
        rejected, reason = ev.hard_constraints(a, b, True)
        self.assertFalse(rejected)
        self.assertEqual(reason, "")


class Test1ArbiterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "ep.db"), 3, "unit")
        await self.store.init()
        self.arbiter = ThreadArbiter()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ep(self, eid, **kwargs):
        base = {
            "episode_id": eid, "entities_json": json.dumps(kwargs.get("entities", ["爱莉"])),
            "event_ts": kwargs.get("event_ts", time.mktime(time.strptime("2026-07-01", "%Y-%m-%d"))),
            "state_change": kwargs.get("state_change", ""),
            "long_effect": "", "source_batch_id": kwargs.get("source_batch_id", ""),
            "evidence_quality": kwargs.get("quality", "source_grounded"),
            "unresolved_json": "[]", "scope_id": "",
            "_embedding": kwargs.get("embedding", [0.8, 0.8, 0.8]),
        }
        return base

    # 17. same source batch alone remains ambiguous
    def test_same_batch_does_not_produce_accepted_retells(self):
        a = self._ep("ep-a", source_batch_id="batch-x")
        b = self._ep("ep-b", source_batch_id="batch-x")
        edge = self.arbiter.decide(a, b)
        self.assertIsNotNone(edge)
        self.assertEqual(edge["edge_type"], "parallel")
        self.assertEqual(edge["status"], "provisional")

    # 18. scope mismatch → rejected edge
    def test_scope_mismatch_produces_rejected(self):
        a = self._ep("ep-a"); a["scope_id"] = "scope-A"
        b = self._ep("ep-b"); b["scope_id"] = "scope-B"
        edge = self.arbiter.decide(a, b)
        self.assertIsNotNone(edge)
        self.assertEqual(edge["status"], "rejected")

    # 19. low-score pair → rejected or provisional (not accepted)
    def test_low_score_not_accepted(self):
        a = self._ep("ep-low-a", entities=[], source_batch_id="", quality="diary_derived",
                     event_ts=time.mktime(time.strptime("2020-01-01", "%Y-%m-%d")),
                     embedding=None)
        b = self._ep("ep-low-b", entities=[], source_batch_id="", quality="diary_derived",
                     event_ts=time.mktime(time.strptime("2026-07-01", "%Y-%m-%d")),
                     embedding=None)
        edge = self.arbiter.decide(a, b)
        self.assertIsNotNone(edge)
        self.assertNotEqual(edge["status"], "accepted")

    # 20. insert_edge is idempotent
    def test_insert_edge_idempotent(self):
        a = self._ep("ep-a2", source_batch_id="bx")
        b = self._ep("ep-b2", source_batch_id="bx")
        edge = self.arbiter.decide(a, b)
        self.store.thread_insert_edge(edge)
        self.store.thread_insert_edge(edge)
        edges = self.store.thread_list_edges(episode_id="ep-a2")
        self.assertEqual(len(edges), 1)

    # 21. list_edges returns written edges
    def test_list_edges_returns_written(self):
        a = self._ep("ep-c", source_batch_id="by")
        b = self._ep("ep-d", source_batch_id="by")
        edge = self.arbiter.decide(a, b)
        self.store.thread_insert_edge(edge)
        edges = self.store.thread_list_edges()
        self.assertGreaterEqual(len(edges), 1)

    # 22. edge_counts returns dict
    def test_edge_counts_structure(self):
        counts = self.store.thread_edge_counts()
        self.assertIsInstance(counts, dict)

    # 23. builder process_batch on empty queue returns 0
    async def test_builder_empty_queue_zero(self):
        builder = ThreadBuilder(self.store._threads, self.store._connect, self.store._lock)
        result = builder.process_batch(batch_size=5)
        self.assertEqual(result.get("processed", 0), 0)


if __name__ == "__main__":
    unittest.main()
