from __future__ import annotations

import asyncio
import tempfile
import unittest
import json
from pathlib import Path

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin


class EpisodicStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(
            str(Path(self.temp.name) / "episodic.db"), 3, "test-embedding",
        )
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _episode(self):
        return {
            "occurred_at": "2026-08-03 21:10",
            "event_ts": 1785762600.0,
            "time_basis": "conversation_now",
            "memory_type": "promise_or_rule",
            "importance": 5,
            "scene_anchor": "雨声里约定不再突然消失",
            "retrieval_key": "雨夜 约定 突然消失 安心",
            "state_change": "从不安变得愿意相信",
            "long_effect": "之后会更认真回应离别和失联话题",
            "trigger_hint": "再次谈到失联时先确认对方的不安",
            "entities": ["雨夜", "约定"],
            "affect_before": "不安",
            "affect_after": "安心",
            "unresolved": ["仍担心承诺会落空"],
            "evidence": [
                {
                    "kind": "promise",
                    "actor": "assistant",
                    "detail": "答应不会再突然消失",
                    "quote": "我不会再突然消失。",
                    "turn_indexes": [1],
                    "confidence": 0.96,
                    "grounded": True,
                }
            ],
        }

    async def test_archive_is_idempotent_and_preserves_turn_order(self):
        messages = [
            {"role": "user", "content": "你会突然消失吗？", "event_ts": 10.0},
            {"role": "assistant", "content": "我不会再突然消失。", "event_ts": 11.0},
        ]
        first = self.store.archive_batch("session-a", messages, "auto")
        second = self.store.archive_batch("session-a", messages, "auto")
        self.assertEqual(first, second)
        self.assertEqual(self.store.stats()["source_batches"], 1)
        turns = self.store.source_turns(first)
        self.assertEqual([turn["turn_index"] for turn in turns], [0, 1])
        self.assertEqual(turns[1]["content"], "我不会再突然消失。")

    async def test_source_turn_vectors_search_and_map_back_to_exact_episode(self):
        batch_id = self.store.archive_batch(
            "session-source",
            [
                {"role": "user", "content": "抽屉里那枚银色钥匙不要弄丢。"},
                {"role": "assistant", "content": "我把钥匙放进了蓝色盒子。"},
            ],
            "auto",
        )
        self.store.upsert_episode(
            memo_name="memos/key",
            episode={
                **self._episode(),
                "scene_anchor": "把银色钥匙收进蓝色盒子",
                "evidence": [{
                    "kind": "object_state", "actor": "assistant",
                    "detail": "银色钥匙在蓝色盒子里",
                    "quote": "我把钥匙放进了蓝色盒子。",
                    "turn_indexes": [1], "confidence": 0.99, "grounded": True,
                }],
            },
            card_text="银色钥匙 蓝色盒子",
            embedding=[0.0, 1.0, 0.0],
            source_batch_id=batch_id,
            legacy=False,
            evidence_quality="source_grounded",
        )
        rows = self.store.source_turn_embedding_rows(batch_id=batch_id, limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.store.replace_source_turn_embeddings([
            (rows[0]["id"], [1.0, 0.0, 0.0]),
            (rows[1]["id"], [0.0, 1.0, 0.0]),
        ]), 2)
        hits = self.store.search_source_turns(
            [0.0, 1.0, 0.0], "蓝色盒子里的钥匙", limit=3,
        )
        self.assertEqual(hits[0]["turn_index"], 1)
        mappings = self.store.episodes_for_source_turn(batch_id, 1)
        self.assertEqual(mappings[0]["memo_name"], "memos/key")
        self.assertTrue(mappings[0]["exact_turn_link"])
        self.assertEqual(self.store.stats()["source_turn_vectors"], 2)

    async def test_plugin_source_batch_indexing_is_idempotent(self):
        batch_id = self.store.archive_batch(
            "session-index",
            [
                {"role": "user", "content": "记住红色围巾放在玄关。"},
                {"role": "assistant", "content": "我会记住玄关的红色围巾。"},
            ],
            "auto",
        )
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store

        async def embed_batch(texts):
            return [[1.0, float(index), 0.0] for index, _text in enumerate(texts)]

        plugin._embed_batch = embed_batch
        self.assertEqual(await plugin._index_source_batch_turns(batch_id), 2)
        self.assertEqual(await plugin._index_source_batch_turns(batch_id), 0)
        self.assertEqual(self.store.stats()["source_turn_vectors"], 2)

    async def test_episode_upsert_search_detail_and_delete(self):
        batch_id = self.store.archive_batch(
            "session-a",
            [
                {"role": "user", "content": "你会突然消失吗？"},
                {"role": "assistant", "content": "我不会再突然消失。"},
            ],
            "auto",
        )
        self.store.upsert_episode(
            memo_name="memos/rain",
            episode=self._episode(),
            card_text="雨夜约定，不再突然消失，从不安到安心",
            embedding=[1.0, 0.0, 0.0],
            source_batch_id=batch_id,
            source_kind="auto",
            legacy=False,
            evidence_quality="source_grounded",
            diary_content_hash="hash-a",
        )
        hits = self.store.search_cards([1.0, 0.0, 0.0], "雨夜约定", limit=3)
        self.assertEqual(hits[0]["memo_name"], "memos/rain")
        self.assertEqual(hits[0]["evidence_quality"], "source_grounded")
        detail = self.store.episode_detail("memos/rain")
        self.assertEqual(detail["evidence"][0]["quote_text"], "我不会再突然消失。")
        self.assertEqual(len(detail["source_turns"]), 2)

        changed = self._episode()
        changed["state_change"] = "从不安变成暂时安心"
        self.store.upsert_episode(
            memo_name="memos/rain",
            episode=changed,
            card_text="雨夜约定，从不安变成暂时安心",
            embedding=[0.9, 0.1, 0.0],
            source_batch_id=batch_id,
            source_kind="memos_user_edit",
            legacy=False,
            evidence_quality="mixed_user_edited",
            diary_content_hash="hash-b",
        )
        self.assertEqual(self.store.stats()["episodes"], 1)
        self.assertEqual(self.store.get_episode("memos/rain")["evidence_quality"], "mixed_user_edited")
        self.assertEqual(self.store.delete_by_memo_name("memos/rain"), 1)
        self.assertIsNone(self.store.get_episode("memos/rain"))
        self.assertEqual(self.store.stats()["source_batches"], 1)

    async def test_4_0_database_is_upgraded_additively_and_idempotently(self):
        batch_id = self.store.archive_batch(
            "old-session",
            [{"role": "user", "content": "这是 4.0 保留下来的原始轮次。"}],
            "auto",
        )
        self.store.upsert_episode(
            memo_name="memos/existing-4-0",
            episode=self._episode(),
            card_text="4.0 已有情景卡",
            embedding=[1.0, 0.0, 0.0],
            source_batch_id=batch_id,
            evidence_quality="source_grounded",
        )
        conn = self.store._connect()
        for table in (
            "semantic_states", "semantic_state_versions", "semantic_state_queue",
            "recall_eval_cases", "recall_eval_runs",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        conn.commit()
        path = self.store.db_path
        self.store.close()

        first = EpisodicStore(path, 3, "test-embedding")
        await first.init()
        self.assertEqual(first.get_episode("memos/existing-4-0")["source_batch_id"], batch_id)
        self.assertEqual(first.source_turns(batch_id)[0]["content"], "这是 4.0 保留下来的原始轮次。")
        self.assertEqual(first._connect().execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"], "3")
        first.close()

        second = EpisodicStore(path, 3, "test-embedding")
        await second.init()
        self.assertEqual(second.stats()["episodes"], 1)
        self.assertEqual(second.stats()["source_turns"], 1)
        second.close()

    async def test_semantic_state_replaces_current_and_keeps_compact_history(self):
        scope = "character:爱莉"
        first = self.store.upsert_semantic_state(
            scope,
            {
                "relationship_position": "仍在试探信任",
                "commitments_boundaries": "离开前会说明",
                "behavior_tendencies": "先确认不安",
                "emotional_baseline": "温柔但谨慎",
                "open_loops": "担心承诺落空",
            },
            reason="bootstrap",
        )
        second = self.store.upsert_semantic_state(
            scope,
            {
                "relationship_position": "已经建立稳定信任",
                "commitments_boundaries": "离开前会说明",
                "behavior_tendencies": "会主动交代去向",
                "emotional_baseline": "安心而亲近",
                "open_loops": "",
            },
            reason="compression",
        )
        current = self.store.get_semantic_state(scope)
        history = self.store.semantic_state_history(scope)
        self.assertEqual((first["version"], second["version"], current["version"]), (1, 2, 2))
        self.assertNotIn("仍在试探信任", current["rendered_text"])
        self.assertIn("已经建立稳定信任", current["rendered_text"])
        self.assertEqual([item["version"] for item in history], [2, 1])
        self.assertIn("仍在试探信任", history[1]["state"]["rendered_text"])

    async def test_state_retry_queue_and_eval_cases_persist(self):
        batch_id = self.store.archive_batch(
            "session-a", [{"role": "user", "content": "记得雨夜的约定吗？"}], "auto",
        )
        self.store.enqueue_state_update(batch_id, "character:爱莉", ["ep-one"])
        self.store.mark_state_update(batch_id, "failed", "provider timeout")
        pending = self.store.pending_state_updates()
        self.assertEqual(pending[0]["batch_id"], batch_id)
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertIn("timeout", pending[0]["last_error"])

        one = self.store.upsert_eval_case("还记得雨夜吗", ["memos/rain"], source="feedback")
        two = self.store.upsert_eval_case("还记得雨夜吗", ["memos/roof"], source="feedback")
        self.assertEqual(one["case_id"], two["case_id"])
        self.assertEqual(
            self.store.list_eval_cases()[0]["expected_memos"],
            ["memos/rain", "memos/roof"],
        )

    async def test_forced_state_rebuild_does_not_seed_from_deleted_current_state(self):
        self.store.upsert_semantic_state(
            "character:爱莉",
            {"relationship_position": "只由已删除日记产生的结论"},
            reason="old_state",
        )
        self.store.upsert_episode(
            memo_name="memos/remaining",
            episode=self._episode(),
            card_text="仍然存在的雨夜约定",
            embedding=[1.0, 0.0, 0.0],
            evidence_quality="diary_derived",
        )
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_bootstrap_episode_limit = 120
        plugin.semantic_state_target_chars = 1800
        plugin.semantic_state_provider_id = ""
        plugin.semantic_state_timeout = 30
        plugin.semantic_state_enable = True
        plugin.enable_affiliate_profile = False
        plugin._semantic_state_lock = asyncio.Lock()
        plugin._log_event = lambda *_args, **_kwargs: None
        prompts = []

        async def call(prompt, **_kwargs):
            prompts.append(prompt)
            return json.dumps({
                "relationship_position": "由仍存在的记忆重新建立",
                "commitments_boundaries": "离开前会说明",
                "behavior_tendencies": "主动确认不安",
                "emotional_baseline": "平静",
                "open_loops": "",
            }, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        result = await plugin._bootstrap_semantic_state(force=True, reason="delete_rebuild")
        self.assertTrue(result["updated"])
        self.assertNotIn("只由已删除日记产生的结论", prompts[0])
        self.assertIn("由仍存在的记忆重新建立", self.store.get_semantic_state("character:爱莉")["rendered_text"])

    async def test_forced_empty_rebuild_clears_current_but_keeps_history(self):
        scope = "character:爱莉"
        self.store.upsert_semantic_state(
            scope, {"relationship_position": "不应继续注入的旧状态"}, reason="old_state",
        )
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_bootstrap_episode_limit = 120
        plugin._semantic_state_lock = asyncio.Lock()
        result = await plugin._bootstrap_semantic_state(force=True, reason="delete_all")
        self.assertTrue(result["cleared"])
        self.assertIsNone(self.store.get_semantic_state(scope))
        self.assertEqual(len(self.store.semantic_state_history(scope)), 1)


class EpisodeBlueprintValidationTests(unittest.TestCase):
    def test_lean_selection_keeps_two_strong_normal_and_four_broad_memories(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_injection_min_score = 0.62
        plugin.lean_story_min_inject = 1
        plugin.lean_story_max_inject = 4
        plugin.lean_texture_enable = False
        plugin._memory_layer = lambda _hit: "plot"
        hits = [
            {"memo_name": f"memos/{index}", "_rerank_score": score}
            for index, score in enumerate((0.90, 0.75, 0.70, 0.65, 0.64), 1)
        ]

        normal, normal_diag, _ = plugin._postprocess_lean_hits(
            "明确问题", hits, {"target": 2, "narrative": False}, set()
        )
        self.assertEqual(len(normal), 2)
        self.assertEqual(normal_diag["max_inject"], 2)

        narrative, narrative_diag, _ = plugin._postprocess_lean_hits(
            "把以前发生的过程完整说说", hits, {"target": 4, "narrative": True}, set()
        )
        self.assertEqual(len(narrative), 4)
        self.assertEqual(narrative_diag["max_inject"], 4)

    def test_lean_coverage_preserves_distinct_dates_and_rejects_same_fact_duplicate(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_injection_min_score = 0.62
        plugin.lean_story_min_inject = 1
        plugin.lean_story_max_inject = 4
        plugin.lean_texture_enable = False
        plugin.lean_coverage_selection_enable = True
        plugin._memory_layer = lambda _hit: "plot"
        hits = [
            {
                "memo_name": "memos/origin", "_rerank_score": 0.92,
                "occurred_at": "2026-05-01", "scene_anchor": "雨夜约定离开前一定说明",
                "memory_type": "promise_or_rule",
            },
            {
                "memo_name": "memos/duplicate", "_rerank_score": 0.84,
                "occurred_at": "2026-05-01", "scene_anchor": "雨夜约定离开前一定说明",
                "memory_type": "promise_or_rule",
            },
            {
                "memo_name": "memos/change", "_rerank_score": 0.68,
                "occurred_at": "2026-07-18", "scene_anchor": "后来重新讨论失联",
                "state_change": "从单向保证改成彼此提前说明",
                "memory_type": "relationship_shift",
            },
        ]
        selected, diag, _ = plugin._postprocess_lean_hits(
            "完整说说这个约定后来怎么变了", hits,
            {"target": 3, "narrative": True, "broad": True}, set(),
        )
        names = [item["memo_name"] for item in selected]
        self.assertIn("memos/origin", names)
        self.assertIn("memos/change", names)
        self.assertNotIn("memos/duplicate", names)
        self.assertGreaterEqual(diag["evidence_coverage"]["covered_dates"], 2)

    def test_recent_memory_is_soft_penalized_but_can_be_reused_when_still_needed(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_injection_min_score = 0.62
        plugin.lean_story_min_inject = 1
        plugin.lean_story_max_inject = 4
        plugin.lean_texture_enable = False
        plugin.lean_coverage_selection_enable = True
        plugin._memory_layer = lambda _hit: "plot"
        selected, diag, _ = plugin._postprocess_lean_hits(
            "继续说刚才那个雨夜约定",
            [{
                "memo_name": "memos/rain", "_rerank_score": 0.91,
                "occurred_at": "2026-08-03", "memory_type": "promise_or_rule",
                "scene_anchor": "雨夜里答应离开前先说明",
            }],
            {"target": 2, "narrative": False},
            {"memos/rain"},
        )
        self.assertEqual([item["memo_name"] for item in selected], ["memos/rain"])
        self.assertEqual(diag["recent_reused"], 1)

    def test_texture_is_not_opportunistic_but_direct_question_can_rescue_it(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_injection_min_score = 0.62
        plugin.lean_story_min_inject = 1
        plugin.lean_story_max_inject = 4
        plugin.lean_texture_enable = False
        plugin.lean_coverage_selection_enable = True
        plugin._memory_layer = lambda _hit: "texture"
        hit = {
            "memo_name": "memos/breakfast", "_rerank_score": 0.76,
            "scene_anchor": "早餐时一起吃草莓蛋糕",
            "chunk_text": "她把最后一颗草莓留给了我。",
            "memory_type": "daily_texture",
        }
        direct, direct_diag, _ = plugin._postprocess_lean_hits(
            "还记得早餐那块草莓蛋糕吗", [hit],
            {"target": 2, "narrative": False}, set(),
        )
        unrelated, _, _ = plugin._postprocess_lean_hits(
            "今晚的战斗计划", [{**hit, "_rerank_score": 0.65}],
            {"target": 2, "narrative": False}, set(),
        )
        self.assertEqual([item["memo_name"] for item in direct], ["memos/breakfast"])
        self.assertEqual(direct_diag["texture_direct_rescued"], 1)
        self.assertEqual(unrelated, [])

    def test_event_and_passage_consensus_can_rescue_provider_scale_borderline(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_injection_min_score = 0.62
        plugin.lean_story_min_inject = 1
        plugin.lean_story_max_inject = 4
        plugin.lean_texture_enable = False
        plugin.lean_coverage_selection_enable = True
        plugin._memory_layer = lambda _hit: "plot"
        selected, diag, _ = plugin._postprocess_lean_hits(
            "雨夜约定",
            [{
                "memo_name": "memos/rain", "_rerank_score": 0.55,
                "relevance": 0.58, "score": 0.58, "_dual_granularity": True,
                "scene_anchor": "雨夜约定",
            }],
            {"target": 2, "narrative": False}, set(),
        )
        self.assertEqual([item["memo_name"] for item in selected], ["memos/rain"])
        self.assertEqual(diag["dual_consensus_rescued"], 1)

    def test_separately_injected_state_suppresses_profile_but_keeps_failure_fallback(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.enable_affiliate_profile = True
        plugin.semantic_state_replace_profile = True
        plugin.enable_time_insight_affiliate = False
        plugin.lean_recall_enable = True
        plugin._semantic_state_injection_block = lambda: "<CurrentSemanticState>STATE</CurrentSemanticState>"
        plugin._affiliate_profile_status = lambda: {
            "connected": True,
            "fresh": True,
            "profile": "OLD PROFILE",
            "profile_facts": "OLD FACTS",
        }
        plugin._time_insight_status = lambda: {"injection_block": ""}

        injected, injected_parts = plugin._format_injection_with_profile(
            [{"layer": "plot", "text": "MEMORY"}],
            return_parts=True,
            include_semantic_state=False,
            semantic_state_already_injected=True,
        )
        self.assertNotIn("OLD PROFILE", injected)
        self.assertEqual(injected_parts["profile"], 0)
        self.assertEqual(injected_parts["semantic_state"], 0)

        fallback, fallback_parts = plugin._format_injection_with_profile(
            [{"layer": "plot", "text": "MEMORY"}],
            return_parts=True,
            include_semantic_state=False,
            semantic_state_already_injected=False,
        )
        self.assertIn("OLD PROFILE", fallback)
        self.assertGreater(fallback_parts["profile"], 0)

    def test_untraceable_quote_is_removed_and_wrong_actor_is_rejected(self):
        plugin = object.__new__(MemosMemoryPlugin)
        payload = """[
          {
            "episode_key":"e1",
            "memory_type":"promise_or_rule",
            "evidence":[
              {"actor":"assistant","detail":"用户担心我会离开","quote":"并不存在的原话","turn_indexes":[0],"confidence":0.9},
              {"actor":"assistant","detail":"我答应不会突然消失","quote":"我不会突然消失","turn_indexes":[1],"confidence":0.95}
            ]
          }
        ]"""
        messages = [
            {"role": "user", "content": "我担心你会离开。"},
            {"role": "assistant", "content": "我不会突然消失。"},
        ]
        episodes = plugin._parse_episode_blueprints(payload, messages, 2)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["evidence"][0]["actor"], "")
        self.assertEqual(episodes[0]["evidence"][0]["quote"], "")
        self.assertLessEqual(episodes[0]["evidence"][0]["confidence"], 0.55)
        self.assertEqual(episodes[0]["evidence"][1]["actor"], "assistant")
        self.assertEqual(episodes[0]["evidence"][1]["quote"], "我不会突然消失")


class LegacyMemosBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_is_idempotent_repairs_missing_vectors_and_follows_deletes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EpisodicStore(str(Path(temp) / "episodes.db"), 3, "test")
            await store.init()
            plugin = object.__new__(MemosMemoryPlugin)
            plugin._episodes = store
            plugin._memos = object()
            plugin.rp_time_timezone = "Asia/Shanghai"
            plugin._log_event = lambda *_args, **_kwargs: None
            plugin.imp_tier5_keywords = "承诺,永远"
            plugin.imp_tier4_keywords = "离开,重逢"
            plugin.imp_tier3_keywords = "约定,担心"
            plugin.imp_low_keywords = "吃饭,天气"

            async def embed_batch(texts):
                return [[1.0, 0.0, 0.0] for _ in texts]

            plugin._embed_batch = embed_batch
            memos = [{
                "name": "memos/legacy",
                "content": "2026-08-03\n那天雨很大，我们在屋檐下说好不再突然消失。\n#约定",
                "createTime": "2026-08-03T12:00:00Z",
                "updateTime": "2026-08-03T12:00:00Z",
            }]
            try:
                first = await plugin._rebuild_episodic_from_memos(memos=memos)
                second = await plugin._rebuild_episodic_from_memos(memos=memos)
                self.assertEqual(first["ok"], 1)
                self.assertEqual(second["skipped"], 1)
                self.assertEqual(store.get_episode("memos/legacy")["evidence_quality"], "diary_derived")

                store._connect().execute("UPDATE episodes SET embedding=NULL")
                store._connect().commit()
                repaired = await plugin._rebuild_episodic_from_memos(memos=memos)
                self.assertEqual(repaired["ok"], 1)
                self.assertTrue(store.get_episode("memos/legacy")["embedding_ready"])

                removed = await plugin._rebuild_episodic_from_memos(memos=[])
                self.assertEqual(removed["removed"], 1)
                self.assertIsNone(store.get_episode("memos/legacy"))
            finally:
                store.close()


class EvidenceFirstGenerationTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.character_name = "爱莉"
        plugin.rp_time_timezone = "Asia/Shanghai"
        plugin.episode_extraction_provider_id = ""
        plugin.episode_extraction_timeout = 30.0
        plugin.diary_render_provider_id = ""
        plugin.diary_render_timeout = 30.0
        return plugin

    async def test_two_stage_generation_keeps_traceable_evidence(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "我有点怕你突然消失。"},
            {"role": "assistant", "content": "我不会突然消失，我会先告诉你。"},
        ]
        extraction = [{
            "episode_key": "e1",
            "event_date": "2026-08-03",
            "time_basis": "conversation_now",
            "scene_anchor": "害怕消失时的约定",
            "memory_type": "promise_or_rule",
            "evidence": [
                {"actor": "user", "detail": "他害怕我突然消失", "quote": "我有点怕你突然消失。", "turn_indexes": [0], "confidence": 0.95},
                {"actor": "assistant", "detail": "我答应不会突然消失并会先告诉他", "quote": "我不会突然消失，我会先告诉你。", "turn_indexes": [1], "confidence": 0.98},
            ],
            "affect_before": "听见他的害怕后有些心疼",
            "affect_after": "愿意更明确地给出安全感",
            "state_change": "从默认陪伴变成主动说明离开",
            "long_effect": "以后离开前会先说明",
            "trigger_hint": "谈到失联时先安抚",
            "retrieval_key": "突然消失 先告诉 约定",
            "entities": ["约定"],
            "unresolved": [],
            "importance": 5,
        }]
        rendered = [{
            "episode_key": "e1",
            "content": "他低声说自己害怕我突然消失。我答应不会突然消失，也会在离开前先告诉他。那一刻，我更想认真给他安全感。",
        }]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append(label)
            return json.dumps(extraction if label == "episode_extract" else rendered, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "[turn:0] 用户: 我有点怕你突然消失。\n[turn:1] 我: 我不会突然消失，我会先告诉你。",
            1,
        )
        self.assertEqual(calls, ["episode_extract", "diary_render"])
        self.assertEqual(diaries[0]["_evidence_quality"], "source_grounded")
        self.assertGreaterEqual(diaries[0]["_render_coverage"], 0.5)
        self.assertTrue(all(item["grounded"] for item in diaries[0]["evidence"]))

    async def test_eod_capacity_does_not_force_split_one_grounded_scene(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "上午我们在窗边谈到雨。"},
            {"role": "assistant", "content": "我把窗关好，陪你听雨。"},
            {"role": "user", "content": "晚上我又问你会不会离开。"},
            {"role": "assistant", "content": "我答应离开前会告诉你。"},
        ]
        one = [{
            "episode_key": "e1", "memory_type": "daily_texture", "scene_anchor": "窗边听雨",
            "evidence": [{"actor": "assistant", "detail": "我关窗陪他听雨", "quote": "我把窗关好，陪你听雨。", "turn_indexes": [1]}],
        }]
        rendered = [{"episode_key": "e1", "content": "我把窗关好，陪他在窗边听雨。"}]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append(label)
            if label == "episode_extract":
                return json.dumps(one, ensure_ascii=False)
            return json.dumps(rendered, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "\n".join(f"[turn:{i}] {m['content']}" for i, m in enumerate(messages)),
            2,
            exact_count=False,
        )
        self.assertEqual(len(diaries), 1)
        self.assertEqual(calls, ["episode_extract", "diary_render"])

    def test_render_coverage_accepts_grounded_quote_when_detail_is_paraphrased(self):
        plugin = self.make_plugin()
        episode = {
            "evidence": [{
                "detail": "对方承诺在离开之前主动说明去向",
                "quote": "我走之前一定告诉你",
            }],
        }
        coverage, missing = plugin._diary_render_coverage(
            "他看着我说：‘我走之前一定告诉你。’我把这句话记了下来。",
            episode,
        )
        self.assertEqual(coverage, 1.0)
        self.assertEqual(missing, [])

    async def test_missing_episode_key_is_merged_from_partial_retry(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "把银色钥匙放进蓝色盒子。"},
            {"role": "assistant", "content": "好，我会记得钥匙在蓝色盒子里。"},
            {"role": "user", "content": "还有，离开前告诉我。"},
            {"role": "assistant", "content": "我答应离开前一定告诉你。"},
        ]
        episodes = [
            {
                "episode_key": "e1", "event_date": "2026-08-04",
                "scene_anchor": "收好银色钥匙", "memory_type": "plot_fact",
                "evidence": [{"actor": "assistant", "detail": "银色钥匙在蓝色盒子里", "quote": "钥匙在蓝色盒子里", "turn_indexes": [1]}],
            },
            {
                "episode_key": "e2", "event_date": "2026-08-04",
                "scene_anchor": "离开前说明的约定", "memory_type": "promise_or_rule",
                "evidence": [{"actor": "assistant", "detail": "我答应离开前一定告诉他", "quote": "我答应离开前一定告诉你", "turn_indexes": [3]}],
            },
        ]
        first = [{"episode_key": "e1", "content": "我记得银色钥匙在蓝色盒子里。"}]
        retry = [{"episode_key": "e2", "content": "我答应过，离开前一定告诉他。"}]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append(label)
            if label == "episode_extract":
                return json.dumps(episodes, ensure_ascii=False)
            if label == "diary_render":
                return json.dumps(first, ensure_ascii=False)
            return json.dumps(retry, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
            2,
        )
        self.assertEqual(calls, ["episode_extract", "diary_render", "diary_render_retry"])
        self.assertEqual([item["episode_key"] for item in diaries], ["e1", "e2"])
        self.assertTrue(all(item["_evidence_quality"] == "source_grounded" for item in diaries))
        self.assertTrue(all(not item["_render_fallback"] for item in diaries))
        self.assertIn("银色钥匙", diaries[0]["content"])
        self.assertIn("离开前", diaries[1]["content"])

    async def test_missing_episode_after_retry_uses_grounded_fallback(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "离开前告诉我。"},
            {"role": "assistant", "content": "我答应离开前一定告诉你。"},
        ]
        episodes = [{
            "episode_key": "e1", "event_date": "2026-08-04",
            "scene_anchor": "离开前说明的约定", "memory_type": "promise_or_rule",
            "evidence": [{"actor": "assistant", "detail": "我答应离开前一定告诉他", "quote": "我答应离开前一定告诉你", "turn_indexes": [1]}],
            "state_change": "我开始主动说明离开",
        }]

        async def call(_prompt, *, provider_id, timeout, label):
            if label == "episode_extract":
                return json.dumps(episodes, ensure_ascii=False)
            return "[]"

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
            1,
        )
        self.assertEqual(len(diaries), 1)
        self.assertTrue(diaries[0]["_render_fallback"])
        self.assertEqual(diaries[0]["_evidence_quality"], "source_grounded")
        self.assertEqual(diaries[0]["_render_coverage"], 1.0)
        self.assertIn("离开前一定告诉", diaries[0]["content"])

    async def test_two_stage_failure_falls_back_without_losing_raw_batch(self):
        class Context:
            def get_provider_by_id(self, _provider_id):
                return None

            def get_using_provider(self, _umo=None):
                return None

            def get_all_providers(self):
                return []

        with tempfile.TemporaryDirectory() as temp:
            plugin = MemosMemoryPlugin(Context(), {
                "vec_db_path": str(Path(temp) / "memories.db"),
                "episodic_db_path": str(Path(temp) / "episodes.db"),
                "webui_enable": False,
                "enable_auto_compress": False,
                "character_name": "爱莉",
            })
            store = EpisodicStore(str(Path(temp) / "episodes.db"), 3, "test")
            await store.init()
            plugin._episodes = store
            messages = []
            for index in range(4):
                messages.extend([
                    {"role": "user", "content": f"对话{index}"},
                    {"role": "assistant", "content": f"回答{index}"},
                ])
            calls = []

            async def fail_evidence(*_args, **_kwargs):
                raise RuntimeError("structured extraction unavailable")

            async def legacy(_prompt):
                calls.append("legacy")
                return json.dumps([{
                    "event_date": "", "time_label": "", "time_basis": "unknown",
                    "scene_anchor": "兼容日记", "content": "我记下了这段对话。",
                    "memory_type": "daily_texture", "long_effect": "我会记得。",
                    "trigger_hint": "再次提到时想起。", "retrieval_key": "这段对话",
                    "state_change": "", "entities": [], "tags": [], "importance": 3,
                }], ensure_ascii=False)

            async def store_diary(item, **_kwargs):
                item["_stored_memo_name"] = "memos/fallback"
                item["_stored_time_meta"] = {"time_basis": "unknown"}
                return True

            persisted = []

            async def persist(item, **kwargs):
                persisted.append((item, kwargs))
                return True

            plugin._generate_evidence_first_diaries = fail_evidence
            plugin._call_llm_compress = legacy
            plugin._store_one_diary = store_diary
            plugin._persist_episode_for_diary = persist
            try:
                result = await plugin._compress_and_store("session-a", messages, diary_count=1)
                self.assertEqual(result, 1)
                self.assertEqual(calls, ["legacy"])
                self.assertEqual(len(persisted), 1)
                batches = store.batch_status(5)
                self.assertEqual(batches[0]["status"], "committed")
                self.assertEqual(batches[0]["message_count"], 8)
            finally:
                await plugin._xinchao.terminate()
                store.close()


class EpisodicCascadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dual_granularity_event_card_rescues_missing_direct_passage(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EpisodicStore(str(Path(temp) / "episodes.db"), 3, "test")
            await store.init()
            episode = EpisodicStoreTests._episode(self)
            store.upsert_episode(
                memo_name="memos/rain", episode=episode,
                card_text="雨夜约定 不再突然消失 从不安到安心",
                embedding=[1.0, 0.0, 0.0], evidence_quality="source_grounded",
            )
            plugin = object.__new__(MemosMemoryPlugin)
            plugin._episodes = store
            plugin.episodic_candidate_pool = 18
            plugin.episodic_default_inject = 5
            plugin.episodic_narrative_inject = 8
            plugin.lean_recall_candidate_k = 50
            plugin.lean_story_min_inject = 1
            plugin.lean_story_max_inject = 4
            plugin.lean_event_index_enable = True
            plugin.lean_temporal_enable = True
            plugin.recall_embed_timeout = 12.0
            plugin.min_similarity_to_inject = 0.52
            plugin.w_relevance = 0.75
            plugin.w_importance = 0.15
            plugin.w_recency = 0.10
            plugin.pin_boost = 0.08
            plugin._rerank_provider = None
            plugin._passage_vector_migration_state = {"status": "ready"}
            embed_calls = []

            async def embed(text, timeout=None):
                embed_calls.append((text, timeout))
                return [1.0, 0.0, 0.0]

            class Vec:
                async def search_topk(self, _query_vec, **kwargs):
                    if not kwargs.get("candidate_memo_names"):
                        return []
                    return [{
                        "memo_name": "memos/rain", "chunk_text": "离开前我会先告诉你。",
                        "ts_text": "2026-08-03", "relevance": 0.76, "score": 0.74,
                        "_matched_passages": [{"text": "离开前我会先告诉你。", "passage_index": 2}],
                    }]

                def get_memo_meta(self, _memo_name):
                    return {}

                def search_temporal_keys(self, _keys, limit=20):
                    return []

            plugin._vec = Vec()
            plugin._embed = embed
            try:
                hits, diagnostics, _ = await plugin._lean_recall_search(
                    "你答应过不会突然消失吗", "", "08月03日",
                )
                self.assertEqual(len(embed_calls), 1)
                self.assertEqual([hit["memo_name"] for hit in hits], ["memos/rain"])
                self.assertTrue(hits[0]["_event_card_hit"])
                self.assertEqual(diagnostics["event_only_rescues"], 1)
                self.assertEqual(diagnostics["embedding_queries"], 1)
                self.assertEqual(diagnostics["mode"], "lean_full_memory_fusion")
            finally:
                store.close()

    async def test_raw_source_turn_rescues_detail_missing_from_diary_and_event_card(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EpisodicStore(str(Path(temp) / "episodes.db"), 3, "test")
            await store.init()
            batch_id = store.archive_batch(
                "source-session",
                [{"role": "assistant", "content": "我把银色钥匙藏在蓝色盒子的夹层里。"}],
                "auto",
            )
            episode = EpisodicStoreTests._episode(self)
            episode.update({
                "scene_anchor": "整理房间里的旧物",
                "retrieval_key": "整理房间 旧物",
                "evidence": [{
                    "detail": "银色钥匙在蓝色盒子夹层",
                    "turn_indexes": [0], "grounded": True, "confidence": 0.99,
                }],
            })
            store.upsert_episode(
                memo_name="memos/key", episode=episode,
                card_text="整理房间里的旧物",
                embedding=[0.0, 1.0, 0.0], source_batch_id=batch_id,
                legacy=False, evidence_quality="source_grounded",
            )
            row = store.source_turn_embedding_rows(batch_id=batch_id, limit=2)[0]
            store.replace_source_turn_embeddings([(row["id"], [1.0, 0.0, 0.0])])

            plugin = object.__new__(MemosMemoryPlugin)
            plugin._episodes = store
            plugin.episodic_candidate_pool = 18
            plugin.episodic_default_inject = 5
            plugin.episodic_narrative_inject = 8
            plugin.lean_recall_candidate_k = 50
            plugin.lean_story_min_inject = 1
            plugin.lean_story_max_inject = 4
            plugin.lean_event_index_enable = True
            plugin.lean_source_evidence_enable = True
            plugin.lean_temporal_enable = True
            plugin.recall_embed_timeout = 12.0
            plugin.min_similarity_to_inject = 0.52
            plugin.w_relevance = 0.75
            plugin.w_importance = 0.15
            plugin.w_recency = 0.10
            plugin.pin_boost = 0.08
            plugin._rerank_provider = None
            plugin._passage_vector_migration_state = {"status": "ready"}
            plugin._source_turn_vector_migration_state = {"status": "ready"}

            async def embed(_text, timeout=None):
                return [1.0, 0.0, 0.0]

            class Vec:
                async def search_topk(self, _query_vec, **_kwargs):
                    return []

                def get_memo_meta(self, _memo_name):
                    return {"chunk_text": "那天只是整理了房间。", "ts_text": "2026-08-03"}

                def search_temporal_keys(self, _keys, limit=20):
                    return []

            plugin._vec = Vec()
            plugin._embed = embed
            try:
                hits, diagnostics, _ = await plugin._lean_recall_search(
                    "蓝色盒子夹层里的银色钥匙呢", "", "08月03日",
                )
                self.assertEqual([hit["memo_name"] for hit in hits], ["memos/key"])
                self.assertTrue(hits[0]["_source_evidence_hit"])
                self.assertEqual(diagnostics["source_episode_rescues"], 1)
                self.assertEqual(diagnostics["embedding_queries"], 1)
                plugin.recall_injection_min_score = 0.62
                plugin.lean_texture_enable = False
                plugin.lean_coverage_selection_enable = True
                selected, selection_diag, _ = plugin._postprocess_lean_hits(
                    "蓝色盒子夹层里的银色钥匙呢", hits,
                    {"target": 2, "narrative": False, "temporal": False}, set(),
                )
                self.assertEqual([hit["memo_name"] for hit in selected], ["memos/key"])
                self.assertEqual(selection_diag["source_direct_rescued"], 1)
            finally:
                store.close()

    async def test_cascade_embeds_once_and_restricts_passage_search(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EpisodicStore(str(Path(temp) / "episodes.db"), 3, "test")
            await store.init()
            episode = EpisodicStoreTests._episode(self)
            store.upsert_episode(
                memo_name="memos/rain", episode=episode,
                card_text="雨夜约定 不再突然消失 从不安到安心",
                embedding=[1.0, 0.0, 0.0], evidence_quality="source_grounded",
            )
            plugin = object.__new__(MemosMemoryPlugin)
            plugin._episodes = store
            plugin.episodic_candidate_pool = 18
            plugin.episodic_default_inject = 5
            plugin.episodic_narrative_inject = 8
            plugin.episodic_min_card_score = 0.40
            plugin.recall_embed_timeout = 12.0
            plugin.min_similarity_to_inject = 0.52
            plugin.w_relevance = 0.75
            plugin.w_importance = 0.15
            plugin.w_recency = 0.10
            plugin.pin_boost = 0.08
            plugin.time_boost = 0.06
            plugin._rerank_provider = None
            embed_calls = []

            async def embed(text, timeout=None):
                embed_calls.append((text, timeout))
                return [1.0, 0.0, 0.0]

            class Vec:
                def __init__(self):
                    self.candidates = None

                async def search_topk(self, _vec, **kwargs):
                    self.candidates = set(kwargs.get("candidate_memo_names") or [])
                    return [{
                        "memo_name": "memos/rain", "chunk_text": "我们在雨夜约定不会突然消失。",
                        "ts_text": "2026-08-03", "relevance": 0.91, "score": 0.88,
                        "_matched_passages": [{"text": "我们在雨夜约定不会突然消失。", "passage_index": 0}],
                    }]

                def get_memo_meta(self, _memo_name):
                    return {}

            vec = Vec()
            plugin._vec = vec
            plugin._embed = embed
            try:
                hits, diagnostics, _facets = await plugin._episodic_recall_search(
                    "还记得雨夜的约定吗", "前面正在谈天气", "08月03日",
                )
                self.assertEqual(len(embed_calls), 1)
                self.assertEqual(vec.candidates, {"memos/rain"})
                self.assertEqual(hits[0]["memo_name"], "memos/rain")
                self.assertTrue(hits[0]["_episodic"])
                self.assertEqual(diagnostics["embedding_queries"], 1)
                self.assertFalse(diagnostics["month_branch"])
            finally:
                store.close()

    async def test_context_is_used_only_for_short_or_ambiguous_queries(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.episodic_candidate_pool = 18
        plugin.episodic_default_inject = 5
        plugin.episodic_narrative_inject = 8
        short = plugin._plan_episodic_query("那后来呢", "我们前面正在说雨夜约定")
        specific = plugin._plan_episodic_query("2026年8月3日雨夜在屋檐下答应过什么", "无关的上文")
        implicit = plugin._plan_episodic_query(
            "你都记得，不用我说了，我们没有可能了",
            "上一轮正在谈爱弥斯和那次关系决裂",
        )
        self.assertTrue(short["use_context"])
        self.assertIn("相关上文", short["search_text"])
        self.assertFalse(specific["use_context"])
        self.assertNotIn("无关的上文", specific["search_text"])
        self.assertTrue(implicit["use_context"])
        self.assertIn("关系决裂", implicit["search_text"])

    async def test_evidence_packet_distinguishes_grounded_and_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EpisodicStore(str(Path(temp) / "episodes.db"), 3, "test")
            await store.init()
            grounded = EpisodicStoreTests._episode(self)
            store.upsert_episode(
                memo_name="memos/grounded", episode=grounded,
                card_text="雨夜约定", embedding=[1.0, 0.0, 0.0],
                evidence_quality="source_grounded",
            )
            legacy = dict(grounded)
            legacy["scene_anchor"] = "旧日记里的车站告别"
            legacy["evidence"] = [{"detail": "旧日记记下了车站告别", "grounded": False}]
            store.upsert_episode(
                memo_name="memos/legacy", episode=legacy,
                card_text="车站告别", embedding=[0.0, 1.0, 0.0],
                evidence_quality="diary_derived",
            )
            plugin = object.__new__(MemosMemoryPlugin)
            plugin._episodes = store
            plugin.episodic_evidence_per_memory = 3
            plugin.episodic_default_inject = 5
            try:
                packet = plugin._format_episode_evidence_packet(
                    "约定和告别",
                    [
                        {"memo_name": "memos/grounded", "_episodic": True, "_necessary": True},
                        {"memo_name": "memos/legacy", "_episodic": True},
                    ],
                )
                self.assertIn("原始对话可追溯", packet)
                self.assertIn("由旧日记推导", packet)
                self.assertIn("不得把旧事件说成刚刚发生", packet)
                self.assertIn("尚未解决", packet)
            finally:
                store.close()

    async def test_adaptive_evidence_is_empty_for_ordinary_roleplay(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EpisodicStore(str(Path(temp) / "episodes.db"), 3, "test")
            await store.init()
            episode = EpisodicStoreTests._episode(self)
            store.upsert_episode(
                memo_name="memos/rain", episode=episode,
                card_text="雨夜约定", embedding=[1.0, 0.0, 0.0],
                evidence_quality="source_grounded",
            )
            plugin = object.__new__(MemosMemoryPlugin)
            plugin._episodes = store
            plugin.lean_recall_enable = True
            plugin.lean_adaptive_evidence_enable = True
            plugin.lean_story_max_inject = 4
            plugin.episodic_evidence_per_memory = 3
            try:
                packet = plugin._format_episode_evidence_packet(
                    "今晚想安静地靠一会儿",
                    [{"memo_name": "memos/rain", "memory_type": "plot_fact"}],
                )
                self.assertEqual(packet, "")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
