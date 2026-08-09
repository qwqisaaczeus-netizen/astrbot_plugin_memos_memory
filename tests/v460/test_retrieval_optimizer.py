from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
from astrbot_plugin_memos_memory.retrieval_optimizer import (
    classify_memory_intent,
    intent_target,
    optimize_fused_hits,
    parse_temporal_constraint,
)


class RetrievalOptimizerTests(unittest.TestCase):
    def test_temporal_parser_handles_cross_year_winter_and_ordinal(self):
        value = parse_temporal_constraint(
            "去年冬天第一次说起搬家的事",
            datetime(2026, 8, 9, 12, 0),
        )
        self.assertTrue(value.active)
        self.assertEqual(value.ordinal, "first")
        self.assertEqual(value.ranges[0][0].isoformat(), "2025-12-01")
        self.assertEqual(value.ranges[0][1].isoformat(), "2026-02-28")

    def test_cross_layer_support_rewards_consensus_without_punishing_old_memory(self):
        hits = [
            {
                "memo_name": "memos/new",
                "chunk_text": "公园里的约定",
                "retrieval_key": "公园约定",
                "_passage_hit": True,
                "_event_card_hit": True,
                "_source_evidence_hit": True,
                "_route_evidence": [
                    {"route": "passage_hybrid", "relevance": 0.8},
                    {"route": "event_card", "relevance": 0.7},
                    {"route": "source_turn", "relevance": 0.75},
                ],
                "_source_turn_hits": [{"content": "我们在公园答应彼此", "relevance": 0.8, "exact_link": True}],
            },
            {"memo_name": "memos/old", "chunk_text": "旧日记里的一件别的事"},
        ]
        diag = optimize_fused_hits("公园约定", hits, {"intent": "specific"})
        self.assertEqual(diag["cross_layer_candidates"], 1)
        self.assertGreater(hits[0]["_retrieval_bonus"], 0.05)
        self.assertEqual(hits[1]["_retrieval_bonus"], 0.0)

    def test_explicit_date_and_previous_select_the_expected_candidates(self):
        hits = [
            {"memo_name": "a", "event_ts": datetime(2026, 7, 1).timestamp()},
            {"memo_name": "b", "event_ts": datetime(2026, 7, 2).timestamp()},
            {"memo_name": "c", "event_ts": datetime(2026, 7, 3).timestamp()},
        ]
        optimize_fused_hits("上一次见面", hits, {"intent": "temporal"}, reference_now=datetime(2026, 8, 9))
        self.assertTrue(hits[1]["_temporal_constraint_match"])
        self.assertFalse(bool(hits[0].get("_temporal_constraint_match")))
        self.assertFalse(bool(hits[2].get("_temporal_constraint_match")))

        exact = [{"memo_name": "x", "event_ts": datetime(2026, 7, 18).timestamp()}]
        optimize_fused_hits("2026年7月18日发生了什么", exact, {"intent": "temporal"})
        self.assertTrue(exact[0]["_temporal_constraint_match"])

    def test_intent_profiles_adjust_targets_within_bounds(self):
        self.assertEqual(classify_memory_intent("我们现在是什么关系"), "current_state")
        self.assertEqual(classify_memory_intent("从头讲讲我们的故事"), "narrative")
        self.assertEqual(intent_target("narrative", 5, 9, 1), 9)
        self.assertEqual(intent_target("current_state", 5, 9, 1), 4)
        self.assertEqual(intent_target("specific_event", 6, 9, 1), 6)


class RecallObservationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tempdir.name) / "memory.db"), None, None)
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    async def test_observation_round_trip_and_feedback(self):
        result = self.store.record_recall_observation({
            "request_id": "req-1", "query": "还记得公园吗", "intent": "specific_event",
            "safety_triggered": True, "safety_reason": "weak_selection",
            "rescue_candidates": 5, "rescue_added": 2,
            "selected_before": [], "selected_after": ["memos/a"],
            "rescue_selected": ["memos/a"], "outcome": "helped",
        })
        self.assertEqual(result["request_id"], "req-1")
        self.assertTrue(self.store.set_recall_observation_feedback("req-1", "useful"))
        rows = self.store.list_recall_observations()
        self.assertEqual(rows[0]["selected_after"], ["memos/a"])
        self.assertEqual(rows[0]["feedback"], "useful")
        summary = self.store.recall_observation_summary()
        self.assertEqual(summary["triggered"], 1)
        self.assertEqual(summary["helped"], 1)
        self.assertEqual(summary["useful"], 1)
        self.assertEqual(self.store.stats()["recall_observations"], 1)

    async def test_observation_upsert_preserves_feedback(self):
        self.store.record_recall_observation({"request_id": "same", "query": "q"})
        self.store.set_recall_observation_feedback("same", "wrong")
        self.store.record_recall_observation({"request_id": "same", "query": "q2", "outcome": "no_change"})
        self.assertEqual(self.store.list_recall_observations()[0]["feedback"], "wrong")


class AutoEvalCaseTests(unittest.TestCase):
    def test_auto_eval_prefers_feedback_and_does_not_mutate_memory(self):
        class FakeEpisodes:
            def __init__(self):
                self.items = {}

            def upsert_eval_case(self, query, expected_memos, **kwargs):
                self.items[kwargs["case_id"]] = (query, list(expected_memos), kwargs)
                return {"case_id": kwargs["case_id"], "query": query, "expected_memos": expected_memos}

            def list_eval_cases(self, **kwargs):
                return [{"case_id": key} for key in self.items]

            def list_episodes(self, **kwargs):
                return [{
                    "memo_name": "memos/episode", "trigger_hint": "雨天在门口等候",
                    "scene_anchor": "门口的伞", "occurred_at": "2026年7月18日",
                }]

            def episode_detail(self, memo_name):
                return {"evidence": [{"detail": "她撑着伞在门口等我回来"}]}

        class FakeVec:
            @staticmethod
            def feedback_event_list(limit=500):
                return [{
                    "action": "useful", "query_text": "还记得那把伞吗",
                    "memo_name": "memos/feedback",
                }]

        fake = SimpleNamespace(
            _episodes=FakeEpisodes(), _vec=FakeVec(), recall_auto_eval_limit=20,
            _last_injection_stats=[{
                "query": "后来我们去了哪里", "memos": ["memos/telemetry"],
            }],
        )
        result = MemosMemoryPlugin._generate_recall_eval_cases(fake, 20)
        self.assertFalse(result["memory_content_changed"])
        self.assertEqual(result["writes"], "local_eval_tables_only")
        self.assertEqual(result["by_source"]["positive_feedback"], 1)
        self.assertEqual(result["by_source"]["telemetry_regression"], 1)
        self.assertEqual(result["by_source"]["episode_holdout"], 1)


if __name__ == "__main__":
    unittest.main()
