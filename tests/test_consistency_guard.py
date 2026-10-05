# -*- coding: utf-8 -*-
"""6.0.0-test8 consistency guard contract tests."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from astrbot_plugin_memos_memory.consistency_guard import evaluate
from astrbot_plugin_memos_memory.consistency_service import ConsistencyService


class ConsistencyGuardRuleTests(unittest.TestCase):
    def check(self, ref, answer, **extra):
        snapshot = {"query": "这段经历现在是什么状态？", "answer": answer,
                    "references": [ref], "thread_text": ref.get("text", "")}
        snapshot.update(extra)
        return evaluate(snapshot)

    def test_date_conflict(self):
        result = self.check({"id": "e1", "text": "这件事发生在2024年3月12日", "status": "historical"},
                            "这件事发生在2025年3月12日")
        self.assertTrue(any(x["error_type"] == "date_conflict" for x in result["findings"]))

    def test_year_only_is_compatible_with_precise_date(self):
        result = self.check({"id": "e1", "text": "这件事发生在2024年3月12日"}, "这件事发生在2024年")
        self.assertFalse(any(x["error_type"] == "date_conflict" for x in result["findings"]))

    def test_historical_as_current(self):
        result = self.check({"id": "e1", "text": "以前我们关系疏远", "status": "historical"},
                            "现在我们关系疏远")
        self.assertTrue(any(x["error_type"] == "historical_as_current" for x in result["findings"]))

    def test_historical_quotation_is_not_flagged(self):
        result = self.check({"id": "e1", "text": "以前我们关系疏远", "status": "historical"},
                            "过去我们关系疏远，但现在已经信任")
        self.assertFalse(any(x["error_type"] == "historical_as_current" for x in result["findings"]))

    def test_dream_reality_conflict(self):
        result = self.check({"id": "e1", "text": "梦里我们在海边重逢", "status": "dream"},
                            "现实中我们确实在海边重逢")
        self.assertFalse(result["findings"])
        self.assertTrue(any(x["error_type"] == "reality_status_conflict" for x in result["candidates"]))

    def test_structured_occurred_at_is_checked(self):
        ref = {
            "id": "e-date", "text": "我们去天台看星星",
            "occurred_at": "2024年3月12日", "status": "historical",
        }
        result = evaluate({
            "query": "那天是什么时候？",
            "answer": "2025年3月12日我们去天台看星星。",
            "references": [ref],
            "thread_text": "2024年3月12日 | 我们去天台看星星",
        })
        self.assertTrue(any(x["error_type"] == "date_conflict" for x in result["findings"]))

    def test_structured_date_must_have_been_injected(self):
        ref = {
            "id": "e-hidden-date", "text": "我们去天台看星星",
            "occurred_at": "2024年3月12日", "status": "historical",
        }
        result = evaluate({
            "query": "那天是什么时候？",
            "answer": "2025年3月12日我们去天台看星星。",
            "references": [ref],
            "thread_text": "我们去天台看星星",
        })
        self.assertFalse(any(x["error_type"] == "date_conflict" for x in result["findings"]))

    def test_empty_thread_text_never_exposes_references(self):
        references = [
            {
                "id": "e-hidden-date",
                "text": "我们去天台看星星",
                "occurred_at": "2024年3月12日",
                "status": "historical",
            },
            {
                "id": "e-hidden-dream",
                "text": "梦里我们在海边重逢",
                "status": "dream",
                "memory_type": "dream",
            },
        ]
        for thread_text in ("", "   \n\t"):
            with self.subTest(thread_text=repr(thread_text)):
                result = evaluate({
                    "query": "这些事情是真的吗？",
                    "answer": "2025年3月12日我们去天台看星星，现实中也确实在海边重逢。",
                    "references": references,
                    "thread_text": thread_text,
                    "thread_used": True,
                })
                self.assertEqual(result["checked_references"], 0)
                self.assertEqual(result["findings"], [])
                self.assertEqual(result["candidates"], [])

    def test_quoted_date_is_not_a_current_assertion(self):
        ref = {
            "id": "e-quote-date", "text": "我们去天台看星星",
            "occurred_at": "2024年3月12日", "status": "historical",
        }
        result = evaluate({
            "query": "那天是什么时候？",
            "answer": "她问：“是不是2025年3月12日我们去天台看星星？”",
            "references": [ref],
            "thread_text": "2024年3月12日 | 我们去天台看星星",
        })
        self.assertFalse(any(x["error_type"] == "date_conflict" for x in result["findings"]))

    def test_hypothetical_prefix_date_is_not_flagged(self):
        ref = {
            "id": "e-soft-date", "text": "我们去天台看星星",
            "occurred_at": "2024年3月12日", "status": "historical",
        }
        result = evaluate({
            "query": "那天是什么时候？",
            "answer": "是不是2025年3月12日我们去天台看星星？",
            "references": [ref],
            "thread_text": "2024年3月12日 | 我们去天台看星星",
        })
        self.assertFalse(any(x["error_type"] == "date_conflict" for x in result["findings"]))

    def test_resolved_pending_conflict(self):
        result = self.check({"id": "e1", "text": "周五的约定已经完成", "status": "resolved"},
                            "周五的约定仍未完成")
        self.assertTrue(any(x["error_type"] == "resolved_pending_conflict" for x in result["findings"]))

    def test_unsupported_causality_is_candidate_only(self):
        result = self.check({"id": "e1", "text": "我们后来在雨里见面", "status": "active"},
                            "因为下雨，所以我们见面")
        self.assertTrue(result["candidates"])
        self.assertFalse(any(x["error_type"] == "unsupported_causality" for x in result["findings"]))

    def test_uncertain_and_hypothetical_answer_is_not_flagged(self):
        result = self.check({"id": "e1", "text": "我们已经完成约定", "status": "active"},
                            "也许我们已经完成约定")
        self.assertFalse(result["findings"])

    def test_unrelated_answer_is_not_flagged(self):
        result = self.check({"id": "e1", "text": "银戒指在抽屉里", "status": "active"}, "今天天气很好")
        self.assertFalse(result["findings"])

    def test_confidence_is_numeric_for_sql(self):
        result = self.check({"id": "e1", "text": "这件事发生在2024年3月12日"},
                            "这件事发生在2025年3月12日")
        self.assertIsInstance(result["findings"][0]["confidence"], (int, float))


class ConsistencyServiceContractTests(unittest.TestCase):
    class Store:
        def __init__(self):
            self.rows = []
            self.requests = {}
            self.lock = threading.Lock()

        def thread_record_consistency_observations(self, request_id, observations, scope_id="default"):
            with self.lock:
                self.rows.extend({"request_id": request_id, **x} for x in observations)

        def thread_record_request_observation(self, request_id, **fields):
            with self.lock:
                self.requests.setdefault(request_id, {}).update(fields)

        def thread_record_consistency_feedback(self, request_id, label, **fields):
            with self.lock:
                if request_id not in self.requests:
                    raise KeyError("request observation not found")
                self.requests[request_id]["feedback"] = {"label": label, **fields}
                return 1

        def thread_list_consistency_feedback(self, request_id="", **kwargs):
            item = self.requests.get(request_id, {}).get("feedback")
            return [item] if item else []

    def test_pair_is_persisted_after_response(self):
        store = self.Store()
        service = ConsistencyService(store, capacity=2, timeout=1.0, ttl=2.0)
        try:
            evidence = [{"id": "e1", "text": "这件事发生在2024年3月12日", "status": "active"}]
            self.assertTrue(service.submit_snapshot({"request_id": "r1", "query": "日期是什么？", "references": evidence, "thread_text": evidence[0]["text"]}))
            self.assertTrue(service.submit_response({"request_id": "r1", "answer": "这件事发生在2025年3月12日"}))
            self.assertTrue(service.wait(2.0))
            self.assertTrue(any(x.get("error_type") == "date_conflict" for x in store.rows))
            self.assertEqual(store.requests["r1"].get("consistency_status"), "flagged")
        finally:
            self.assertTrue(service.close(2.0))

    def test_response_before_snapshot_pairs_and_duplicates_are_idempotent(self):
        store = self.Store()
        service = ConsistencyService(store, capacity=4, timeout=1.0, ttl=2.0)
        try:
            response = {"request_id": "r2", "answer": "这件事发生在2025年3月12日"}
            evidence = [{"id": "e1", "text": "这件事发生在2024年3月12日", "status": "active"}]
            snapshot = {"request_id": "r2", "query": "日期是什么？", "references": evidence,
                        "thread_text": evidence[0]["text"]}
            self.assertTrue(service.submit_response(response))
            self.assertTrue(service.submit_response(response))
            self.assertTrue(service.submit_snapshot(snapshot))
            self.assertTrue(service.submit_snapshot(snapshot))
            self.assertTrue(service.wait(2.0))
            self.assertEqual(sum(x.get("error_type") == "date_conflict" for x in store.rows), 1)
        finally:
            self.assertTrue(service.close(2.0))

    def test_feedback_uses_storage_contract(self):
        store = self.Store()
        store.thread_record_request_observation("r3", query_text="q")
        service = ConsistencyService(store, capacity=1)
        try:
            self.assertEqual(service.feedback("r3", "false_positive", note="checked"), 1)
            self.assertEqual(service.feedback_history("r3")[0]["label"], "false_positive")
            with self.assertRaises(ValueError):
                service.feedback("r3", "invalid")
        finally:
            self.assertTrue(service.close(2.0))

    def test_anonymous_export_has_no_sensitive_fields_or_stable_sample(self):
        class ExportStore:
            def thread_list_consistency_observations(self, **kwargs):
                return [{"id": 42, "request_id": "secret-request", "query": "secret query", "answer": "secret answer",
                         "evidence": "secret evidence", "source_id": "secret-source", "error_type": "date_conflict",
                         "severity": "high", "confidence": .9}]
        service = ConsistencyService(ExportStore(), capacity=1)
        try:
            first = service.export(limit=1)
            second = service.export(limit=1)
            self.assertEqual(len(first), 1)
            self.assertNotEqual(first[0]["sample"], second[0]["sample"])
            dumped = json.dumps(first[0], ensure_ascii=False)
            for secret in ("secret-request", "secret query", "secret answer", "secret evidence", "secret-source"):
                self.assertNotIn(secret, dumped)
        finally:
            self.assertTrue(service.close(2.0))

    def test_finding_metadata_and_zero_confidence_round_trip(self):
        from astrbot_plugin_memos_memory.thread_store import ThreadStore
        with tempfile.TemporaryDirectory() as temp:
            conn = sqlite3.connect(str(Path(temp) / "thread.db"))
            conn.row_factory = sqlite3.Row
            lock = threading.RLock()
            store = ThreadStore(lambda: conn, lock)
            try:
                store.init_schema(conn)
                conn.commit()
                store.record_request_observation("r4", query_text="q")
                count = store.record_thread_consistency_observations("r4", [{
                    "error_type": "date_conflict", "description": "d", "confidence": 0.0,
                    "severity": "medium", "rule_id": "CG-date", "decision_source": "rule",
                    "answer_excerpt": "a", "response_excerpt": "a", "source_quality": "source",
                    "guard_version": "8",
                    "evidence": {"id": "e4", "text": "e"},
                }])
                self.assertEqual(count, 1)
                row = store.list_thread_consistency_observations(request_id="r4")[0]
                self.assertEqual(row["confidence"], 0.0)
                self.assertEqual(row["rule_id"], "CG-date")
                self.assertEqual(row["decision_source"], "rule")
                self.assertEqual(row["answer_excerpt"], "a")
            finally:
                conn.close()

    def test_sqlite_snapshot_response_finding_and_feedback_round_trip(self):
        from astrbot_plugin_memos_memory.episodic_store import EpisodicStore

        with tempfile.TemporaryDirectory() as temp:
            store = EpisodicStore(str(Path(temp) / "episodic.db"), 3, "unit")
            asyncio.run(store.init())
            try:
                evidence = [{
                    "id": "e2",
                    "source_id": "source-2",
                    "text": "项目在2024年3月12日完成",
                    "status": "active",
                    "category": "factual",
                }]
                service = store.consistency_service
                self.assertIsNotNone(service)
                self.assertTrue(service.submit_snapshot({
                    "request_id": "r5",
                    "scope_id": "role-e2e",
                    "query": "项目什么时候完成？",
                    "references": evidence,
                    "thread_text": evidence[0]["text"],
                    "snapshot_complete": True,
                    "thread_used": True,
                }))
                self.assertTrue(service.submit_response({
                    "request_id": "r5",
                    "answer": "项目在2025年3月12日完成",
                    "response_status": "completed",
                    "chunk_status": "final",
                }))
                self.assertTrue(service.wait(2.0))
                observation = store.thread_request_observation("r5")
                self.assertEqual(observation["query_text"], "项目什么时候完成？")
                self.assertEqual(observation["response_status"], "completed")
                self.assertEqual(observation["chunk_status"], "final")
                self.assertEqual(observation["observation_status"], "checked")
                self.assertEqual(observation["consistency_status"], "flagged")
                self.assertEqual(observation["answer_chars"], len("项目在2025年3月12日完成"))
                findings = store.thread_list_consistency_observations(request_id="r5")
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["rule_id"], "CG-date_conflict")
                self.assertEqual(findings[0]["decision_source"], "answer_vs_injected_reference")
                self.assertEqual(findings[0]["evidence"]["source_id"], "source-2")
                self.assertEqual(findings[0]["confidence"], 0.9)
                self.assertEqual(service.feedback("r5", "uncertain", note="needs review"), 1)
                self.assertEqual(service.feedback_history("r5")[0]["label"], "uncertain")
                store.thread_record_consistency_result(
                    "r-clean", [], scope_id="role-e2e",
                    observation_status="checked", consistency_status="clean",
                    snapshot_complete=1, response_status="completed",
                )
                store.thread_record_consistency_result(
                    "r-missing", [], scope_id="role-e2e",
                    observation_status="skipped", consistency_status="skipped",
                    skip_reason="missing_response", snapshot_complete=1,
                )
                overview = store.thread_consistency_request_overview(
                    scope_id="role-e2e", limit=10,
                )
                self.assertEqual(overview["summary"]["requests"], 3)
                self.assertEqual(overview["summary"]["status"]["flagged"], 1)
                self.assertEqual(overview["summary"]["status"]["clean"], 1)
                self.assertEqual(overview["summary"]["status"]["skipped"], 1)
                self.assertEqual(overview["summary"]["skip_reason"]["missing_response"], 1)
                self.assertEqual(overview["summary"]["findings"], 1)
                self.assertEqual(overview["summary"]["feedback"]["uncertain"], 1)
                flagged = next(item for item in overview["items"] if item["request_id"] == "r5")
                self.assertEqual(flagged["finding_count"], 1)
                self.assertEqual(flagged["feedback_count"], 1)
                self.assertEqual(flagged["latest_feedback"], "uncertain")
                detail = store.thread_consistency_detail("r5")
                self.assertEqual(detail["observation"]["consistency_status"], "flagged")
                self.assertEqual(len(detail["findings"]), 1)
                self.assertEqual(len(detail["feedback"]), 1)
                exported = service.export(request_id="r5", limit=10)
                self.assertEqual(exported[0]["label"], "uncertain")
                self.assertNotIn("source-2", json.dumps(exported, ensure_ascii=False))
            finally:
                store.close()

    def test_terminal_request_observation_cannot_regress(self):
        from astrbot_plugin_memos_memory.thread_store import ThreadStore
        with tempfile.TemporaryDirectory() as temp:
            conn = sqlite3.connect(str(Path(temp) / "thread.db"))
            conn.row_factory = sqlite3.Row
            store = ThreadStore(lambda: conn, threading.RLock())
            try:
                store.init_schema(conn)
                store.record_request_observation(
                    "r-terminal", observation_status="skipped", consistency_status="skipped",
                    skip_reason="missing_response", terminal=1,
                )
                store.record_request_observation(
                    "r-terminal", observation_status="pending", consistency_status="pending",
                    skip_reason="", terminal=0,
                )
                row = store.request_observation("r-terminal")
                self.assertEqual(row["terminal"], 1)
                self.assertEqual(row["observation_status"], "pending")
                self.assertEqual(row["consistency_status"], "pending")
            finally:
                conn.close()

    def test_stream_chunks_assemble_without_duplicate_cumulative_text(self):
        from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._consistency_response_buffers = {}
        self.assertEqual(plugin._consistency_response_text("r-stream", "第一段", is_chunk=True), "第一段")
        self.assertEqual(plugin._consistency_response_text("r-stream", "第二段", is_chunk=True), "第一段第二段")
        self.assertEqual(plugin._consistency_response_text("r-stream", "", is_chunk=False), "第一段第二段")
        plugin._consistency_response_buffers = {}
        plugin._consistency_response_text("r-cumulative", "第一个", is_chunk=True)
        plugin._consistency_response_text("r-cumulative", "第一个第二个", is_chunk=True)
        self.assertEqual(plugin._consistency_response_text("r-cumulative", "", is_chunk=False), "第一个第二个")
        plugin._consistency_response_buffers = {}
        self.assertEqual(plugin._consistency_response_text("r-empty", "", is_chunk=False), "")

    def test_close_persists_unpaired_and_late_response_cannot_resurrect_it(self):
        store = self.Store()
        service = ConsistencyService(store, capacity=2, ttl=30.0)
        self.assertTrue(service.submit_snapshot({
            "request_id": "r-close",
            "query": "还记得吗？",
            "references": [],
            "snapshot_complete": True,
        }))
        self.assertTrue(service.wait(2.0))
        self.assertTrue(service.close(2.0))
        row = store.requests["r-close"]
        self.assertEqual(row["observation_status"], "skipped")
        self.assertEqual(row["skip_reason"], "missing_response")
        self.assertEqual(row["terminal"], 1)
        self.assertFalse(service.submit_response({
            "request_id": "r-close", "answer": "晚到回答",
        }))
        self.assertEqual(store.requests["r-close"]["skip_reason"], "missing_response")

    def test_incomplete_snapshot_is_visible_as_terminal_skip(self):
        store = self.Store()
        service = ConsistencyService(store, capacity=1)
        try:
            self.assertFalse(service.submit_snapshot({
                "request_id": "r-incomplete",
                "query": "q",
                "snapshot_complete": False,
            }))
            row = store.requests["r-incomplete"]
            self.assertEqual(row["skip_reason"], "incomplete_snapshot")
            self.assertEqual(row["snapshot_complete"], 0)
            self.assertEqual(row["terminal"], 1)
        finally:
            self.assertTrue(service.close(2.0))

    def test_seen_and_pending_are_bounded(self):
        store = self.Store()
        service = ConsistencyService(store, capacity=1, ttl=.05)
        try:
            for i in range(20):
                service.submit_snapshot({"request_id": f"r{i}", "query": "q", "references": []})
            time.sleep(.08)
            service.stats()
            self.assertLessEqual(service.stats()["seen"], 20)
            self.assertLessEqual(service.stats()["pending"], 1)
        finally:
            self.assertTrue(service.close(2.0))


if __name__ == "__main__":
    unittest.main()
