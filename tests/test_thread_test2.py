# -*- coding: utf-8 -*-
"""6.0.0-test2 arbitration, projection, scope, and reversibility tests."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.thread_arbiter import ThreadArbiter
from astrbot_plugin_memos_memory.thread_candidates import ThreadCandidates
from astrbot_plugin_memos_memory.thread_eval import evaluate_relation_predictions
from astrbot_plugin_memos_memory.thread_llm_arbiter import ThreadLLMArbiter
from astrbot_plugin_memos_memory.thread_projector import ThreadProjector
from astrbot_plugin_memos_memory.thread_webui import ThreadWebUIHandlers


def episode(name, *, ts=0, entities=None, batch="", card="天台约定", kind="plot_fact"):
    return {
        "memo_name": name, "occurred_at": "2026-07-01 晚上", "event_ts": ts or time.time(),
        "time_basis": "source_turn", "memory_type": kind, "importance": 3,
        "scene_anchor": "天台", "retrieval_key": "银戒指 天台",
        "state_change": "关系更加信任", "long_effect": "仍记得承诺", "trigger_hint": "银戒指",
        "entities": entities if entities is not None else ["爱莉"], "unresolved": [],
        "card_text": card, "evidence_quality": "source_grounded" if batch else "diary_derived",
        "source_batch_id": batch, "source_updated_ts": 0.0,
    }


class ThreadTest2StoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "memory.db"), 3, "unit")
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, item, scope="role-a"):
        if item.get("source_batch_id"):
            now = time.time()
            self.store._connect().execute(
                """INSERT OR IGNORE INTO source_batches(batch_id,session_id,source_kind,
                   content_hash,message_count,first_event_ts,last_event_ts,status,created_ts,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item["source_batch_id"], "unit", "test", item["source_batch_id"], 1,
                 item["event_ts"], item["event_ts"], "archived", now, now),
            )
            self.store._connect().commit()
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item, card_text=item["card_text"],
            embedding=[0.7, 0.7, 0.7], evidence_quality=item["evidence_quality"],
            source_batch_id=item.get("source_batch_id", ""),
        )
        value = self.store.get_episode(item["memo_name"])
        eid = str(value["episode_id"])
        self.store.thread_enqueue(scope, eid)
        return eid

    def edge(self, a, b, **kwargs):
        payload = {
            "scope_id": "role-a", "source_episode_id": a, "target_episode_id": b,
            "edge_type": "parallel", "direction": "none", "confidence": 0.7,
            "status": "provisional", "evidence_json": "{}", "counter_evidence_json": "[]",
            "route_sources_json": "[]", "arbiter_version": "test2-local-v1",
        }
        payload.update(kwargs)
        return self.store._threads.upsert_edge(payload)

    def test_schema_contains_test2_tables_and_columns(self):
        conn = self.store._connect()
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("thread_episode_scopes", "thread_episode_terms", "thread_arbitration_jobs",
                      "thread_arbitration_cache", "thread_operations"):
            self.assertIn(table, tables)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_episode_edges)")}
        self.assertTrue({"scope_id", "decision_source", "decision_json", "content_hash"} <= columns)

    def test_scope_isolation_in_candidates(self):
        a = self.add(episode("memos/a"), "role-a")
        self.add(episode("memos/b"), "role-b")
        ids = {value for pair in ThreadCandidates(self.store._connect).candidates_for(a, "role-a")
               for value in (pair["episode_id_a"], pair["episode_id_b"])}
        self.assertEqual(ids, set())

    def test_candidate_ranking_keeps_strong_route_before_date_only(self):
        now = time.time()
        anchor = self.add(episode("memos/anchor", ts=now, batch="batch-x"))
        strong = self.add(episode("memos/strong", ts=now, batch="batch-x"))
        self.add(episode("memos/date", ts=now + 86400, entities=[], card="完全不同"))
        pairs = ThreadCandidates(self.store._connect).candidates_for(anchor, "role-a")
        self.assertTrue(pairs)
        self.assertIn(strong, {pairs[0]["episode_id_a"], pairs[0]["episode_id_b"]})
        self.assertIn("same_source_batch", pairs[0]["blocking_reasons"])

    def test_pair_upsert_changes_relation_without_duplicate(self):
        a, b = self.add(episode("memos/a")), self.add(episode("memos/b"))
        first = self.edge(a, b)
        second = self.edge(b, a, edge_type="continues", direction="directed", status="accepted")
        rows = self.store.thread_list_edges(scope_id="role-a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["edge_type"], "continues")
        self.assertEqual(first["edge_id"], second["edge_id"])

    def test_manual_lock_blocks_automatic_overwrite(self):
        a, b = self.add(episode("memos/a")), self.add(episode("memos/b"))
        stored = self.edge(a, b)
        result = self.store.thread_manual_edge_decision(stored["edge_id"], {
            "decision": "accepted", "relation": "continues", "direction": "a_to_b", "confidence": 1,
        })
        blocked = self.edge(a, b, edge_type="unrelated_similar", status="rejected")
        self.assertTrue(blocked["locked"])
        self.assertTrue(result["operation_id"])
        self.assertEqual(self.store.thread_list_edges()[0]["edge_type"], "continues")

    def test_manual_edge_operation_reverts(self):
        a, b = self.add(episode("memos/a")), self.add(episode("memos/b"))
        stored = self.edge(a, b)
        result = self.store.thread_manual_edge_decision(stored["edge_id"], {
            "decision": "accepted", "relation": "continues", "direction": "a_to_b", "confidence": 1,
        })
        self.store.thread_revert_operation(result["operation_id"])
        edge = self.store.thread_list_edges()[0]
        self.assertEqual(edge["status"], "provisional")
        self.assertEqual(edge["manual_lock"], 0)

    def test_builder_queue_retries_before_quarantine(self):
        self.store.thread_enqueue("role-a", "missing")
        batch = self.store.thread_take_pending_batch(1)
        self.store.thread_mark_completed(batch[0]["id"], error="boom", max_retries=1)
        self.assertEqual(self.store.thread_queue_counts().get("pending"), 1)
        conn = self.store._connect()
        conn.execute("UPDATE thread_build_queue SET next_attempt_ts=0")
        conn.commit()
        batch = self.store.thread_take_pending_batch(1)
        self.store.thread_mark_completed(batch[0]["id"], error="boom", max_retries=1)
        self.assertEqual(self.store.thread_queue_counts().get("quarantined"), 1)

    def test_exact_turn_overlap_can_auto_accept_retell(self):
        base = {
            "entities_json": json.dumps(["爱莉"]), "event_ts": time.time(),
            "state_change": "", "long_effect": "", "source_batch_id": "batch",
            "evidence_quality": "source_grounded", "unresolved_json": "[]",
            "scope_id": "role-a", "_embedding": [1.0, 0.0],
            "_turn_refs": [("batch", 2)],
        }
        a, b = dict(base, episode_id="a"), dict(base, episode_id="b")
        edge = ThreadArbiter().decide(a, b, scope_id="role-a")
        self.assertEqual(edge["status"], "accepted")
        self.assertEqual(edge["edge_type"], "retells")

    async def test_llm_valid_decision_updates_edge(self):
        a, b = self.add(episode("memos/a")), self.add(episode("memos/b", ts=time.time() + 60))
        stored = self.edge(a, b)
        self.store._threads.ensure_arbitration_job(stored["edge_id"], "role-a")

        async def fake(_prompt):
            return json.dumps([{"edge_id": stored["edge_id"], "decision": "accepted",
                "relation": "continues", "direction": "a_to_b", "confidence": .93,
                "supporting_evidence": ["time order"], "counterevidence": [],
                "requires_source_verification": False, "reason_code": "continuity"}])

        result = await ThreadLLMArbiter(self.store._threads, fake, daily_budget=10).process_pending()
        self.assertEqual(result["processed"], 1)
        edge = self.store.thread_list_edges()[0]
        self.assertEqual(edge["status"], "accepted")
        self.assertEqual(edge["decision_source"], "llm")

    async def test_llm_invalid_json_fails_open_and_retries(self):
        a, b = self.add(episode("memos/a")), self.add(episode("memos/b"))
        stored = self.edge(a, b)
        self.store._threads.ensure_arbitration_job(stored["edge_id"], "role-a")

        async def bad(_prompt):
            return "not json"

        result = await ThreadLLMArbiter(self.store._threads, bad, max_retries=0, daily_budget=10).process_pending()
        self.assertGreater(result["errors"], 0)
        edge = self.store.thread_list_edges()[0]
        self.assertEqual(edge["status"], "provisional")

    async def test_llm_cache_avoids_provider_call(self):
        a, b = self.add(episode("memos/a")), self.add(episode("memos/b", ts=time.time() + 60))
        stored = self.edge(a, b)
        self.store._threads.ensure_arbitration_job(stored["edge_id"], "role-a")
        calls = 0

        async def fake(_prompt):
            nonlocal calls
            calls += 1
            return json.dumps([{"edge_id": stored["edge_id"], "decision": "uncertain",
                "relation": "parallel", "direction": "none", "confidence": .6,
                "supporting_evidence": [], "counterevidence": [],
                "requires_source_verification": False, "reason_code": "weak"}])

        arbiter = ThreadLLMArbiter(self.store._threads, fake, daily_budget=10)
        await arbiter.process_pending()
        self.assertEqual(calls, 1)
        cache = self.store._connect().execute("SELECT COUNT(*) AS n FROM thread_arbitration_cache").fetchone()["n"]
        self.assertEqual(cache, 1)

    def test_projector_creates_ordered_thread(self):
        now = time.time()
        a = self.add(episode("memos/a", ts=now))
        b = self.add(episode("memos/b", ts=now + 60))
        self.edge(a, b, edge_type="continues", direction="directed", status="accepted", confidence=.94)
        result = ThreadProjector(self.store._threads).project(scope_id="role-a")
        self.assertEqual(result["threads_projected"], 1)
        threads = self.store.thread_list_threads(scope_id="role-a")
        detail = self.store.thread_detail(threads[0]["thread_id"])
        self.assertEqual(len(detail["members"]), 2)
        self.assertLess(detail["members"][0]["sequence_no"], detail["members"][1]["sequence_no"])

    def test_projection_optimistic_lock_rejects_stale_write(self):
        payload = {"thread_id": "t", "scope_id": "role-a", "thread_type": "narrative_arc", "title": "x"}
        self.store._threads.replace_thread_projection(payload, [], expected_version=0)
        with self.assertRaises(RuntimeError):
            self.store._threads.replace_thread_projection(payload, [], expected_version=0)

    def test_merge_and_split_reject_stale_versions(self):
        from astrbot_plugin_memos_memory.thread_store import ThreadVersionConflict
        now = time.time()
        first = self.add(episode("memos/merge-a", ts=now))
        second = self.add(episode("memos/merge-b", ts=now + 1))
        third = self.add(episode("memos/merge-c", ts=now + 2))
        t1 = self.store._threads.replace_thread_projection(
            {"thread_id": "manual-a", "scope_id": "role-a", "thread_type": "other", "title": "A", "status": "active"},
            [{"episode_id": first}], expected_version=0,
        )
        t2 = self.store._threads.replace_thread_projection(
            {"thread_id": "manual-b", "scope_id": "role-a", "thread_type": "other", "title": "B", "status": "active"},
            [{"episode_id": second}, {"episode_id": third}], expected_version=0,
        )
        preview = self.store.thread_merge_preview(["manual-a", "manual-b"])
        self.store._threads.replace_thread_projection(
            {"thread_id": "manual-a", "scope_id": "role-a", "thread_type": "other", "title": "A2", "status": "active"},
            [{"episode_id": first}], expected_version=1,
        )
        with self.assertRaises(ThreadVersionConflict):
            self.store.thread_apply_merge(["manual-a", "manual-b"], expected_versions=preview["versions"])
        split = self.store.thread_split_preview("manual-b", [second])
        self.store._threads.replace_thread_projection(
            {"thread_id": "manual-b", "scope_id": "role-a", "thread_type": "other", "title": "B2", "status": "active"},
            [{"episode_id": second}, {"episode_id": third}], expected_version=1,
        )
        with self.assertRaises(ThreadVersionConflict):
            self.store.thread_apply_split("manual-b", [second], expected_version=split["version"])

    def test_split_and_revert_are_lossless(self):
        now = time.time()
        ids = [self.add(episode(f"memos/{i}", ts=now + i)) for i in range(3)]
        self.edge(ids[0], ids[1], edge_type="continues", status="accepted", confidence=.95)
        self.edge(ids[1], ids[2], edge_type="continues", status="accepted", confidence=.95)
        ThreadProjector(self.store._threads).project(scope_id="role-a")
        thread = self.store.thread_list_threads()[0]
        result = self.store.thread_apply_split(thread["thread_id"], [ids[2]])
        self.assertEqual(len(self.store.thread_list_threads(status="active")), 2)
        self.store.thread_revert_operation(result["operation_id"])
        self.assertEqual(len(self.store.thread_detail(thread["thread_id"])["members"]), 3)

    def test_webui_resume_handler_is_a_class_method(self):
        self.assertTrue(callable(getattr(ThreadWebUIHandlers(), "_api_threads_resume", None)))


class ThreadTest2EvalCase(unittest.TestCase):
    def test_four_required_classes_and_three_engine_comparison(self):
        labels = [
            ("same_event_retell", "accepted", "retells"),
            ("same_theme_distinct", "accepted", "parallel"),
            ("continuation_response", "accepted", "continues"),
            ("uncertain", "uncertain", "parallel"),
        ]
        cases = []
        for kind, decision, relation in labels:
            prediction = {"decision": decision, "relation": relation}
            cases.append({"case_type": kind, "expected_decision": decision,
                          "expected_relation": relation, "local": prediction,
                          "llm": prediction, "fused": prediction})
        result = evaluate_relation_predictions(cases)
        self.assertEqual(result["missing_case_types"], [])
        self.assertEqual(result["engines"]["fused"]["accuracy"], 1.0)
        self.assertTrue(result["engines"]["fused"]["test3_gate"])


if __name__ == "__main__":
    unittest.main()
