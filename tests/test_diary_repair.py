"""Regression tests for the rc5 no-publish gate and in-place fallback repair."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_memos_memory.diary_pipeline import DiaryGenerationDeferred
from astrbot_plugin_memos_memory.compress import build_diary_render_prompt
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin


class MemoryMemos:
    def __init__(self, content: str):
        self.rows = {"memos/old": {"name": "memos/old", "content": content}}
        self.updates: list[tuple[str, str]] = []

    async def get_memo(self, name: str):
        row = self.rows.get(name)
        return dict(row) if row else None

    async def update_memo(self, name: str, content: str):
        if name not in self.rows:
            raise RuntimeError("missing memo")
        self.updates.append((name, content))
        self.rows[name]["content"] = content
        return dict(self.rows[name])


class DiaryRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.temp.name) / "ep.db"), 3, "test")
        await self.store.init()
        self.batch = self.store.archive_batch(
            "session-old",
            [
                {"role": "user", "content": "把糖葫芦递给你。", "event_ts": 1789369200.0},
                {"role": "assistant", "content": "我接过糖葫芦，想起自己的承诺。", "event_ts": 1789369201.0},
            ],
            "eod",
        )
        self.old_content = "2026年9月14日 · 下午\n我记得，把糖葫芦递给你。把糖葫芦递给你。"
        self.memos = MemoryMemos(self.old_content)
        self.store.upsert_episode(
            memo_name="memos/old",
            episode={
                "episode_id": "ep-old", "occurred_at": "2026-09-14",
                "event_ts": 1789369200.0, "time_basis": "conversation_now",
                "scene_start_turn": 0, "scene_end_turn": 1,
                "scene_anchor": "旧场景", "retrieval_key": "糖葫芦",
                "evidence": [{
                    "kind": "event", "detail": "递了糖葫芦", "quote": "把糖葫芦递给你",
                    "turn_indexes": [0], "tier": "must_write", "grounded": True,
                }],
            },
            card_text="旧卡片",
            embedding=[0.2, 0.3, 0.4],
            source_batch_id=self.batch,
            source_kind="eod",
            legacy=False,
            evidence_quality="source_grounded",
            diary_content_hash="old-hash",
            render_fallback=True,
            render_retry_reason="render_call_failed:TimeoutError",
        )
        self.plugin = object.__new__(MemosMemoryPlugin)
        self.plugin._episodes = self.store
        self.plugin._memos = self.memos
        self.plugin._vec = None
        self.plugin.rp_time_timezone = "Asia/Shanghai"
        self.plugin.raw_archive_prompt_view_max_chars = 8000
        self.plugin.memory_forgetting_enable = False
        self.plugin.thread_memory_enable = False

        async def render(*_args, **_kwargs):
            return [{
                "episode_key": "repaired-e1",
                "content": "午后的糖壳在唇边轻轻裂开。我把糖葫芦递给他，"
                           "也把那句会好好照顾彼此的承诺留在心里。",
                "event_date": "2026-09-14", "time_label": "下午",
                "time_basis": "conversation_now", "importance": 4,
                "memory_type": "relationship", "scene_anchor": "午后递糖葫芦",
                "retrieval_key": "糖葫芦 承诺", "scene_start_turn": 0,
                "scene_end_turn": 1,
                "evidence": [{
                    "kind": "event", "detail": "我把糖葫芦递给他",
                    "quote": "把糖葫芦递给你", "turn_indexes": [0],
                    "tier": "must_write", "grounded": True,
                }],
                "_must_coverage": 1.0, "_support_coverage": 1.0,
                "_transcript_risk": 0.1, "_evidence_quality": "source_grounded",
                "_render_fallback": False,
            }]

        async def embed(_text):
            return [0.4, 0.3, 0.2]

        self.plugin._generate_evidence_first_diaries = render
        self.plugin._embed = embed

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_preview_and_confirm_replace_same_memo_and_preserve_date(self):
        before = self.store.episode_snapshot("memos/old")
        observed = {}
        render = self.plugin._generate_evidence_first_diaries

        async def capture(*args, **kwargs):
            observed.update(kwargs)
            return await render(*args, **kwargs)

        self.plugin._generate_evidence_first_diaries = capture
        preview = await self.plugin._create_fallback_repair_preview("ep-old")
        self.assertEqual(self.memos.updates, [])
        self.assertEqual(len(observed["prebuilt_episodes"]), 1)
        self.assertEqual(len(observed["prebuilt_episodes"][0]["evidence"]), 2)
        self.assertEqual(preview["mode"], "inplace_fallback_repair")
        self.assertEqual(preview["source_turns"], 2)
        result = await self.plugin._confirm_fallback_repair(preview["preview_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["memo_name"], "memos/old")
        self.assertEqual(len(self.memos.rows), 1)
        self.assertTrue(self.memos.rows["memos/old"]["content"].startswith("2026年9月14日 · 下午\n"))
        episode = self.store.get_episode("memos/old")
        self.assertEqual(episode["episode_id"], "ep-old")
        self.assertEqual(episode["render_fallback"], 0)
        self.assertEqual(episode["source_batch_id"], self.batch)
        self.assertEqual(self.store.batch_status(1)[0]["status"], "archived")
        self.assertEqual(len(self.store.source_turns(self.batch)), 2)
        second = await self.plugin._confirm_fallback_repair(preview["preview_id"])
        self.assertTrue(second["skipped"])
        self.assertEqual(len(self.memos.updates), 1)

        rolled = await self.plugin._rollback_diary_rewrite(preview_id=preview["preview_id"])
        self.assertEqual(rolled["restored"], 1)
        self.assertEqual(self.memos.rows["memos/old"]["content"], self.old_content)
        self.assertEqual(self.store.episode_snapshot("memos/old"), before)

    async def test_large_inplace_repair_requires_split_rewrite_preview(self):
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant",
             "content": f"第{index}条原始对话", "event_ts": 1789369200.0 + index * 30}
            for index in range(46)
        ]
        batch = self.store.archive_batch("session-large", messages, "eod")
        self.store.upsert_episode(
            memo_name="memos/large",
            episode={
                "episode_id": "ep-large", "occurred_at": "2026-09-14",
                "scene_start_turn": 0, "scene_end_turn": 45,
                "scene_anchor": "旧长场景", "retrieval_key": "长场景",
                "evidence": [{"kind": "fact", "detail": "第0条原始对话",
                              "turn_indexes": [0], "tier": "must_write", "grounded": True}],
            },
            card_text="旧长日记", embedding=[0.2, 0.3, 0.4],
            source_batch_id=batch, source_kind="eod", legacy=False,
            evidence_quality="source_grounded", diary_content_hash="old-hash",
            render_fallback=True,
        )
        with self.assertRaises(DiaryGenerationDeferred) as caught:
            await self.plugin._create_fallback_repair_preview("ep-large")
        self.assertEqual(caught.exception.diagnostics["source_messages"], 46)
        self.assertEqual(self.memos.updates, [])

    async def test_local_recovery_prompt_keeps_evidence_without_duplicate_transcript(self):
        details = [f"事件 {i}: 她记得当天的具体动作和回应" for i in range(46)]
        episode = {
            "episode_key": "recovery_e1",
            "_episode_extraction_mode": "local_grounded_recovery",
            "evidence": [{"detail": detail, "quote": detail} for detail in details],
        }
        prompt = build_diary_render_prompt("角色", "DUPLICATE-TRANSCRIPT", [episode])
        payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("[{"):])
        self.assertEqual([item["detail"] for item in payload[0]["evidence"]], details)
        self.assertNotIn("DUPLICATE-TRANSCRIPT", prompt)
        self.assertTrue(all("quote" not in item for item in payload[0]["evidence"]))

    async def test_medium_recovery_prompt_uses_literary_compact_view(self):
        episode = {
            "episode_key": "recovery_e1",
            "scene_start_turn": 0, "scene_end_turn": 25,
            "_episode_extraction_mode": "local_grounded_recovery",
            "evidence": [
                {"detail": "约定明日再见", "tier": "must_write"},
                {"detail": "院中的茶已经凉了", "tier": "supporting"},
            ],
        }
        prompt = build_diary_render_prompt("角色", "DUPLICATED-FULL-TRANSCRIPT", [episode])
        self.assertNotIn("DUPLICATED-FULL-TRANSCRIPT", prompt)
        self.assertIn("第一人称私密日记", prompt)
        self.assertIn("约定明日再见", prompt)

    async def test_normal_render_prompt_keeps_grounding_and_time_without_index_metadata(self):
        episode = {
            "episode_key": "e1", "event_date": "2026-09-14",
            "time_label": "下午", "time_basis": "conversation_now",
            "scene_start_turn": 2, "scene_end_turn": 5,
            "retrieval_key": "a duplicate search sentence",
            "importance": 5,
            "evidence": [{
                "detail": "答应明天见面", "quote": "明天见", "tier": "must_write",
                "turn_indexes": [4], "confidence": 0.9,
            }],
        }
        prompt = build_diary_render_prompt("角色", "[turn:4] 明天见", [episode])
        payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("[{"):])
        self.assertEqual(payload[0]["event_date"], "2026-09-14")
        self.assertEqual(payload[0]["evidence"][0]["quote"], "明天见")
        self.assertEqual(payload[0]["evidence"][0]["tier"], "must_write")
        self.assertNotIn("retrieval_key", payload[0])
        self.assertNotIn("confidence", payload[0]["evidence"][0])
        self.assertIn("[turn:4] 明天见", prompt)

    async def test_preview_detects_external_memo_edit(self):
        preview = await self.plugin._create_fallback_repair_preview("ep-old")
        self.memos.rows["memos/old"]["content"] += "\n外部编辑"
        with self.assertRaisesRegex(RuntimeError, "重新生成"):
            await self.plugin._confirm_fallback_repair(preview["preview_id"])
        self.assertEqual(self.memos.updates, [])

    async def test_failed_generation_never_writes_a_preview_or_memo(self):
        async def timeout(*_args, **_kwargs):
            raise DiaryGenerationDeferred("render timeout")

        self.plugin._generate_evidence_first_diaries = timeout
        with self.assertRaises(DiaryGenerationDeferred):
            await self.plugin._create_fallback_repair_preview("ep-old")
        self.assertEqual(self.memos.updates, [])
        self.assertEqual(self.store.list_diary_previews(), [])

    async def test_failed_post_write_index_restores_old_memo_and_episode(self):
        preview = await self.plugin._create_fallback_repair_preview("ep-old")
        before = self.store.episode_snapshot("memos/old")
        marker = self.old_content

        class Vec:
            pass

        self.plugin._vec = Vec()

        async def index(memo, *, replace=False):
            return str(memo.get("content") or "") == marker

        self.plugin._index_memo_record = index
        with self.assertRaisesRegex(RuntimeError, "段落索引更新失败"):
            await self.plugin._confirm_fallback_repair(preview["preview_id"])
        self.assertEqual(self.memos.rows["memos/old"]["content"], marker)
        self.assertEqual(self.store.episode_snapshot("memos/old"), before)


if __name__ == "__main__":
    unittest.main()
