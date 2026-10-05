import random
import threading
import time
import unittest
from unittest.mock import patch

from astrbot_plugin_memos_memory.consistency_guard import evaluate
from astrbot_plugin_memos_memory.consistency_service import ConsistencyService
from astrbot_plugin_memos_memory.store_utils import extract_terms
from astrbot_plugin_memos_memory.text_overlap import longest_common_substring
from astrbot_plugin_memos_memory.thread_retrieval import ALGORITHM_VERSION


class IntegratedCoreTests(unittest.TestCase):
    def test_online_terms_do_not_initialize_cold_jieba(self):
        try:
            import jieba
        except ImportError:
            self.skipTest("jieba is not installed in this isolated interpreter")

        with patch.object(jieba.dt, "initialized", False), patch.object(
            jieba, "lcut", side_effect=AssertionError("cold tokenizer entered online path")
        ):
            terms = extract_terms("我们的关系仍然信任", initialize_segmenter=False)
        self.assertIn("关系", terms)
        self.assertIn("信任", terms)

    def test_linear_overlap_matches_reference(self):
        rng = random.Random(6008)
        for _ in range(300):
            left = "".join(rng.choices("abcde天空", k=rng.randrange(28)))
            right = "".join(rng.choices("abcde天空", k=rng.randrange(28)))
            expected = max(
                (end - start for start in range(len(left)) for end in range(start + 1, len(left) + 1)
                 if left[start:end] in right),
                default=0,
            )
            self.assertEqual(longest_common_substring(left, right), expected)

    def test_algorithm_version_identifies_integrated_runtime(self):
        self.assertEqual(ALGORITHM_VERSION, "test8-context-retrieval-v5-integrated")

    def test_structured_date_and_dream_rules_work_together(self):
        references = [
            {
                "episode_id": "date-event", "text": "我们去天台看星星",
                "occurred_at": "2026-08-10 20:00", "evidence_quality": "source_grounded",
            },
            {
                "episode_id": "dream-event", "text": "梦里我们在海边重逢",
                "status": "dream", "memory_type": "dream", "evidence_quality": "source_grounded",
            },
        ]
        result = evaluate({
            "query": "你还记得吗？",
            "answer": "2026年8月11日我们去天台看星星。现实中我们确实在海边重逢。",
            "references": references,
            "thread_text": "2026-08-10 20:00 | 我们去天台看星星\n梦里我们在海边重逢",
            "thread_used": True,
        })
        self.assertEqual(
            {item["error_type"] for item in result["findings"]},
            {"date_conflict"},
        )
        self.assertEqual(
            {item["error_type"] for item in result["candidates"]},
            {"reality_status_conflict"},
        )

    def test_blank_injection_snapshot_is_a_hard_evidence_boundary(self):
        result = evaluate({
            "query": "那天到底发生了什么？",
            "answer": "现实中我们在2026年8月11日去了海边。",
            "references": [{
                "episode_id": "hidden-reference",
                "text": "梦里我们在海边重逢",
                "occurred_at": "2026-08-10 20:00",
                "status": "dream",
                "memory_type": "dream",
                "evidence_quality": "source_grounded",
            }],
            "thread_text": "",
            "thread_used": True,
        })
        self.assertEqual(result["checked_references"], 0)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["candidates"], [])


class RecordingStore:
    def __init__(self):
        self.thread_names = []
        self.rows = []

    def thread_record_request_observation(self, request_id, **fields):
        self.thread_names.append(threading.current_thread().name)
        self.rows.append((request_id, fields))

    def thread_record_consistency_result(self, request_id, findings, **fields):
        self.thread_names.append(threading.current_thread().name)
        self.rows.append((request_id, fields))


class AsyncObservationTests(unittest.TestCase):
    def test_observation_database_write_runs_on_service_worker(self):
        store = RecordingStore()
        service = ConsistencyService(store, evaluator=lambda payload: {"findings": []})
        try:
            caller = threading.current_thread().name
            self.assertTrue(service.submit_observation({"request_id": "req-async", "scope_id": "role"}))
            self.assertTrue(service.wait(1.0))
            self.assertEqual(store.rows[0][0], "req-async")
            self.assertNotEqual(store.thread_names[0], caller)
            self.assertEqual(store.thread_names[0], "memos-consistency")
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
