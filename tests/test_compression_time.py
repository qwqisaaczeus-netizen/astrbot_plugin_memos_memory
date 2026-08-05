from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.provider import ProviderRequest

from astrbot_plugin_memos_memory.compress import (
    format_messages,
    recorded_message_dates,
    recorded_time_context,
)
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
from astrbot_plugin_memos_memory.vector_store import VectorStore


class FakeContext:
    def get_provider_by_id(self, provider_id):
        return None

    def get_using_provider(self, umo=None):
        return None

    def get_all_providers(self):
        return []


class FakeEvent:
    def __init__(self, text="你好", umo="default:friend"):
        self.message_str = text
        self.unified_msg_origin = umo
        self.extra = {}

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)


class CaptureBuffer:
    def __init__(self):
        self.rows = []

    async def buffer_append(self, session_id, rows):
        self.rows.extend(dict(row) for row in rows)

    async def buffer_snapshot(self, session_id, max_messages=0):
        rows = self.rows[:max_messages] if max_messages else list(self.rows)
        return rows, len(rows) - 1


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def diary(date: str, content: str, *, basis: str = "conversation_now") -> dict:
    return {
        "event_date": date,
        "time_label": "",
        "time_basis": basis,
        "scene_anchor": content[:12],
        "content": content,
        "memory_type": "daily_texture",
        "long_effect": "我会记得这份日常。",
        "trigger_hint": "再次提到这件事时，我会想起来。",
        "retrieval_key": content,
        "state_change": "",
        "entities": [],
        "tags": ["#日常"],
        "importance": 3,
    }


class CompressionMessageTimeTests(unittest.TestCase):
    def test_format_messages_keeps_each_turn_time_across_midnight(self):
        messages = [
            {"role": "user", "content": "睡前说的话", "event_ts": ts("2026-07-18T15:55:00"), "event_timezone": "Asia/Shanghai"},
            {"role": "assistant", "content": "我听见了", "event_ts": ts("2026-07-18T15:55:00"), "event_timezone": "Asia/Shanghai"},
            {"role": "user", "content": "过了午夜继续聊", "event_ts": ts("2026-07-18T16:08:00"), "event_timezone": "Asia/Shanghai"},
            {"role": "assistant", "content": "已经是第二天了", "event_ts": ts("2026-07-18T16:08:00"), "event_timezone": "Asia/Shanghai"},
        ]
        text = format_messages(messages, timezone_name="Asia/Shanghai")
        self.assertIn("2026-07-18 23:55", text)
        self.assertIn("2026-07-19 00:08", text)
        self.assertEqual(text.count("[对话记录时间:"), 2)
        self.assertEqual(recorded_message_dates(messages, "Asia/Shanghai"), ["2026-07-18", "2026-07-19"])
        summary = recorded_time_context(messages, "Asia/Shanghai")
        self.assertIn("2026-07-18, 2026-07-19", summary)
        self.assertIn("不是压缩执行时间", summary)


class BufferTimeMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_pending_rows_fall_back_to_original_created_time(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """CREATE TABLE pending_messages (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                   seq INTEGER NOT NULL, created_ts REAL NOT NULL)"""
            )
            old_ts = ts("2026-07-18T03:20:00")
            conn.execute(
                "INSERT INTO pending_messages(session_id,role,content,seq,created_ts) VALUES (?,?,?,?,?)",
                ("old", "user", "旧缓冲", 0, old_ts),
            )
            conn.commit()
            conn.close()

            store = VectorStore(str(db), 3, "test")
            await store.init()
            rows, seq = await store.buffer_snapshot("old")
            self.assertEqual(seq, 0)
            self.assertEqual(rows[0]["event_ts"], old_ts)
            self.assertEqual(rows[0]["event_timezone"], "")
            store.close()

    async def test_new_rows_preserve_request_time_and_timezone(self):
        with tempfile.TemporaryDirectory() as temp:
            store = VectorStore(str(Path(temp) / "new.db"), 3, "test")
            await store.init()
            event_ts = ts("2026-07-18T01:10:00")
            await store.buffer_append("new", [
                {"role": "user", "content": "上午的话", "event_ts": event_ts, "event_timezone": "Asia/Shanghai"},
                {"role": "assistant", "content": "上午的回答", "event_ts": event_ts, "event_timezone": "Asia/Shanghai"},
            ])
            rows, _ = await store.buffer_snapshot("new")
            self.assertEqual([x["event_ts"] for x in rows], [event_ts, event_ts])
            self.assertEqual({x["event_timezone"] for x in rows}, {"Asia/Shanghai"})
            store.close()


class PassageVectorMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_passage_vector_replacement_is_resumable_and_searchable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = VectorStore(str(Path(temp) / "passages.db"), 3, "test")
            await store.init()
            await store.insert_chunks(
                "memos/one", ["只属于这一段的局部细节"], [[1.0, 0.0, 0.0]],
                passages=[{
                    "text": "只属于这一段的局部细节", "passage_index": 0,
                    "char_start": 0, "char_end": 11,
                }],
            )
            rows = store.passage_embedding_rows(0, 48)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["memo_name"], "memos/one")
            self.assertEqual(store.replace_chunk_embeddings([(rows[0]["id"], [0.0, 1.0, 0.0])]), 1)
            store.set_meta_value("passage_embedding_strategy", "local_passage_v2")
            self.assertEqual(store.get_meta_value("passage_embedding_strategy"), "local_passage_v2")
            hits = await store.search_topk(
                [0.0, 1.0, 0.0], top_k=1, min_similarity=0.0,
                bm25_query="", rerank_fn=None,
            )
            self.assertEqual(hits[0]["memo_name"], "memos/one")
            self.assertGreater(hits[0]["relevance"], 0.99)
            store.close()

    def test_passage_embedding_text_excludes_global_event_prefix(self):
        text = MemosMemoryPlugin._passage_embedding_text(
            "  只保留这一段   的内容  ",
            {"retrieval_key": "整篇检索词", "scene_anchor": "整件事"},
        )
        self.assertEqual(text, "只保留这一段 的内容")
        self.assertNotIn("整篇检索词", text)


class CompressionValidationTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, temp: str) -> MemosMemoryPlugin:
        plugin = MemosMemoryPlugin(FakeContext(), {
            "vec_db_path": str(Path(temp) / "memories.db"),
            "webui_enable": False,
            "enable_auto_compress": False,
            "rp_time_timezone": "Asia/Shanghai",
        })
        plugin.character_name = "爱莉"
        return plugin

    async def test_eod_accepts_one_complete_turn_below_normal_message_floor(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(temp)
            event_ts = ts("2026-08-02T14:30:00")
            messages = []
            for index in range(1):
                messages.extend([
                    {"role": "user", "content": f"夜间对话{index}", "event_ts": event_ts, "event_timezone": "Asia/Shanghai"},
                    {"role": "assistant", "content": f"夜间回答{index}", "event_ts": event_ts, "event_timezone": "Asia/Shanghai"},
                ])
            calls = []

            async def call(prompt):
                calls.append(prompt)
                return json.dumps([diary("2026-08-02", "今晚留下了值得保存的片段。")], ensure_ascii=False)

            async def store(_item, **_kwargs):
                return True

            plugin._call_llm_compress = call
            plugin._store_one_diary = store
            eod_result = await plugin._compress_and_store(
                "eod-three", messages, diary_count=1, source_kind="eod",
            )
            auto_result = await plugin._compress_and_store(
                "auto-three", messages, diary_count=1, source_kind="auto",
            )
            self.assertEqual(eod_result, 1)
            self.assertEqual(auto_result, 0)
            self.assertEqual(len(calls), 1)

    def test_eod_plan_uses_dates_gaps_and_density_as_capacity_not_fixed_buckets(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(temp)
            start = ts("2026-08-02T02:00:00")
            messages = []
            for index in range(17):
                event_ts = start + index * 120
                messages.extend([
                    {"role": "user", "content": f"连续对话{index}", "event_ts": event_ts, "event_timezone": "Asia/Shanghai"},
                    {"role": "assistant", "content": f"连续回答{index}", "event_ts": event_ts, "event_timezone": "Asia/Shanghai"},
                ])
            dense = plugin._plan_eod_checkpoint(messages)
            self.assertEqual(dense["turns"], 17)
            self.assertEqual(dense["scene_windows"], 1)
            self.assertEqual(dense["diary_capacity"], 3)

            messages[-1]["event_ts"] += 3 * 3600
            with_gap = plugin._plan_eod_checkpoint(messages)
            self.assertEqual(with_gap["scene_windows"], 2)
            self.assertEqual(with_gap["diary_capacity"], 3)

            messages[-1]["event_ts"] += 24 * 3600
            cross_day = plugin._plan_eod_checkpoint(messages)
            self.assertEqual(len(cross_day["dates"]), 2)
            self.assertGreaterEqual(cross_day["diary_capacity"], 2)

    async def test_eod_failure_uses_five_minute_retry_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(temp)
            messages = []
            for index in range(3):
                messages.extend([
                    {"role": "user", "content": f"对话{index}"},
                    {"role": "assistant", "content": f"回答{index}"},
                ])

            class Buffer:
                async def buffer_snapshot(self, _session, _limit):
                    return list(messages), 6

            attempts = []

            async def fail_compress(*_args, **_kwargs):
                attempts.append(1)
                return 0

            plugin._buffer = {"session": list(messages)}
            plugin._vec = Buffer()
            plugin._compress_with_lock = fail_compress
            plugin.enable_auto_compress = True
            first = await plugin._run_eod_flush_once(
                datetime(2026, 8, 2, 23, 45, tzinfo=timezone.utc)
            )
            too_soon = await plugin._run_eod_flush_once(
                datetime(2026, 8, 2, 23, 46, tzinfo=timezone.utc)
            )
            retry = await plugin._run_eod_flush_once(
                datetime(2026, 8, 2, 23, 50, tzinfo=timezone.utc)
            )
            third = await plugin._run_eod_flush_once(
                datetime(2026, 8, 2, 23, 55, tzinfo=timezone.utc)
            )
            capped = await plugin._run_eod_flush_once(
                datetime(2026, 8, 2, 23, 59, tzinfo=timezone.utc)
            )
            self.assertEqual(first["attempted"], 1)
            self.assertEqual(too_soon["attempted"], 0)
            self.assertEqual(too_soon["cooldown"], 1)
            self.assertEqual(retry["attempted"], 1)
            self.assertEqual(third["attempted"], 1)
            self.assertEqual(capped["attempted"], 0)
            self.assertEqual(capped["cooldown"], 1)
            self.assertEqual(len(attempts), 3)

    async def test_eod_new_snapshot_is_not_blocked_by_old_failure_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(temp)
            plugin.enable_auto_compress = True
            current = {
                "messages": [
                    {"role": "user", "content": "第一批"},
                    {"role": "assistant", "content": "第一批回答"},
                ],
                "seq": 2,
            }

            class Buffer:
                async def buffer_snapshot(self, _session, _limit):
                    return list(current["messages"]), current["seq"]

            attempts = []

            async def fail_compress(*_args, **_kwargs):
                attempts.append(current["seq"])
                return 0

            plugin._buffer = {"session": list(current["messages"])}
            plugin._vec = Buffer()
            plugin._compress_with_lock = fail_compress
            for minute in (45, 50, 55):
                await plugin._run_eod_flush_once(
                    datetime(2026, 8, 2, 23, minute, tzinfo=timezone.utc)
                )
            current["messages"].extend([
                {"role": "user", "content": "第二批新消息"},
                {"role": "assistant", "content": "第二批新回答"},
            ])
            current["seq"] = 4
            refreshed = await plugin._run_eod_flush_once(
                datetime(2026, 8, 2, 23, 59, tzinfo=timezone.utc)
            )
            self.assertEqual(refreshed["attempted"], 1)
            self.assertEqual(attempts, [2, 2, 2, 4])

    async def test_eod_success_marks_snapshot_and_schedules_time_insight_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(temp)
            plugin.enable_auto_compress = True
            current = {
                "messages": [
                    {"role": "user", "content": "记住今晚"},
                    {"role": "assistant", "content": "我会记住"},
                ],
                "seq": 2,
            }

            class Buffer:
                async def buffer_snapshot(self, _session, _limit):
                    return list(current["messages"]), current["seq"]

            scheduled = []
            plugin._time_insight = type("Insight", (), {
                "schedule_refresh": lambda _self, reason: scheduled.append(reason) or True,
            })()

            async def succeed(*_args, **_kwargs):
                current["messages"] = []
                return 1

            plugin._buffer = {"session": list(current["messages"])}
            plugin._vec = Buffer()
            plugin._compress_with_lock = succeed
            result = await plugin._run_eod_flush_once(
                datetime(2026, 8, 2, 23, 45, tzinfo=timezone.utc)
            )
            self.assertEqual(result["written"], 1)
            self.assertTrue(result["insight_refresh"])
            self.assertEqual(scheduled, ["eod_checkpoint"])
            self.assertEqual(plugin._eod_flush_done["session"]["snapshot_seq"], 2)

    async def test_single_day_conversation_now_is_corrected_deterministically(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(temp)
            event_ts = ts("2026-07-18T01:10:00")
            messages = []
            for i in range(4):
                messages.extend([
                    {"role": "user", "content": f"上午对话{i}", "event_ts": event_ts, "event_timezone": "Asia/Shanghai"},
                    {"role": "assistant", "content": f"上午回答{i}", "event_ts": event_ts, "event_timezone": "Asia/Shanghai"},
                ])
            calls = []
            stored = []

            async def call(prompt):
                calls.append(prompt)
                return json.dumps([diary("2026-07-19", "这其实发生在十八日上午。")], ensure_ascii=False)

            async def store(item, **kwargs):
                stored.append(item)
                return True

            plugin._call_llm_compress = call
            plugin._store_one_diary = store
            result = await plugin._compress_and_store("single", messages, diary_count=1, source_kind="auto")
            self.assertEqual(result, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(stored[0]["event_date"], "2026-07-18")
            self.assertEqual(stored[0]["time_label"], "上午")

    async def test_explicit_story_time_is_not_overwritten_by_recorded_time(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(temp)
            item = diary(
                "2025-12-24",
                "她明确说这是去年平安夜发生的事。",
                basis="explicit_dialogue",
            )
            normalized = plugin._normalize_buffer_diary_times(
                [item],
                ["2026-07-18"],
                {"2026-07-18": ["上午"]},
            )
            diagnostics = plugin._buffer_diary_time_diagnostics(
                normalized, ["2026-07-18"]
            )

            self.assertEqual(normalized[0]["event_date"], "2025-12-24")
            self.assertEqual(normalized[0]["time_basis"], "explicit_dialogue")
            self.assertEqual(diagnostics["invalid_conversation_now"], [])

    async def test_cross_day_eod_retries_missing_date_and_expands_target(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = self.make_plugin(temp)
            day_one = ts("2026-07-18T14:30:00")
            day_two = ts("2026-07-18T16:20:00")
            messages = []
            for i in range(2):
                messages.extend([
                    {"role": "user", "content": f"十八日夜里{i}", "event_ts": day_one, "event_timezone": "Asia/Shanghai"},
                    {"role": "assistant", "content": f"十八日回应{i}", "event_ts": day_one, "event_timezone": "Asia/Shanghai"},
                    {"role": "user", "content": f"十九日凌晨{i}", "event_ts": day_two, "event_timezone": "Asia/Shanghai"},
                    {"role": "assistant", "content": f"十九日回应{i}", "event_ts": day_two, "event_timezone": "Asia/Shanghai"},
                ])
            responses = [
                [diary("2026-07-19", "只覆盖了十九日。"), diary("2026-07-19", "仍然只是十九日。")],
                [diary("2026-07-18", "十八日夜里发生的事。"), diary("2026-07-19", "十九日凌晨发生的事。")],
            ]
            prompts = []
            stored = []

            async def call(prompt):
                prompts.append(prompt)
                return json.dumps(responses[len(prompts) - 1], ensure_ascii=False)

            async def store(item, **kwargs):
                stored.append(item)
                return True

            plugin._call_llm_compress = call
            plugin._store_one_diary = store
            result = await plugin._compress_and_store("cross", messages, diary_count=1, source_kind="eod")
            self.assertEqual(result, 2)
            self.assertEqual(len(prompts), 2)
            self.assertIn("2 条以内", prompts[0])
            self.assertIn("尚未覆盖这些有价值日期: 2026-07-18", prompts[1])
            self.assertEqual({x["event_date"] for x in stored}, {"2026-07-18", "2026-07-19"})

    async def test_request_snapshot_reaches_persistent_response_buffer(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin = MemosMemoryPlugin(FakeContext(), {
                "vec_db_path": str(Path(temp) / "memories.db"),
                "webui_enable": False,
                "enable_auto_compress": True,
                "enable_auto_recall": False,
                "rp_enhancer_enable": False,
                "context_governance_enable": False,
                "cache_prefix_drift_enable": False,
                "rp_time_timezone": "Asia/Shanghai",
            })
            fixed = datetime(2026, 7, 18, 1, 10, tzinfo=timezone.utc)
            plugin._request_now = lambda: fixed
            capture = CaptureBuffer()
            plugin._vec = capture
            plugin._initialized = True

            async def ready():
                return True

            async def noop(*args, **kwargs):
                return None

            plugin._ensure_init = ready
            plugin._xinchao.on_request = noop
            plugin._xinchao.on_response = noop
            event = FakeEvent("上午发生的事")
            request = ProviderRequest(prompt=event.message_str)
            await plugin.on_llm_request(event, request)
            await plugin.on_llm_response(event, SimpleNamespace(completion_text="我记得。", is_chunk=False))

            self.assertEqual(len(capture.rows), 2)
            self.assertEqual({row["event_ts"] for row in capture.rows}, {fixed.timestamp()})
            self.assertEqual({row["event_timezone"] for row in capture.rows}, {"Asia/Shanghai"})


if __name__ == "__main__":
    unittest.main()
