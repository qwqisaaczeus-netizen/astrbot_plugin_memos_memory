from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
from astrbot_plugin_memos_memory.diary_pipeline import DiaryGenerationDeferred
from astrbot_plugin_memos_memory.llm_runtime import (
    LLMEmptyFinalError, LLMRouteExhaustedError,
)
from astrbot_plugin_memos_memory.episode_recovery import (
    build_local_grounded_episodes, project_grounded_evidence_for_render,
)
from astrbot_plugin_memos_memory.scene_splitter import SceneSplitter


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
        ).fetchone()["value"], str(EpisodicStore.SCHEMA_VERSION))
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

    async def test_semantic_state_queue_isolated_by_character_scope(self):
        batch_a = self.store.archive_batch(
            "scope-a", [{"role": "user", "content": "A 的变化"}], "auto",
        )
        batch_b = self.store.archive_batch(
            "scope-b", [{"role": "user", "content": "B 的变化"}], "auto",
        )
        self.store.enqueue_state_update(batch_a, "character:A", ["ep-a"])
        self.store.enqueue_state_update(batch_b, "character:B", ["ep-b"])

        pending_a = self.store.pending_state_updates(scope_id="character:A")
        pending_b = self.store.pending_state_updates(scope_id="character:B")
        self.assertEqual([item["batch_id"] for item in pending_a], [batch_a])
        self.assertEqual([item["batch_id"] for item in pending_b], [batch_b])
        self.assertEqual(self.store.pending_state_update_count("character:A"), 1)
        self.assertEqual(self.store.pending_state_update_count("character:B"), 1)
        self.assertEqual(self.store.pending_state_update_count(), 2)

        changed = self.store.supersede_pending_state_updates(
            "rebuild-a", scope_id="character:A",
        )
        self.assertEqual(changed, 1)
        self.assertFalse(self.store.pending_state_updates(scope_id="character:A"))
        self.assertEqual(
            [item["batch_id"] for item in self.store.pending_state_updates(scope_id="character:B")],
            [batch_b],
        )

    async def test_completed_state_queue_is_not_reopened_by_duplicate_enqueue(self):
        batch_id = self.store.archive_batch(
            "duplicate", [{"role": "user", "content": "同一批次"}], "auto",
        )
        scope = "character:爱莉"
        self.store.enqueue_state_update(batch_id, scope, ["ep-one"])
        self.store.mark_state_update(batch_id, "done")
        self.store.enqueue_state_update(batch_id, scope, ["ep-one"])
        self.assertFalse(self.store.pending_state_updates(scope_id=scope))

    async def test_orphan_state_queue_does_not_block_valid_batch(self):
        scope = "character:爱莉"
        self.store.upsert_semantic_state(
            scope, {"relationship_position": "旧状态"}, reason="bootstrap",
        )
        orphan = self.store.archive_batch(
            "orphan", [{"role": "user", "content": "没有 Episode"}], "auto",
        )
        valid = self.store.archive_batch(
            "valid", [{"role": "user", "content": "确认重要承诺"}], "auto",
        )
        episode = {
            **self._episode(),
            "memory_type": "promise_or_rule",
            "importance": 5,
            "scene_anchor": "确认重要承诺",
            "state_change": "双方答应不会失约",
        }
        saved_episode = self.store.upsert_episode(
            memo_name="memos/valid", episode=episode,
            card_text="确认重要承诺", embedding=[1.0, 0.0, 0.0],
            source_batch_id=valid, evidence_quality="source_grounded",
        )
        self.store.enqueue_state_update(orphan, scope, [])
        self.store.enqueue_state_update(valid, scope, [saved_episode])

        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_enable = True
        plugin.semantic_state_update_policy = "adaptive"
        plugin.semantic_state_batch_threshold = 3
        plugin.semantic_state_max_wait_hours = 72
        plugin.semantic_state_significance_threshold = 0.72
        plugin.semantic_state_merge_max_batches = 6
        plugin.semantic_state_target_chars = 1800
        plugin.semantic_state_provider_id = ""
        plugin.semantic_state_timeout = 30
        plugin.enable_affiliate_profile = False
        plugin._semantic_state_lock = asyncio.Lock()
        plugin._semantic_state_last_defer_key = ""
        plugin._log_event = lambda *_args, **_kwargs: None

        async def call(_prompt, **_kwargs):
            return json.dumps({
                "relationship_position": "新状态",
                "commitments_boundaries": "不会失约",
                "behavior_tendencies": "主动兑现",
                "emotional_baseline": "坚定",
                "open_loops": "",
            }, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        result = await plugin._drain_semantic_state_queue(limit=6)
        self.assertEqual(result["updated"], 1)
        self.assertFalse(self.store.pending_state_updates(scope_id=scope))
        row = self.store._connect().execute(
            "SELECT status,last_error FROM semantic_state_queue WHERE batch_id=?",
            (orphan,),
        ).fetchone()
        self.assertEqual(row["status"], "superseded")
        self.assertEqual(row["last_error"], "episode_view_missing")

    async def test_state_batch_arriving_during_merge_is_checked_immediately_afterward(self):
        first = self.store.archive_batch(
            "during-merge-a", [{"role": "user", "content": "第一批"}], "auto",
        )
        second = self.store.archive_batch(
            "during-merge-b", [{"role": "user", "content": "第二批"}], "auto",
        )
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_enable = True
        plugin._semantic_state_pending_tasks = set()
        plugin._semantic_state_reschedule_needed = False
        plugin._terminating = False
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def drain():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return {"updated": 0, "reason": "test"}

        plugin._drain_semantic_state_queue = drain
        episodes = [{"episode_id": "ep", "scene_anchor": "变化"}]
        plugin._schedule_semantic_state_update(
            episodes, source_batch_id=first, reason="test",
        )
        await started.wait()
        plugin._schedule_semantic_state_update(
            episodes, source_batch_id=second, reason="test",
        )
        self.assertTrue(plugin._semantic_state_reschedule_needed)
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if len(calls) >= 2 and not plugin._semantic_state_pending_tasks:
                break
        self.assertEqual(calls, [1, 2])
        self.assertFalse(plugin._semantic_state_reschedule_needed)
        self.assertFalse(plugin._semantic_state_pending_tasks)

    async def test_semantic_state_drain_does_not_consume_another_character_queue(self):
        scope_a = "character:A"
        scope_b = "character:B"
        self.store.upsert_semantic_state(
            scope_a, {"relationship_position": "A 的旧状态"}, reason="bootstrap",
        )
        for scope, label in ((scope_a, "A"), (scope_b, "B")):
            batch_id = self.store.archive_batch(
                f"scope-{label}",
                [{"role": "user", "content": f"{label} 的重大约定"}],
                "auto",
            )
            episode = {
                **self._episode(),
                "episode_id": f"ep-{label}",
                "memory_type": "promise_or_rule",
                "importance": 5,
                "scene_anchor": f"{label} 的重大约定",
                "retrieval_key": f"{label} 承诺",
                "state_change": "双方确认了不会失约的承诺",
            }
            self.store.upsert_episode(
                memo_name=f"memos/scope-{label}", episode=episode,
                card_text=f"{label} 的重大约定", embedding=[1.0, 0.0, 0.0],
                source_batch_id=batch_id, evidence_quality="source_grounded",
            )
            self.store.enqueue_state_update(batch_id, scope, [f"ep-{label}"])

        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "A"
        plugin.semantic_state_enable = True
        plugin.semantic_state_update_policy = "adaptive"
        plugin.semantic_state_batch_threshold = 3
        plugin.semantic_state_max_wait_hours = 72
        plugin.semantic_state_significance_threshold = 0.72
        plugin.semantic_state_merge_max_batches = 6
        plugin.semantic_state_target_chars = 1800
        plugin.semantic_state_provider_id = ""
        plugin.semantic_state_timeout = 30
        plugin.semantic_state_replace_profile = True
        plugin.enable_affiliate_profile = False
        plugin._semantic_state_lock = asyncio.Lock()
        plugin._semantic_state_last_defer_key = ""
        plugin._log_event = lambda *_args, **_kwargs: None

        async def call(prompt, **_kwargs):
            self.assertIn("A 的重大约定", prompt)
            self.assertNotIn("B 的重大约定", prompt)
            return json.dumps({
                "relationship_position": "A 的新状态",
                "commitments_boundaries": "不会失约",
                "behavior_tendencies": "主动兑现",
                "emotional_baseline": "坚定",
                "open_loops": "",
            }, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        result = await plugin._drain_semantic_state_queue(limit=6)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.store.pending_state_update_count(scope_a), 0)
        self.assertEqual(self.store.pending_state_update_count(scope_b), 1)
        status = plugin._semantic_state_status()
        self.assertEqual(status["pending"], 0)
        self.assertIn("A 的新状态", status["state"]["rendered_text"])

    async def test_adaptive_state_cadence_defers_daily_batches_but_never_decisive_change(self):
        scope = "character:爱莉"
        self.store.upsert_semantic_state(
            scope, {"relationship_position": "关系稳定"}, reason="bootstrap",
        )
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_update_policy = "adaptive"
        plugin.semantic_state_batch_threshold = 3
        plugin.semantic_state_max_wait_hours = 72
        plugin.semantic_state_significance_threshold = 0.72
        ordinary = [{
            "memory_type": "daily_texture", "importance": 3,
            "scene_anchor": "一起吃了晚饭", "evidence": [],
        }]
        one = [{"batch_id": "b1", "status": "pending", "created_ts": time.time()}]
        decision = plugin._semantic_state_update_decision(one, ordinary)
        self.assertFalse(decision["update"])
        self.assertEqual(decision["reason"], "minimum_interval")
        casual_agreement = [{
            "memory_type": "daily_texture", "importance": 3,
            "scene_anchor": "早饭时约定晚点一起去买菜",
            "state_change": "日常安排更自然", "long_effect": "", "evidence": [],
        }]
        casual_signal = plugin._semantic_state_change_score(casual_agreement)
        self.assertFalse(casual_signal["hard"])
        self.assertFalse(plugin._semantic_state_update_decision(one, casual_agreement)["update"])

        three = [
            {"batch_id": f"b{i}", "status": "pending", "created_ts": time.time()}
            for i in range(3)
        ]
        self.assertEqual(
            plugin._semantic_state_update_decision(three, ordinary)["reason"],
            "minimum_interval",
        )
        decisive = plugin._semantic_state_update_decision(one, [self._episode()])
        self.assertTrue(decisive["update"])
        self.assertEqual(decisive["reason"], "hard_change")
        self.assertTrue(decisive["signal"]["hard"])

    async def test_adaptive_state_cadence_uses_max_wait_and_marks_merged_batches(self):
        scope = "character:爱莉"
        self.store.upsert_semantic_state(
            scope, {"relationship_position": "关系稳定"}, reason="bootstrap",
        )
        old_ts = time.time() - 200 * 3600
        self.store._connect().execute(
            "UPDATE semantic_states SET updated_ts=? WHERE scope_id=?", (old_ts, scope),
        )
        self.store._connect().commit()
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_update_policy = "adaptive"
        plugin.semantic_state_batch_threshold = 3
        plugin.semantic_state_max_wait_hours = 72
        plugin.semantic_state_significance_threshold = 0.72
        old = [{
            "batch_id": "old", "status": "pending",
            "created_ts": time.time() - 73 * 3600,
        }]
        ordinary = [{
            "memory_type": "behavior_bias", "importance": 4,
            "state_change": "逐渐更愿意主动分享", "evidence": [],
        }]
        self.assertEqual(
            plugin._semantic_state_update_decision(old, ordinary)["reason"],
            "max_wait_meaningful",
        )

        batch_ids = [
            self.store.archive_batch(
                session_id, [{"role": "user", "content": session_id}], "auto",
            )
            for session_id in ("merge-a", "merge-b")
        ]
        for batch_id in batch_ids:
            self.store.enqueue_state_update(batch_id, scope, [])
        self.assertEqual(
            self.store.mark_state_updates(batch_ids, "done"), 2,
        )
        self.assertFalse(any(
            item["batch_id"] in set(batch_ids)
            for item in self.store.pending_state_updates()
        ))

    async def test_state_pending_preview_is_read_only_and_explains_decision(self):
        scope = "character:爱莉"
        self.store.upsert_semantic_state(
            scope, {"relationship_position": "关系稳定"}, reason="bootstrap",
        )
        batch_id = self.store.archive_batch(
            "preview", [{"role": "user", "content": "一起吃了晚饭"}], "auto",
        )
        episode = {
            **self._episode(),
            "memory_type": "daily_texture",
            "importance": 2,
            "scene_anchor": "一起吃了晚饭",
            "retrieval_key": "晚饭 日常",
            "state_change": "",
            "long_effect": "",
            "trigger_hint": "",
            "entities": ["晚饭"],
            "affect_before": "平静",
            "affect_after": "平静",
            "unresolved": [],
            "evidence": [],
        }
        self.store.upsert_episode(
            memo_name="memos/preview", episode=episode,
            card_text="一起吃了晚饭", embedding=[1.0, 0.0, 0.0],
            source_batch_id=batch_id, evidence_quality="source_grounded",
        )
        self.store.enqueue_state_update(batch_id, scope, [])
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_update_policy = "adaptive"
        plugin.semantic_state_batch_threshold = 3
        plugin.semantic_state_max_wait_hours = 72
        plugin.semantic_state_significance_threshold = 0.72
        plugin.semantic_state_merge_max_batches = 6

        before = self.store.pending_state_updates()
        preview = plugin._semantic_state_pending_preview()
        after = self.store.pending_state_updates()
        self.assertEqual(preview["reason"], "minimum_interval")
        self.assertFalse(preview["update"])
        self.assertEqual(preview["pending_batches"], 1)
        self.assertEqual(before, after)

    async def test_cluster_diagnostics_do_not_compute_clusters_when_apply_is_off(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.recall_cluster_fold_enable = True
        plugin.recall_cluster_fold_apply = False
        plugin._cluster_candidate_hits = lambda _hits: self.fail(
            "diagnostic-only clusters must not run in the request path"
        )
        hits = [{"memo_name": "memos/a"}, {"memo_name": "memos/b"}]
        kept, diagnostics = plugin._apply_cluster_fold(hits)
        self.assertEqual(kept, hits)
        self.assertEqual(diagnostics["reason"], "diagnostic_only")
        self.assertEqual(diagnostics["folded"], 0)

    async def test_adaptive_state_drain_does_not_rewrite_for_three_ordinary_batches(self):
        scope = "character:爱莉"
        self.store.upsert_semantic_state(
            scope, {"relationship_position": "关系稳定"}, reason="bootstrap",
        )
        batch_ids = []
        for index in range(3):
            batch_id = self.store.archive_batch(
                f"ordinary-{index}",
                [{"role": "user", "content": f"普通日常 {index}"}],
                "auto",
            )
            episode = {
                **self._episode(),
                "memory_type": "daily_texture",
                "importance": 2,
                "scene_anchor": f"普通日常 {index}",
                "retrieval_key": f"日常 {index}",
                "state_change": "",
                "long_effect": "",
                "unresolved": [],
                "evidence": [],
            }
            self.store.upsert_episode(
                memo_name=f"memos/ordinary-{index}", episode=episode,
                card_text=f"普通日常 {index}", embedding=[1.0, 0.0, 0.0],
                source_batch_id=batch_id, evidence_quality="source_grounded",
            )
            self.store.enqueue_state_update(batch_id, scope, [])
            batch_ids.append(batch_id)

        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_enable = True
        plugin.semantic_state_update_policy = "adaptive"
        plugin.semantic_state_batch_threshold = 3
        plugin.semantic_state_max_wait_hours = 72
        plugin.semantic_state_significance_threshold = 0.72
        plugin.semantic_state_merge_max_batches = 6
        plugin.semantic_state_target_chars = 1800
        plugin.semantic_state_provider_id = ""
        plugin.semantic_state_timeout = 30
        plugin.enable_affiliate_profile = False
        plugin._semantic_state_lock = asyncio.Lock()
        plugin._semantic_state_last_defer_key = ""
        plugin._log_event = lambda *_args, **_kwargs: None
        calls = []

        async def call(prompt, **_kwargs):
            calls.append(prompt)
            return json.dumps({
                "relationship_position": "关系稳定，日常相处更自然",
                "commitments_boundaries": "",
                "behavior_tendencies": "更自然地分享日常",
                "emotional_baseline": "平静亲近",
                "open_loops": "",
            }, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        result = await plugin._drain_semantic_state_queue(limit=6)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["reason"], "minimum_interval")
        self.assertEqual(len(calls), 0)
        self.assertEqual(self.store.get_semantic_state(scope)["version"], 1)
        self.assertEqual(len(self.store.pending_state_updates()), 3)

    async def test_adaptive_state_updates_after_four_distinct_meaningful_axes(self):
        scope = "character:爱莉"
        self.store.upsert_semantic_state(
            scope, {"relationship_position": "关系稳定"}, reason="bootstrap",
        )
        self.store._connect().execute(
            "UPDATE semantic_states SET updated_ts=? WHERE scope_id=?",
            (time.time() - 100 * 3600, scope),
        )
        self.store._connect().commit()
        episodes = [
            {"memory_type": "behavior_bias", "importance": 4, "state_change": "更愿意主动分享"},
            {"memory_type": "emotional_anchor", "importance": 4, "long_effect": "安心感延续"},
            {"memory_type": "plot_fact", "importance": 4, "unresolved": ["仍要兑现一件事"]},
            {"memory_type": "plot_fact", "importance": 5, "state_change": "关系理解变得更坚定"},
        ]
        pending = [{
            "batch_id": "meaningful", "status": "pending",
            "created_ts": time.time() - 80 * 3600,
        }]
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_update_policy = "adaptive"
        plugin.semantic_state_batch_threshold = 4
        plugin.semantic_state_max_wait_hours = 168
        plugin.semantic_state_min_interval_hours = 72
        plugin.semantic_state_significance_threshold = 0.95
        decision = plugin._semantic_state_update_decision(pending, episodes)
        self.assertTrue(decision["update"])
        self.assertEqual(decision["reason"], "meaningful_delta_threshold")
        self.assertEqual(decision["delta"]["cluster_count"], 4)

    async def test_semantic_state_noop_does_not_create_another_version(self):
        scope = "character:爱莉"
        state = {
            "relationship_position": "关系稳定",
            "commitments_boundaries": "离开前会说明",
            "behavior_tendencies": "愿意主动确认",
            "emotional_baseline": "平静亲近",
            "open_loops": "等待一次共同出行",
        }
        self.store.upsert_semantic_state(scope, state, reason="bootstrap")
        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = self.store
        plugin.character_name = "爱莉"
        plugin.semantic_state_enable = True
        plugin.semantic_state_target_chars = 1200
        plugin.semantic_state_provider_id = ""
        plugin.semantic_state_timeout = 30
        plugin.semantic_state_noop_similarity = 0.94
        plugin._semantic_state_lock = asyncio.Lock()
        plugin._log_event = lambda *_args, **_kwargs: None

        async def call(_prompt, **_kwargs):
            return json.dumps(state, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        result = await plugin._update_semantic_state([
            {"episode_id": "same", "memory_type": "behavior_bias", "importance": 4},
        ], reason="test_noop")
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "no_material_change")
        self.assertEqual(self.store.get_semantic_state(scope)["version"], 1)

    def test_identity_core_is_compact_and_separate_from_current_state(self):
        plugin = object.__new__(MemosMemoryPlugin)
        plugin.enable_affiliate_profile = True
        plugin.identity_core_inject_chars = 520
        plugin._affiliate_profile_status = lambda: {
            "connected": True,
            "fresh": True,
            "profile": "她重视承诺，也会在不安时先观察。" * 30,
            "profile_facts": "- 不喜欢突然失联\n- 会认真回应明确约定\n" * 10,
        }
        block = plugin._identity_core_injection_block()
        self.assertIn("LongTermIdentityCore", block)
        self.assertIn("不是近期情绪", block)
        self.assertLess(len(block), 850)

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
        plugin.time_insight_llm_provider_id = ""
        plugin.diary_render_provider_id = ""
        plugin.diary_render_timeout = 30.0
        return plugin

    def test_short_scene_length_hint_stays_below_publication_ratio(self):
        lower, upper, hint = MemosMemoryPlugin._diary_length_hint(443)
        self.assertGreaterEqual(lower, 120)
        self.assertLess(upper, int(443 * 0.82))
        self.assertIn(f"必须不超过{upper}字", hint)

    async def test_runtime_config_initializes_compensation_fast_provider(self):
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
                "episode_extraction_provider_id": "slow-pro",
                "time_insight_llm_provider_id": "fast-structured",
            })
            self.assertEqual(
                plugin.time_insight_llm_provider_id, "fast-structured"
            )

    async def test_long_eod_reserves_budget_for_compact_recovery(self):
        plugin = self.make_plugin()
        plugin.episode_extraction_timeout = 150.0
        plugin._resolve_chat_provider = lambda _id: None
        calls = []

        async def fail(_prompt, *, provider_id, timeout, label):
            calls.append((label, timeout))
            raise asyncio.TimeoutError()

        plugin._call_memory_generation_llm = fail
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": (
                 "今晚谈到私塾与家里的开销。" if index < 16
                 else "后来回屋休息，也想起旧事与约定。"
             ) * 12,
             "event_ts": 1790160000 + index * 60,
             "event_timezone": "Asia/Shanghai"}
            for index in range(30)
        ]
        episodes, mode, errors, _timing = await plugin._extract_episode_blueprints_resilient(
            messages,
            "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
            2, 3, SceneSplitter(max_scenes=6).detect(messages),
            exact_count=False, source_kind="eod",
        )
        self.assertEqual(calls[0], ("episode_extract", 90.0))
        self.assertEqual(calls[1][0], "episode_extract_compact_retry")
        self.assertGreater(calls[1][1], 40.0)
        self.assertEqual(mode, "local_grounded_recovery")
        self.assertEqual(len(episodes), 2)
        self.assertEqual(len(errors), 2)

    async def test_compensation_shard_prefers_fast_structured_provider(self):
        plugin = self.make_plugin()
        plugin.episode_extraction_provider_id = "slow-pro"
        plugin.time_insight_llm_provider_id = "fast-structured"
        calls = []

        async def complete(_prompt, *, provider_id, timeout, label):
            calls.append((provider_id, timeout, label))
            return json.dumps([{
                "episode_key": "e1",
                "event_date": "2026-09-27",
                "time_basis": "conversation_now",
                "scene_anchor": "院子里的一次完整问答",
                "scene_start_turn": 0,
                "scene_end_turn": 1,
                "memory_type": "daily_life",
                "evidence": [{
                    "actor": "assistant",
                    "detail": "我认真回应了他",
                    "turn_indexes": [1],
                    "confidence": 0.95,
                }],
                "importance": 3,
            }], ensure_ascii=False)

        plugin._call_memory_generation_llm = complete
        messages = [
            {"role": "user", "content": "你还记得吗？"},
            {"role": "assistant", "content": "我记得，也会认真回应。"},
        ]
        episodes, mode, errors, _timing = (
            await plugin._extract_episode_blueprints_resilient(
                messages, "[turn:0] 你还记得吗？\n[turn:1] 我记得。",
                1, 2, [], exact_count=False,
                source_kind="compensation_shard",
            )
        )
        self.assertEqual(mode, "llm_primary")
        self.assertTrue(episodes)
        self.assertFalse(errors)
        self.assertEqual(calls, [(
            "fast-structured", 180.0, "episode_extract",
        )])

    async def test_transport_routes_exhausted_skips_same_route_compact_retry(self):
        plugin = self.make_plugin()
        plugin._resolve_chat_provider = lambda _id: None
        calls = []

        async def fail(_prompt, *, provider_id, timeout, label):
            calls.append(label)
            raise LLMRouteExhaustedError(
                "transport exhausted", failure_kind="transport", record_id="llm_test",
            )

        plugin._call_memory_generation_llm = fail
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": f"第{index}轮仍在同一个院子里谈那封信。",
             "event_ts": 1789308000.0 + index * 30}
            for index in range(20)
        ]
        episodes, mode, errors, timing = await plugin._extract_episode_blueprints_resilient(
            messages,
            "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
            1, 3, [], exact_count=False, source_kind="eod",
        )
        self.assertEqual(calls, ["episode_extract"])
        self.assertEqual(mode, "local_grounded_recovery")
        self.assertTrue(episodes)
        self.assertIn("compact:skipped_after_transport_routes_exhausted", errors)
        self.assertEqual(timing["stage_ms"]["compact_recovery"], 0)

    def test_local_recovery_avoids_tiny_first_diary(self):
        from astrbot_plugin_memos_memory.episode_recovery import _scene_ranges

        messages = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": "记住这一段真实的夜间对话。" * (2 if index % 2 == 0 else 12)}
            for index in range(30)
        ]
        starts = [0, 3, 5, 7, 19, 25]
        candidates = [
            {"start_turn": start,
             "end_turn": (starts[pos + 1] - 1 if pos + 1 < len(starts) else 29),
             "reasons": ["topic_shift"]}
            for pos, start in enumerate(starts)
        ]
        ranges = _scene_ranges(messages, candidates, 2)
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], 29)
        self.assertEqual(ranges[0][1] + 1, ranges[1][0])
        self.assertEqual(messages[ranges[1][0]]["role"], "user")
        total = sum(len(item["content"]) for item in messages)
        self.assertTrue(all(
            sum(len(messages[index]["content"]) for index in range(start, end + 1))
            >= total * 0.25
            for start, end, _reasons in ranges
        ))

    async def test_recent_heavy_provider_timeout_skips_repeat_full_extraction(self):
        plugin = self.make_plugin()
        prompt = "相同来源的证据抽取" * 800
        fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]

        class Provider:
            provider_id = "same-provider"

        plugin._resolve_chat_provider = lambda _id: Provider()
        plugin._llm_call_events = [{
            "task": "episode_extract", "provider": "same-provider",
            "prompt_chars": len(prompt), "prompt_fingerprint": fingerprint,
            "outcome": "timeout", "ts": time.time(),
        }]
        messages = [{
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"第{index}轮我们在院子里说到明日的约定和彼此的心情。",
            "event_ts": 1789308000.0 + index * 30,
        } for index in range(26)]

        async def forbidden(*_args, **_kwargs):
            self.fail("same heavy model request should have cooled down")

        plugin._call_memory_generation_llm = forbidden
        with patch("astrbot_plugin_memos_memory.main.build_episode_extraction_prompt", return_value=prompt):
            episodes, mode, errors, timing = await plugin._extract_episode_blueprints_resilient(
                messages, "[turn:0] " + "院子里的对话" * 800,
                1, 3, [], exact_count=False,
            )
        self.assertEqual(mode, "local_grounded_recovery")
        self.assertTrue(episodes)
        self.assertIn("primary:recent_heavy_failure_cooldown", errors)
        self.assertEqual(timing["stage_ms"]["primary"], 0)

    async def test_recent_heavy_failure_never_skips_a_different_or_stale_prompt(self):
        prompt = "另一次对话的证据抽取" * 800
        fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]
        messages = [{
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"第{index}轮我们在院子里说话。",
            "event_ts": 1789308000.0 + index * 30,
        } for index in range(26)]

        class Provider:
            provider_id = "same-provider"

        for sample in (
            {"task": "episode_extract", "provider": "same-provider",
             "prompt_fingerprint": "other-prompt", "outcome": "timeout", "ts": time.time()},
            {"task": "episode_extract", "provider": "same-provider",
             "prompt_fingerprint": fingerprint, "outcome": "timeout", "ts": time.time() - 1900},
            {"task": "episode_extract", "provider": "different-provider",
             "prompt_fingerprint": fingerprint, "outcome": "timeout", "ts": time.time()},
            {"task": "episode_extract", "provider": "same-provider",
             "prompt_fingerprint": fingerprint, "outcome": "timeout", "ts": "invalid"},
        ):
            with self.subTest(sample=sample):
                plugin = self.make_plugin()
                plugin._resolve_chat_provider = lambda _id: Provider()
                plugin._llm_call_events = [None, sample]
                calls = []

                async def unavailable(*_args, **kwargs):
                    calls.append(kwargs.get("label"))
                    raise RuntimeError("simulated model failure")

                plugin._call_memory_generation_llm = unavailable
                with patch("astrbot_plugin_memos_memory.main.build_episode_extraction_prompt", return_value=prompt):
                    await plugin._extract_episode_blueprints_resilient(
                        messages, "[turn:0] " + "院子里的对话" * 800,
                        1, 3, [], exact_count=False,
                    )
                self.assertIn("episode_extract", calls)

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

    async def test_extraction_timeout_uses_compact_grounded_recovery(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "我害怕你不告而别。"},
            {"role": "assistant", "content": "我答应离开前一定告诉你。"},
        ]
        extraction = [{
            "episode_key": "e1", "event_date": "2026-08-11",
            "scene_anchor": "离开前说明的约定", "memory_type": "promise_or_rule",
            "evidence": [{
                "kind": "commitment", "actor": "assistant",
                "detail": "我答应离开前一定告诉他", "quote": "我答应离开前一定告诉你。",
                "turn_indexes": [1], "tier": "must_write", "confidence": 0.99,
            }],
            "retrieval_key": "离开前说明 约定", "importance": 5,
        }]
        rendered = [{
            "episode_key": "e1",
            "content": "我答应过他，离开前一定告诉他；这份约定会被我认真记住。",
        }]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append((label, timeout))
            if label == "episode_extract":
                raise asyncio.TimeoutError()
            if label == "episode_extract_compact_retry":
                return json.dumps(extraction, ensure_ascii=False)
            return json.dumps(rendered, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
            1,
        )
        self.assertEqual([item[0] for item in calls], [
            "episode_extract", "episode_extract_compact_retry", "diary_render",
        ])
        self.assertEqual(calls[1][1], 30.0)
        self.assertEqual(diaries[0]["_episode_extraction_mode"], "llm_compact_recovery")
        self.assertEqual(diaries[0]["_evidence_quality"], "source_grounded")

    async def test_double_extraction_failure_keeps_local_source_links(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "离开前一定要告诉我。", "event_ts": 1786462200},
            {"role": "assistant", "content": "我答应离开前一定告诉你。", "event_ts": 1786462260},
        ]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append(label)
            if label.startswith("episode_extract"):
                raise asyncio.TimeoutError()
            return json.dumps([
                {
                    "episode_key": "recovery_e1",
                    "content": "他要我离开前一定告诉他，我把这句话认真记住了。",
                },
                {
                    "episode_key": "recovery_e2",
                    "content": "我答应离开前一定告诉他，这份约定我会守住。",
                },
            ], ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
            1,
        )
        self.assertEqual(calls, [
            "episode_extract", "episode_extract_compact_retry", "diary_render",
        ])
        self.assertTrue(all(
            item["_episode_extraction_mode"] == "local_grounded_recovery" for item in diaries
        ))
        self.assertTrue(all(item["event_date"] == "2026-08-11" for item in diaries))
        self.assertEqual(
            {
                index for diary in diaries for item in diary["evidence"]
                for index in item["turn_indexes"]
            },
            {0, 1},
        )
        self.assertTrue(all(
            item["grounded"] for diary in diaries for item in diary["evidence"]
        ))

    async def test_eod_double_failure_uses_target_not_soft_cap(self):
        plugin = self.make_plugin()
        start = 1789308000.0
        messages = []
        for index in range(13):
            messages.extend([
                {
                    "role": "user",
                    "content": f"同一场景里的连续问题{index}",
                    "event_ts": start + index * 150,
                    "event_timezone": "Asia/Shanghai",
                },
                {
                    "role": "assistant",
                    "content": f"我仍在同一处回应这件事{index}",
                    "event_ts": start + index * 150 + 30,
                    "event_timezone": "Asia/Shanghai",
                },
            ])

        async def call(_prompt, *, provider_id, timeout, label):
            if label.startswith("episode_extract"):
                raise asyncio.TimeoutError()
            return json.dumps([{
                "episode_key": "recovery_e1",
                "content": "我把这段连续的谈话完整记了下来。",
            }], ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
            1,
            diary_cap=3,
            source_kind="eod",
        )
        self.assertEqual(len(diaries), 1)
        self.assertEqual(diaries[0]["scene_start_turn"], 0)
        self.assertEqual(diaries[0]["scene_end_turn"], 25)
        self.assertEqual(diaries[0]["_episode_extraction_mode"], "local_grounded_recovery")
        cited = {index for item in diaries[0]["evidence"] for index in item["turn_indexes"]}
        self.assertLessEqual(len(cited), 10)
        self.assertEqual(len({min(2, index * 3 // 26) for index in cited}), 3)

    async def test_long_local_recovery_rejects_failed_literary_render(self):
        plugin = self.make_plugin()
        plugin.episode_extraction_timeout = 0.01
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": f"连续情景中的第{index}条对话和动作",
             "event_ts": 1789308000.0 + index * 30}
            for index in range(46)
        ]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append((label, timeout, _prompt))
            if label == "episode_extract":
                await asyncio.sleep(0.03)
            raise asyncio.TimeoutError()

        plugin._call_memory_generation_llm = call
        with self.assertRaises(DiaryGenerationDeferred) as caught:
            await plugin._generate_evidence_first_diaries(
                messages,
                "\n".join(f"[turn:{index}] {item['content']}"
                          for index, item in enumerate(messages)),
                1, diary_cap=4, source_kind="eod",
            )
        self.assertEqual(caught.exception.diagnostics["stage"], "diary_render")
        self.assertEqual([item[0] for item in calls], [
            "episode_extract", "episode_extract_compact_retry", "diary_render_grounded",
            "diary_render_timeout_retry",
        ])
        self.assertEqual(calls[1][1], 60.0)
        self.assertIn("最多 3 个", calls[1][2])
        self.assertLess(calls[2][2].count("连续情景中的第"), len(messages))

    async def test_long_recovery_keeps_soft_scene_whole_and_projects_evidence(self):
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": f"我在院里聊起第{index}件日常小事，也记得那阵风。",
             "event_ts": 1789308000.0 + index * 30,
             "event_timezone": "Asia/Shanghai"}
            for index in range(46)
        ]
        messages[17]["content"] = "我答应明天把那封信带过来。"
        splitter = SceneSplitter(max_scenes=6)
        episodes = build_local_grounded_episodes(
            messages, splitter.detect(messages), 1, "Asia/Shanghai",
        )
        self.assertEqual(len(episodes), 1)
        self.assertEqual((episodes[0]["scene_start_turn"], episodes[0]["scene_end_turn"]), (0, 45))
        cited = {index for item in episodes[0]["evidence"] for index in item["turn_indexes"]}
        self.assertIn(17, cited)
        self.assertLessEqual(len(cited), 20)
        self.assertEqual(len({min(2, index * 3 // 46) for index in cited}), 3)

    async def test_medium_render_projection_preserves_persisted_evidence(self):
        messages = [{
            "role": "assistant",
            "content": ("我答应明日再见。" if index < 7 else "我记得院里的风声。")
                       + f"第{index}轮 " + "细节" * 90,
            "event_ts": 1789308000.0 + index * 30,
        } for index in range(26)]
        episodes = build_local_grounded_episodes(messages, [], 1)
        original = episodes[0]["evidence"]
        projected = project_grounded_evidence_for_render(original)
        self.assertEqual(len(original), 11)
        self.assertLessEqual(len(projected), 12)
        self.assertEqual(
            {item["turn_indexes"][0] for item in projected if item["tier"] == "must_write"},
            set(range(7)),
        )
        self.assertGreaterEqual(
            sum(item["tier"] == "supporting" for item in projected), 4,
        )
        self.assertTrue(all(len(item["detail"]) <= 162 for item in projected))
        self.assertEqual(len(episodes[0]["evidence"]), 11)

    async def test_long_recovery_never_merges_across_recorded_date(self):
        start = datetime(2026, 9, 13, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        messages = [
            {"role": "assistant", "content": f"我记得第{index}件事。",
             "event_ts": start + index * 90 + (86400 if index >= 23 else 0),
             "event_timezone": "Asia/Shanghai"}
            for index in range(46)
        ]
        episodes = build_local_grounded_episodes(
            messages, SceneSplitter(max_scenes=6).detect(messages),
            1, "Asia/Shanghai",
        )
        self.assertEqual(len(episodes), 2)
        self.assertEqual(
            [(item["scene_start_turn"], item["scene_end_turn"]) for item in episodes],
            [(0, 22), (23, 45)],
        )
        self.assertNotEqual(episodes[0]["event_date"], episodes[1]["event_date"])

    async def test_long_batch_does_not_publish_after_hard_boundary_clamps_range(self):
        plugin = self.make_plugin()
        start = datetime(2026, 9, 13, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        messages = [
            {"role": "assistant", "content": f"我记得那一天第{index}件日常事情。",
             "event_ts": start + index * 90 + (86400 if index >= 20 else 0),
             "event_timezone": "Asia/Shanghai"}
            for index in range(40)
        ]
        prebuilt = [{
            "episode_key": "recovery_e1", "scene_start_turn": 0, "scene_end_turn": 39,
            "event_date": "2026-09-13", "time_basis": "conversation_now",
            "evidence": [{"detail": messages[0]["content"], "quote": messages[0]["content"],
                          "turn_indexes": [0], "grounded": True, "tier": "supporting"}],
        }]
        with self.assertRaises(DiaryGenerationDeferred) as caught:
            await plugin._generate_evidence_first_diaries(
                messages, "原文只用于校验", 1, diary_cap=4,
                source_kind="eod", prebuilt_episodes=prebuilt,
            )
        self.assertEqual(caught.exception.diagnostics["stage"], "episode_coverage")
        self.assertIn(20, caught.exception.diagnostics["uncovered_turns"])

    async def test_long_recovery_rejects_dense_paraphrase(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": "我坐在院里，聊起那年写给彼此的信，也说了各自的心情。" * 5,
             "event_ts": 1789308000.0 + index * 30}
            for index in range(46)
        ]

        async def call(_prompt, *, provider_id, timeout, label):
            if label.startswith("episode_extract"):
                raise asyncio.TimeoutError()
            return json.dumps([{
                "episode_key": "recovery_e1",
                "content": "我想起院里那封信，心里仍有一些舍不得。" * 120,
            }], ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        with self.assertRaises(DiaryGenerationDeferred) as caught:
            await plugin._generate_evidence_first_diaries(
                messages,
                "\n".join(f"[turn:{index}] {item['content']}"
                          for index, item in enumerate(messages)),
                1, diary_cap=4, source_kind="eod",
            )
        self.assertEqual(caught.exception.diagnostics["stage"], "diary_render")
        self.assertIn(
            "single_diary_too_long",
            caught.exception.diagnostics["reasons"]["recovery_e1"],
        )

    async def test_long_recovery_retries_timed_out_render_once(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": "我在院里等他，听见风吹过树叶，也想起那封信。",
             "event_ts": 1789308000.0 + index * 30}
            for index in range(46)
        ]
        labels = []

        async def call(_prompt, *, provider_id, timeout, label):
            labels.append(label)
            if label.startswith("episode_extract") or label == "diary_render_grounded":
                raise asyncio.TimeoutError()
            return json.dumps([{
                "episode_key": "recovery_e1",
                "content": "我在院里等他，风吹过树叶时又想起那封信。"
                           "那些细碎的等待没有改变这一天的走向，却留在了我心里。",
            }], ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "\n".join(f"[turn:{index}] {item['content']}"
                      for index, item in enumerate(messages)),
            1, diary_cap=4, source_kind="eod",
        )
        self.assertEqual(len(diaries), 1)
        self.assertEqual(labels.count("diary_render_timeout_retry"), 1)
        self.assertEqual(diaries[0]["scene_end_turn"], 45)

    async def test_short_batch_also_retries_transient_render_timeout(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "明天还去院子里看花吗？", "event_ts": 1789308000.0},
            {"role": "assistant", "content": "我答应明天陪你去看花。", "event_ts": 1789308030.0},
        ]
        labels = []

        async def call(_prompt, *, provider_id, timeout, label):
            labels.append(label)
            if label == "diary_render":
                raise asyncio.TimeoutError()
            return json.dumps([{
                "episode_key": "recovery_e1",
                "content": "我答应明天陪他去院子里看花，也记住了这份约定。",
            }], ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        prebuilt = build_local_grounded_episodes(messages, [], 1)
        diaries = await plugin._generate_evidence_first_diaries(
            messages, "原文只用于校验", 1, prebuilt_episodes=prebuilt,
        )
        self.assertEqual(len(diaries), 1)
        self.assertEqual(labels, ["diary_render", "diary_render_timeout_retry"])

    async def test_reasoning_only_render_uses_quality_retry_not_short_timeout_retry(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "明天还去院子里看花吗？", "event_ts": 1789308000.0},
            {"role": "assistant", "content": "我答应明天陪你去看花。", "event_ts": 1789308030.0},
        ]
        labels = []

        async def call(_prompt, *, provider_id, timeout, label):
            labels.append(label)
            if label == "diary_render":
                raise LLMEmptyFinalError("provider returned no final text")
            return json.dumps([{
                "episode_key": "recovery_e1",
                "content": "我答应明天陪他去院子里看花，也记住了这份约定。",
            }], ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        prebuilt = build_local_grounded_episodes(messages, [], 1)
        diaries = await plugin._generate_evidence_first_diaries(
            messages, "原文只用于校验", 1, prebuilt_episodes=prebuilt,
        )
        self.assertEqual(len(diaries), 1)
        self.assertEqual(labels, ["diary_render", "diary_render_retry"])

    async def test_local_recovery_ignores_render_keys_beyond_planned_target(self):
        plugin = self.make_plugin()
        plugin.episode_extraction_timeout = 120.0
        messages = [
            {"role": "user", "content": "离开前一定要告诉我。", "event_ts": 1786462200},
            {"role": "assistant", "content": "我答应离开前一定告诉你。", "event_ts": 1786462260},
        ]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append((label, timeout))
            if label.startswith("episode_extract"):
                raise asyncio.TimeoutError()
            return json.dumps([
                {
                    # Some models follow the generic eN example even when the
                    # source-grounded recovery episode is named recovery_eN.
                    "episode_key": "e1",
                    "content": "他要我离开前一定告诉他，我认真记住了这句话。",
                },
                {
                    "episode_key": "e2",
                    "content": "我答应离开前一定告诉他，也会认真守住这份约定。",
                },
            ], ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        diaries = await plugin._generate_evidence_first_diaries(
            messages,
            "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
            1,
        )

        self.assertEqual([item[0] for item in calls], [
            "episode_extract", "episode_extract_compact_retry", "diary_render",
        ])
        self.assertEqual(calls[1][1], 60.0)
        self.assertEqual(len(diaries), 1)
        self.assertEqual(
            [item["episode_key"] for item in diaries],
            ["recovery_e1"],
        )
        self.assertTrue(all(not item["_render_fallback"] for item in diaries))
        self.assertTrue(all(
            "missing_episode_key" not in item["_render_retry_reason"] for item in diaries
        ))

    async def test_render_timeout_stays_inside_evidence_first_pipeline(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "离开前告诉我。"},
            {"role": "assistant", "content": "我答应离开前一定告诉你。"},
        ]
        extraction = [{
            "episode_key": "e1", "scene_anchor": "离开前说明的约定",
            "memory_type": "promise_or_rule",
            "evidence": [{
                "kind": "commitment", "actor": "assistant",
                "detail": "我答应离开前一定告诉他", "quote": "我答应离开前一定告诉你。",
                "turn_indexes": [1], "tier": "must_write", "confidence": 1.0,
            }],
        }]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append(label)
            if label == "episode_extract":
                return json.dumps(extraction, ensure_ascii=False)
            raise asyncio.TimeoutError()

        plugin._call_memory_generation_llm = call
        with self.assertRaises(DiaryGenerationDeferred) as deferred:
            await plugin._generate_evidence_first_diaries(
                messages,
                "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
                1,
            )
        self.assertEqual(calls, [
            "episode_extract", "diary_render", "diary_render_timeout_retry",
        ])
        self.assertIn("render_call_failed:TimeoutError", str(deferred.exception.diagnostics["reasons"]))

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
        with self.assertRaises(DiaryGenerationDeferred) as deferred:
            await plugin._generate_evidence_first_diaries(
                messages,
                "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
                1,
            )
        self.assertIn("missing_key_fallback", str(deferred.exception.diagnostics["reasons"]))

    async def test_persistently_invalid_render_uses_grounded_fallback(self):
        plugin = self.make_plugin()
        messages = [
            {"role": "user", "content": "把银色钥匙放进蓝色盒子。"},
            {"role": "assistant", "content": "好，我会记得钥匙在蓝色盒子里。"},
        ]
        episodes = [{
            "episode_key": "e1", "event_date": "2026-08-04",
            "scene_anchor": "收好银色钥匙", "memory_type": "plot_fact",
            "evidence": [{
                "actor": "assistant", "detail": "银色钥匙放在蓝色盒子里",
                "quote": "钥匙在蓝色盒子里", "turn_indexes": [1],
            }],
            "state_change": "关系变得更稳定",
        }]
        invalid = [{"episode_key": "e1", "content": "用户要求保存一件物品。"}]
        calls = []

        async def call(_prompt, *, provider_id, timeout, label):
            calls.append(label)
            if label == "episode_extract":
                return json.dumps(episodes, ensure_ascii=False)
            return json.dumps(invalid, ensure_ascii=False)

        plugin._call_memory_generation_llm = call
        with self.assertRaises(DiaryGenerationDeferred) as deferred:
            await plugin._generate_evidence_first_diaries(
                messages,
                "\n".join(f"[turn:{i}] {item['content']}" for i, item in enumerate(messages)),
                1,
            )
        self.assertEqual(calls, ["episode_extract", "diary_render", "diary_render_retry"])
        self.assertIn("grounded_fallback_after:", str(deferred.exception.diagnostics["reasons"]))

    async def test_two_stage_failure_keeps_raw_batch_without_legacy_publication(self):
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
                self.assertEqual(result, 0)
                self.assertEqual(calls, [])
                self.assertEqual(len(persisted), 0)
                batches = store.batch_status(5)
                self.assertEqual(batches[0]["status"], "generation_failed")
                self.assertEqual(batches[0]["attempts"], 1)
                self.assertEqual(batches[0]["message_count"], 8)
                self.assertEqual(len(store.source_turns(batches[0]["batch_id"])), 8)
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
