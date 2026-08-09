from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory import main as main_module
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
from astrbot_plugin_memos_memory.vector_store import VectorStore


class FirstTurnMigrationWaitTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, wait_seconds: float) -> MemosMemoryPlugin:
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.episode_migration_wait_seconds = wait_seconds
        plugin._episode_migration_ready = False
        plugin._log_event = lambda *args, **kwargs: None
        return plugin

    async def test_first_turn_waits_for_running_migration(self):
        plugin = self._plugin(2.0)

        async def migration():
            await asyncio.sleep(0.15)
            plugin._episode_migration_ready = True

        plugin._episode_migration_task = asyncio.create_task(migration())
        ready = await plugin._wait_episode_migration()
        self.assertTrue(ready)
        self.assertTrue(plugin._episode_migration_ready)

    async def test_wait_timeout_does_not_cancel_migration(self):
        plugin = self._plugin(0.05)
        finished = asyncio.Event()

        async def slow_migration():
            await asyncio.sleep(0.3)
            plugin._episode_migration_ready = True
            finished.set()

        task = asyncio.create_task(slow_migration())
        plugin._episode_migration_task = task
        ready = await plugin._wait_episode_migration()
        self.assertFalse(ready)
        self.assertFalse(task.cancelled())
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        self.assertTrue(plugin._episode_migration_ready)

    async def test_zero_wait_returns_immediately(self):
        plugin = self._plugin(0.0)
        plugin._episode_migration_task = asyncio.create_task(asyncio.sleep(1))
        ready = await plugin._wait_episode_migration()
        self.assertFalse(ready)
        plugin._episode_migration_task.cancel()


class LifecycleCoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_webui_start_creates_one_server(self):
        original = main_module.WebUIServer

        class FakeWebUI:
            starts = 0

            def __init__(self, _plugin):
                self.stops = 0

            async def start(self):
                FakeWebUI.starts += 1
                await asyncio.sleep(0.03)

            async def stop(self):
                self.stops += 1

        try:
            main_module.WebUIServer = FakeWebUI
            plugin = object.__new__(MemosMemoryPlugin)
            plugin.webui_enable = True
            plugin.webui_host = "127.0.0.1"
            plugin.webui_port = 18181
            plugin._webui = None
            plugin._webui_start_lock = asyncio.Lock()
            results = await asyncio.gather(
                plugin._ensure_webui_started(),
                plugin._ensure_webui_started(),
                plugin._ensure_webui_started(),
            )
            self.assertEqual(results, [True, True, True])
            self.assertEqual(FakeWebUI.starts, 1)
            self.assertIsInstance(plugin._webui, FakeWebUI)
        finally:
            main_module.WebUIServer = original

    async def test_terminate_cancels_and_awaits_warmup_before_webui_stop(self):
        events = []

        async def warmup():
            try:
                await asyncio.sleep(30)
            finally:
                events.append("warmup_done")

        class Controller:
            async def terminate(self):
                return None

        class WebUI:
            async def stop(self):
                events.append("webui_stopped")

        plugin = object.__new__(MemosMemoryPlugin)
        plugin._warmup_task = asyncio.create_task(warmup())
        await asyncio.sleep(0)
        plugin._xinchao = Controller()
        plugin._time_insight = Controller()
        plugin._semantic_state_pending_tasks = set()
        plugin._webui = WebUI()
        plugin._memos = None
        plugin._vec = None
        plugin._episodes = None
        await plugin.terminate()
        self.assertTrue(plugin._warmup_task.done())
        self.assertEqual(events, ["warmup_done", "webui_stopped"])
        self.assertIsNone(plugin._webui)


class ContextTrimGovernanceTests(unittest.TestCase):
    def _plugin(self, backup_dir: str, backup_enable: bool = True) -> MemosMemoryPlugin:
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.context_governance_enable = True
        plugin.context_keep_recent_messages = 40
        plugin.context_min_messages_before_trim = 80
        plugin.context_preserve_system_messages = True
        plugin.context_trim_backup_enable = backup_enable
        plugin.context_archive_backup_dir = backup_dir
        plugin._context_stats = {}
        plugin._log_event = lambda *args, **kwargs: None
        return plugin

    def _request(self, count: int = 100):
        contexts = [{"role": "user", "content": f"消息 {index}"} for index in range(count)]
        return SimpleNamespace(
            contexts=contexts,
            conversation=SimpleNamespace(cid="conv-1", token_usage=241667),
        )

    def test_trim_resets_stale_token_usage_and_writes_backup(self):
        event = SimpleNamespace(unified_msg_origin="test:umo")
        with tempfile.TemporaryDirectory() as temp:
            plugin = self._plugin(temp)
            req = self._request(100)
            plugin._govern_request_context(event, req)
            self.assertEqual(len(req.contexts), 40)
            self.assertEqual(req.conversation.token_usage, 0)
            stat = plugin._context_stats["test:umo"]
            self.assertTrue(stat.get("token_usage_reset"))
            backup_path = Path(stat.get("backup_path") or "")
            self.assertTrue(backup_path.exists())
            payload = backup_path.read_text(encoding="utf-8")
            self.assertIn("消息 0", payload)
            self.assertIn("消息 99", payload)

    def test_trim_is_skipped_when_backup_fails(self):
        event = SimpleNamespace(unified_msg_origin="test:umo")
        with tempfile.TemporaryDirectory() as temp:
            blocker = Path(temp) / "not-a-dir"
            blocker.write_text("occupied", encoding="utf-8")
            plugin = self._plugin(str(blocker))
            req = self._request(100)
            plugin._govern_request_context(event, req)
            self.assertEqual(len(req.contexts), 100)
            self.assertEqual(req.conversation.token_usage, 241667)

    def test_no_trim_below_threshold_keeps_token_usage(self):
        event = SimpleNamespace(unified_msg_origin="test:umo")
        with tempfile.TemporaryDirectory() as temp:
            plugin = self._plugin(temp)
            req = self._request(50)
            plugin._govern_request_context(event, req)
            self.assertEqual(len(req.contexts), 50)
            self.assertEqual(req.conversation.token_usage, 241667)


class FakeCommandEvent:
    def __init__(self, message: str = "/memos-sync"):
        self.message_str = message
        self.unified_msg_origin = "test:command"
        command_filter = type("CommandFilter", (), {})()
        handler = SimpleNamespace(event_filters=[command_filter])
        self.extras = {"activated_handlers": [handler]}

    def get_message_str(self):
        return self.message_str

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class CommandContextExclusionTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self) -> MemosMemoryPlugin:
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.enable = True
        plugin.context_exclude_command_turns = True
        plugin.rp_time_timezone = "Asia/Shanghai"
        plugin._command_context_candidates = {}
        plugin._command_context_locks = {}
        plugin._context_stats = {}
        plugin._log_event = lambda *args, **kwargs: None
        return plugin

    def test_command_detection_uses_activated_handler(self):
        event = FakeCommandEvent("memos-sync")
        self.assertTrue(MemosMemoryPlugin._event_is_command(event))
        event.extras["activated_handlers"] = []
        self.assertFalse(MemosMemoryPlugin._event_is_command(event))
        event.message_str = "/memos-sync"
        self.assertTrue(MemosMemoryPlugin._event_is_command(event))

    def test_strip_only_target_command_and_following_reply(self):
        plugin = self._plugin()
        history = [
            {"role": "user", "content": "普通对话"},
            {"role": "assistant", "content": "普通回复"},
            {"role": "user", "content": "/memos-sync"},
            {"role": "assistant", "content": "同步完成"},
            {"role": "user", "content": "继续聊天"},
            {"role": "assistant", "content": "好"},
        ]
        cleaned, turns, messages = plugin._strip_command_turns(
            history, {"memos-sync"}, minimum_index=2,
        )
        self.assertEqual(turns, 1)
        self.assertEqual(messages, 2)
        self.assertEqual(cleaned, history[:2] + history[4:])

    def test_minimum_index_prevents_old_equal_text_from_being_deleted(self):
        plugin = self._plugin()
        history = [
            {"role": "user", "content": "memos-sync"},
            {"role": "assistant", "content": "这里是普通讨论"},
            {"role": "user", "content": "/memos-sync"},
            {"role": "assistant", "content": "同步完成"},
        ]
        cleaned, turns, messages = plugin._strip_command_turns(
            history, {"memos-sync"}, minimum_index=2,
        )
        self.assertEqual((turns, messages), (1, 2))
        self.assertEqual(cleaned, history[:2])

    async def test_command_request_isolated_before_normal_injection_chain(self):
        plugin = self._plugin()
        event = FakeCommandEvent("memos-sync")
        req = SimpleNamespace(
            prompt="memos-sync",
            contexts=[
                {"role": "user", "content": "普通对话"},
                {"role": "assistant", "content": "普通回复"},
            ],
            conversation=SimpleNamespace(token_usage=100),
        )
        await plugin.on_llm_request(event, req)
        self.assertTrue(event.get_extra("memos_memory_command_event"))
        self.assertEqual(event.get_extra("memos_memory_command_history_before"), 2)
        self.assertEqual(req.contexts[0]["content"], "普通对话")

    async def test_after_send_removes_persisted_command_pair(self):
        plugin = self._plugin()
        event = FakeCommandEvent("memos-sync")
        event.set_extra("memos_memory_command_event", True)
        event.set_extra("memos_memory_command_texts", ["memos-sync"])
        event.set_extra("memos_memory_command_history_before", 2)
        history = [
            {"role": "user", "content": "普通对话"},
            {"role": "assistant", "content": "普通回复"},
            {"role": "user", "content": "memos-sync"},
            {"role": "assistant", "content": "同步完成"},
        ]

        class Manager:
            def __init__(self):
                self.updated = None

            async def get_curr_conversation_id(self, _umo):
                return "conv-1"

            async def get_conversation(self, _umo, _cid):
                return SimpleNamespace(history=history)

            async def update_conversation(self, umo, cid, *, history, token_usage):
                self.updated = (umo, cid, history, token_usage)

        manager = Manager()
        plugin.context = SimpleNamespace(conversation_manager=manager)
        removed = await plugin._remove_persisted_command_turn(event)
        self.assertEqual(removed, 2)
        self.assertEqual(manager.updated[2], history[:2])
        self.assertEqual(manager.updated[3], 0)

    async def test_command_response_does_not_enter_memory_or_xinchao(self):
        plugin = self._plugin()
        plugin._extract_cache_token_usage = lambda _response: {}
        event = FakeCommandEvent("memos-sync")
        response = SimpleNamespace(completion_text="同步完成")
        await plugin.on_llm_response(event, response)
        self.assertIn("memos-sync", plugin._recent_command_candidates(event.unified_msg_origin))

class BatchedFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = VectorStore(str(Path(self.temp.name) / "memories.db"), 3, "test-embedding")
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_batched_feedback_matches_single_lookup(self):
        self.store.feedback_record(
            "req-1", "memos/rain", "雨夜的约定还记得吗", "helpful", 0.12,
            query_embedding=[1.0, 0.0, 0.0],
        )
        self.store.feedback_record(
            "req-2", "memos/rain", "雨夜约定", "key_memory", 0.08,
            query_embedding=[0.9, 0.1, 0.0],
        )
        self.store.feedback_record(
            "req-3", "memos/other", "完全无关的问题", "incorrect", -0.10,
            query_embedding=[0.0, 0.0, 1.0],
        )
        query_vec = [1.0, 0.0, 0.0]
        query_text = "雨夜的约定还记得吗"
        batch = self.store.feedback_effects_for_query(
            ["memos/rain", "memos/other", "memos/none"], query_vec, query_text,
        )
        for name in ("memos/rain", "memos/other", "memos/none"):
            single = self.store.feedback_effect_for_query(name, query_vec, query_text)
            self.assertEqual(batch[name], single, name)
        self.assertGreater(batch["memos/rain"]["boost"], 0.0)
        self.assertEqual(batch["memos/none"]["matches"], 0)


class EpisodicLexicalRescueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(
            str(Path(self.temp.name) / "episodic.db"), 3, "test-embedding",
        )
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _episode(self, anchor: str) -> dict:
        return {
            "occurred_at": "2026-08-01 21:00",
            "event_ts": 1785589200.0,
            "time_basis": "explicit",
            "memory_type": "plot_fact",
            "importance": 3,
            "scene_anchor": anchor,
            "retrieval_key": anchor,
            "state_change": "",
            "long_effect": "",
            "trigger_hint": "",
            "entities": [],
            "affect_before": "",
            "affect_after": "",
            "unresolved": [],
            "evidence": [{
                "kind": "dialogue", "actor": "user", "detail": anchor,
                "quote": "", "turn_indexes": [0], "confidence": 0.8, "grounded": True,
            }],
        }

    def test_sql_lexical_rescue_finds_card_outside_ann_window(self):
        # Semantic vector points away from the query so only the lexical path
        # can rescue this card. The term contains an underscore to exercise
        # LIKE-wildcard escaping.
        self.store.upsert_episode(
            memo_name="memos/special",
            episode=self._episode("我们讨论了 memo_name 索引和银杏叶书签"),
            card_text="2026-08-01 我们讨论了 memo_name 索引和银杏叶书签",
            embedding=[0.0, 1.0, 0.0],
            legacy=False,
            evidence_quality="source_grounded",
        )
        self.store.upsert_episode(
            memo_name="memos/other",
            episode=self._episode("普通的下午散步"),
            card_text="2026-08-01 普通的下午散步",
            embedding=[0.0, 0.0, 1.0],
            legacy=False,
            evidence_quality="source_grounded",
        )
        hits = self.store.search_cards([1.0, 0.0, 0.0], "银杏叶书签在哪", limit=5)
        names = [str(item.get("memo_name")) for item in hits]
        self.assertIn("memos/special", names)
        self.assertEqual(names[0], "memos/special")
        underscore_hits = self.store.search_cards([1.0, 0.0, 0.0], "memo_name", limit=5)
        underscore_names = [str(item.get("memo_name")) for item in underscore_hits]
        self.assertIn("memos/special", underscore_names)
        self.assertEqual(underscore_names[0], "memos/special")


class SafetyNetQualificationTests(unittest.TestCase):
    def _plugin(self) -> MemosMemoryPlugin:
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_injection_min_score = 0.62
        plugin.min_similarity_to_inject = 0.52
        plugin.lean_story_min_inject = 1
        plugin.lean_story_max_inject = 6
        plugin.lean_texture_enable = False
        plugin._memory_layer = lambda _hit: "plot"
        return plugin

    def test_safety_net_rescue_qualifies_slightly_below_min_score(self):
        plugin = self._plugin()
        hits = [
            {"memo_name": "memos/strong", "_rerank_score": 0.80},
            {"memo_name": "memos/rescued", "_rerank_score": 0.57, "_safety_net_rescue": True},
            {"memo_name": "memos/weak", "_rerank_score": 0.57},
        ]
        selected, diag, annotated = plugin._postprocess_lean_hits(
            "随便聊聊", hits, {"target": 3, "narrative": False}, set(),
        )
        names = {str(hit.get("memo_name")) for hit in selected}
        self.assertIn("memos/rescued", names)
        by_name = {str(hit.get("memo_name")): hit for hit in annotated}
        self.assertTrue(by_name["memos/rescued"]["_qualified"])
        self.assertFalse(by_name["memos/weak"]["_qualified"])

    def test_safety_net_rescue_still_rejected_below_similarity_floor(self):
        plugin = self._plugin()
        hits = [
            {"memo_name": "memos/strong", "_rerank_score": 0.80},
            {"memo_name": "memos/too-weak", "_rerank_score": 0.40, "_safety_net_rescue": True},
        ]
        _selected, _diag, annotated = plugin._postprocess_lean_hits(
            "随便聊聊", hits, {"target": 2, "narrative": False}, set(),
        )
        by_name = {str(hit.get("memo_name")): hit for hit in annotated}
        self.assertFalse(by_name["memos/too-weak"]["_qualified"])

    def test_same_memo_rescue_merges_stronger_score_and_distinct_evidence(self):
        plugin = self._plugin()
        existing = {
            "memo_name": "memos/same",
            "relevance": 0.44,
            "score": 0.46,
            "_route_evidence": [{"route": "passage_hybrid", "rank": 9}],
            "_matched_passages": [{"chunk_id": 1, "passage_index": 0}],
        }
        rescue = {
            "memo_name": "memos/same",
            "relevance": 0.69,
            "score": 0.65,
            "_rerank_score": 0.72,
            "bm25_relevance": 0.61,
            "lexical_rescue": True,
            "_route_evidence": [
                {"route": "passage_hybrid", "rank": 9},
                {"route": "relationship", "rank": 1},
            ],
            "_matched_passages": [
                {"chunk_id": 1, "passage_index": 0},
                {"chunk_id": 2, "passage_index": 1},
            ],
        }
        changed = plugin._merge_safety_net_candidate(existing, rescue)
        self.assertTrue(changed)
        self.assertEqual(existing["_rerank_score"], 0.72)
        self.assertEqual(existing["relevance"], 0.69)
        self.assertEqual(existing["bm25_relevance"], 0.61)
        self.assertTrue(existing["_safety_net_rescue"])
        self.assertTrue(existing["lexical_rescue"])
        self.assertEqual(len(existing["_route_evidence"]), 2)
        self.assertEqual(len(existing["_matched_passages"]), 2)

        selected, _diag, _annotated = plugin._postprocess_lean_hits(
            "我们之前的关系", [existing], {"target": 1, "narrative": False}, set(),
        )
        self.assertEqual([item["memo_name"] for item in selected], ["memos/same"])


class BudgetedInjectionFormatTests(unittest.TestCase):
    def _plugin(self) -> MemosMemoryPlugin:
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.lean_recall_enable = True
        plugin.inject_compact_chars = 100
        return plugin

    def test_compact_mode_truncates_diary_and_limits_evidence(self):
        plugin = self._plugin()
        content = "这是一段很长的日记正文。" * 30
        hit = {
            "ts_text": "2026-08-01", "occurred_at": "2026-08-01",
            "event_ts": 1785589200.0, "time_basis": "explicit",
            "memory_type": "plot_fact",
            "_fusion_event_core": ["事件: 雨夜里的约定"],
            "_fusion_source_evidence": ["用户: 第一条证据", "角色: 第二条证据"],
        }
        block = plugin._format_inject_block(
            hit, content, passage_mode=True, compact_mode=True,
            reference_now_ts=1785675600.0,
        )
        self.assertIn("紧凑摘录", block)
        self.assertIn("…", block)
        self.assertIn("事件: 雨夜里的约定", block)
        self.assertIn("第一条证据", block)
        self.assertNotIn("第二条证据", block)
        full_block = plugin._format_inject_block(
            hit, content, passage_mode=False, reference_now_ts=1785675600.0,
        )
        self.assertIn("完整日记", full_block)
        self.assertGreater(len(full_block), len(block))


class AdaptiveSourceEvidenceTests(unittest.TestCase):
    class _EpisodeRepo:
        @staticmethod
        def get_episode(_memo_name: str):
            return {
                "scene_anchor": "雨夜里重新确认约定",
                "state_change": "从犹疑转为愿意相信",
                "unresolved": [],
                "memory_type": "promise_or_rule",
                "trigger_hint": "离开前必须说明",
                "evidence_quality": "source_grounded",
            }

        @staticmethod
        def evidence_for_memo(_memo_name: str, _query: str, limit: int = 3):
            return [
                {"actor": "用户", "quote_text": f"证据原话{i}", "match_score": 0.9 - i * 0.01}
                for i in range(1, limit + 1)
            ]

    def _plugin(self) -> MemosMemoryPlugin:
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self._EpisodeRepo()
        plugin.character_name = "角色"
        plugin.episodic_evidence_per_memory = 3
        plugin.lean_adaptive_evidence_enable = True
        plugin.lean_story_max_inject = 10
        return plugin

    def test_event_core_remains_when_source_expansion_is_not_selected(self):
        plugin = self._plugin()
        hit = {"memo_name": "memos/rain", "_source_turn_hits": []}
        plugin._prepare_fused_memory_hit(
            "今晚想安静地靠一会儿", hit, include_source_evidence=False,
        )
        self.assertTrue(hit["_fusion_event_core"])
        self.assertEqual(hit["_fusion_source_evidence"], [])

    def test_precise_query_uses_configured_evidence_limit(self):
        plugin = self._plugin()
        hit = {"memo_name": "memos/rain", "_source_turn_hits": []}
        plugin._prepare_fused_memory_hit(
            "把当时的原话和证据告诉我", hit, include_source_evidence=True,
        )
        self.assertEqual(len(hit["_fusion_source_evidence"]), 3)
        self.assertIn("证据原话3", hit["_fusion_source_evidence"][2])

    def test_adaptive_selector_limits_memory_groups(self):
        plugin = self._plugin()
        hits = [
            {"memo_name": "memos/a", "memory_type": "plot_fact", "_injection_score": 0.9},
            {"memo_name": "memos/b", "memory_type": "promise_or_rule", "_injection_score": 0.8},
            {"memo_name": "memos/c", "memory_type": "plot_fact", "_injection_score": 0.7},
        ]
        ordinary, ordinary_mode = plugin._select_lean_evidence_hits("今晚抱抱我", hits)
        self.assertEqual(ordinary_mode, "constraint")
        self.assertEqual([item["memo_name"] for item in ordinary], ["memos/b"])
        precise, precise_mode = plugin._select_lean_evidence_hits("当时具体说过什么原话", hits)
        self.assertEqual(precise_mode, "precise")
        self.assertEqual(len(precise), 2)


class EpisodeCardTextTests(unittest.TestCase):
    def test_card_text_keeps_quotes_dates_and_prioritizes_missing_details(self):
        diary = {
            "occurred_at": "2026-08-01 21:00",
            "scene_anchor": "雨夜屋檐下的约定",
            "retrieval_key": "约定 雨夜",
            "entities": ["我", "你"],
            "state_change": "彼此更信任",
            "unresolved": [],
            "_render_missing": ["递给我一枚银杏叶书签"],
            "evidence": [
                {"kind": "dialogue", "actor": "user", "detail": "答应离开前一定说明",
                 "quote": "我走之前一定告诉你", "turn_indexes": [0], "confidence": 0.9},
                {"kind": "object", "actor": "user", "detail": "递给我一枚银杏叶书签",
                 "quote": "", "turn_indexes": [1], "confidence": 0.8},
            ],
        }
        card = MemosMemoryPlugin._episode_card_text(diary)
        self.assertIn("2026-08-01", card)
        self.assertIn("「我走之前一定告诉你」", card)
        self.assertIn("递给我一枚银杏叶书签", card)
        evidence_section = card.splitlines()[-1]
        self.assertLess(
            evidence_section.find("银杏叶书签"),
            evidence_section.find("答应离开前一定说明"),
        )


if __name__ == "__main__":
    unittest.main()
