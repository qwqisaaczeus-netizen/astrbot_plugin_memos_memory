from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_memos_memory.vector_store import VectorStore
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin


class VectorStoreFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = VectorStore(str(Path(self.temp.name) / "memories.db"), 3, "test-embedding")
        await self.store.init()
        await self.store.insert_chunks(
            "memo-a",
            ["雨夜里我们约定不会忘记彼此。"],
            [[1.0, 0.0, 0.0]],
            ts_text="2026-07-01",
            tags=["约定", "雨夜"],
            importance=5,
            manual=1,
            memory_type="relationship_milestone",
            occurred_at="2026-07-01",
            event_ts=1782835200.0,
            time_basis="explicit",
            source_created_ts=1782838800.0,
            passages=[{"text": "雨夜里我们约定不会忘记彼此。", "passage_index": 0, "char_start": 0, "char_end": 15}],
            content_hash="hash-a",
            scene_anchor="雨夜屋檐",
            retrieval_key="约定和雨夜",
            state_change="关系更信任",
            entities=["我", "你"],
        )
        await self.store.insert_chunks(
            "memo-b",
            ["午后一起整理了房间。"],
            [[0.0, 1.0, 0.0]],
            ts_text="2026-07-02",
            tags=["日常"],
            importance=2,
            memory_type="daily_texture",
            occurred_at="2026-07-02",
            event_ts=1782921600.0,
            time_basis="explicit",
        )

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_hybrid_search_preserves_time_and_traceability(self):
        hits = await self.store.search_topk(
            [1.0, 0.0, 0.0],
            top_k=2,
            min_similarity=0.1,
            bm25_query="雨夜约定",
        )
        self.assertEqual(hits[0]["memo_name"], "memo-a")
        self.assertEqual(hits[0]["occurred_at"], "2026-07-01")
        self.assertEqual(hits[0]["time_basis"], "explicit")
        self.assertEqual(hits[0]["passage_index"], 0)
        self.assertIn("relevance", hits[0]["score_parts"])
        self.assertIn("importance", hits[0]["score_parts"])

        meta = self.store.get_memo_meta("memo-a")
        passages = self.store.get_memo_passages("memo-a")
        self.assertEqual(meta["scene_anchor"], "雨夜屋檐")
        self.assertEqual(meta["retrieval_key"], "约定和雨夜")
        self.assertEqual(passages[0]["char_start"], 0)
        self.assertGreaterEqual(self.store.passage_index_stats()["traced_passages"], 1)

    async def test_bm25_can_rescue_an_exact_name_outside_vector_candidates(self):
        hits = await self.store.search_topk(
            [0.0, 0.0, 1.0],
            top_k=1,
            min_similarity=0.4,
            bm25_query="雨夜里我们约定不会忘记彼此",
            bm25_weight=0.30,
        )
        self.assertEqual(hits[0]["memo_name"], "memo-a")
        self.assertTrue(hits[0]["lexical_rescue"])
        self.assertGreater(hits[0]["bm25_relevance"], 0)

    async def test_temporal_metadata_search_does_not_need_an_embedding(self):
        hits = self.store.search_temporal_keys(["2026-07-01", "7月1日"], limit=3)
        self.assertEqual(hits[0]["memo_name"], "memo-a")
        self.assertIn("2026-07-01", hits[0]["_temporal_keys"])

    async def test_query_feedback_month_router_keywords_and_clusters(self):
        event = self.store.feedback_record(
            "req-1", "memo-a", "你还记得那个雨夜的约定吗", "useful", 0.06,
            "有帮助", "test", [1.0, 0.0, 0.0],
        )
        self.assertGreater(event["id"], 0)
        related = self.store.feedback_effect_for_query(
            "memo-a", [1.0, 0.0, 0.0], "你还记得那个雨夜的约定吗",
        )
        unrelated = self.store.feedback_effect_for_query(
            "memo-a", [0.0, 0.0, 1.0], "今天晚饭吃什么",
        )
        self.assertGreater(related["boost"], 0)
        self.assertEqual(unrelated["boost"], 0)

        self.store.keywords_store("memo-a", [("雨夜", 2.0), ("约定", 1.8)])
        keyword_hits = self.store.keywords_find_in_text("你还记得那个雨夜的约定吗", min_hits=1)
        self.assertEqual(keyword_hits[0]["memo_name"], "memo-a")

        month_stats = self.store.month_index_rebuild()
        self.assertEqual(month_stats["months"], 1)
        overview = self.store.month_index_overview()
        self.assertEqual(overview[0]["year_month"], "2026-07")
        self.assertEqual(overview[0]["memo_count"], 2)
        calendar = self.store.month_calendar("2026-07")
        self.assertEqual(calendar["day_count"], 2)
        routes = self.store.month_route_search([1.0, 0.0, 0.0], "雨夜的约定", limit=2)
        self.assertEqual(routes[0]["year_month"], "2026-07")

        self.store.similarity_clusters_store(
            [
                {"memo_name": "memo-a", "cluster_id": "cluster-1", "representative": "memo-a", "cluster_size": 2, "max_similarity": 0.91},
                {"memo_name": "memo-b", "cluster_id": "cluster-1", "representative": "memo-a", "cluster_size": 2, "max_similarity": 0.91},
            ],
            [{"memo_a": "memo-a", "memo_b": "memo-b", "similarity": 0.91, "source": "test"}],
        )
        self.assertEqual(self.store.similarity_cluster_map({"memo-a", "memo-b"})["memo-b"], "cluster-1")
        self.assertEqual(self.store.similarity_edges_for_memo("memo-a")[0]["memo_name"], "memo-b")
        self.assertEqual(self.store.similarity_cluster_overview()[0]["size"], 2)

    async def test_month_candidate_filter_supports_more_than_sqlite_variable_limit(self):
        await self.store.insert_chunks(
            "memo-target", ["目标日记"], [[1.0, 0.0, 0.0]]
        )
        names = {f"memo-{index}" for index in range(1000)}
        names.add("memo-target")
        hits = await self.store.search_topk(
            [1.0, 0.0, 0.0], top_k=3, min_similarity=0.1,
            candidate_memo_names=names,
        )
        self.assertEqual([hit["memo_name"] for hit in hits], ["memo-target"])

    async def test_parallel_month_merge_never_duplicates_or_double_scores(self):
        direct = [{"memo_name": "memo-a", "score": 0.8, "_route_evidence": [{"route": "direct", "rank": 1}]}]
        month = [
            {"memo_name": "memo-a", "score": 1.5, "_route_evidence": [{"route": "month:2026-07", "rank": 1}]},
            {"memo_name": "memo-b", "score": 0.7, "_route_evidence": [{"route": "month:2026-07", "rank": 2}]},
        ]
        merged, diag = MemosMemoryPlugin._merge_parallel_recall_hits(direct, month)
        self.assertEqual([item["memo_name"] for item in merged], ["memo-a", "memo-b"])
        self.assertEqual(merged[0]["score"], 0.8)
        self.assertFalse(merged[0].get("_month_route_only", False))
        self.assertTrue(merged[1]["_month_route_only"])
        self.assertEqual(diag, {"direct": 1, "month": 2, "added": 1, "duplicates": 1})

    async def test_month_supplements_use_a_separate_injection_quota(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_month_route_inject_max = 2

        def fake_postprocess(_query, hits, _recent_seen=None):
            selected = list(hits[:3])
            return selected, {
                "candidate_pool": len(hits), "qualified": len(hits),
                "selected": len(selected), "folded": 0,
            }, list(hits)

        plugin._postprocess_recall_hits = fake_postprocess
        direct = [{"memo_name": f"direct-{index}"} for index in range(4)]
        month = [
            {"memo_name": f"month-{index}", "_month_route_only": True}
            for index in range(3)
        ]
        selected, diagnostics, _annotated = plugin._postprocess_parallel_recall_hits(
            "query", direct + month, set(),
        )
        self.assertEqual(
            [item["memo_name"] for item in selected],
            ["direct-0", "direct-1", "direct-2", "month-0", "month-1"],
        )
        self.assertEqual(diagnostics["direct_selected"], 3)
        self.assertEqual(diagnostics["month_supplement"]["selected"], 2)
        self.assertTrue(diagnostics["month_supplement"]["separate_quota"])

    async def test_month_branch_failure_leaves_direct_branch_unchanged(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_month_route_enable = True
        plugin.recall_month_route_timeout = 0.05

        async def direct(*_args):
            return ([{"memo_name": "memo-a", "score": 0.8}], {"routes": [{"name": "direct"}]}, {"key_terms": ["雨夜"]})

        async def failed_month(*_args):
            raise RuntimeError("month route unavailable")

        plugin._multi_route_search = direct
        plugin._month_route_search = failed_month
        hits, diag, facets = await plugin._parallel_recall_search("雨夜", "", 8, "7月1日")
        self.assertEqual(hits, [{"memo_name": "memo-a", "score": 0.8}])
        self.assertEqual(facets["key_terms"], ["雨夜"])
        self.assertEqual(diag["parallel_merge"]["added"], 0)
        self.assertIn("month route unavailable", diag["month_route"]["error"])

    async def test_retired_anchor_and_summary_rows_survive_derived_rebuild(self):
        self.store.anchor_set("memo-a", "plot_anchor", 4, "legacy")
        self.store.summary_store("2026-07", "legacy summary")
        self.store.feedback_set("memo-a", 0.2, "legacy feedback")
        await self.store.clear_all()
        self.assertEqual(self.store.anchor_list("memo-a")[0]["note"], "legacy")
        self.assertEqual(self.store.summary_get("2026-07")["summary"], "legacy summary")
        self.assertAlmostEqual(self.store.feedback_get_boost("memo-a"), 0.2)
        self.assertEqual(self.store.month_index_overview(), [])

    async def test_persistent_buffer_snapshot_and_bounded_drop(self):
        await self.store.buffer_append(
            "session-a",
            [
                {"role": "user", "content": "第一句"},
                {"role": "assistant", "content": "第一答"},
            ],
        )
        first, first_seq = await self.store.buffer_snapshot("session-a")
        await self.store.buffer_append("session-a", [{"role": "user", "content": "第二句"}])
        self.assertEqual(len(first), 2)
        self.assertEqual(first_seq, 1)
        self.assertEqual(await self.store.buffer_drop("session-a", first_seq), 2)
        remaining, remaining_seq = await self.store.buffer_snapshot("session-a")
        self.assertEqual(
            [{"role": x["role"], "content": x["content"]} for x in remaining],
            [{"role": "user", "content": "第二句"}],
        )
        self.assertGreater(remaining[0]["event_ts"], 0)
        self.assertEqual(remaining_seq, 2)

    async def test_health_listing_and_delete_are_scoped(self):
        health = self.store.health_check()
        self.assertEqual(health["total_memos"], 2)
        self.assertEqual(health["counts"]["high"], 0)
        self.assertEqual(self.store.distinct_memo_count(), 2)
        self.assertEqual({x["memo_name"] for x in self.store.list_memories()}, {"memo-a", "memo-b"})
        self.assertGreater(self.store.delete_memo_index("memo-a"), 0)
        self.assertFalse(self.store.memo_name_exists("memo-a"))
        self.assertTrue(self.store.memo_name_exists("memo-b"))


if __name__ == "__main__":
    unittest.main()
