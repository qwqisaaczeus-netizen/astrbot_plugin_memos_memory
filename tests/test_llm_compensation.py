from __future__ import annotations

import asyncio
import base64
import json
import http.client
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory.external_models import (
    ExternalModelRegistry, migrate_external_model_state,
)
from astrbot_plugin_memos_memory.llm_compensation import LLMCompensationStore
from astrbot_plugin_memos_memory.llm_runtime import (
    LLMRouteExhaustedError, PluginLLMRuntime,
)
from astrbot_plugin_memos_memory.main import (
    MemosMemoryPlugin, _ACTIVE_SOURCE_BATCH, _memo_requires_sync,
)
from astrbot_plugin_memos_memory.webui import _make_handler


class AstrProvider:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def meta(self):
        return SimpleNamespace(id="astr:test")

    async def text_chat(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(completion_text="astr result")


class APIClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(completion_text="api result")

    async def close(self):
        pass


def model():
    return dict(id="model_backup", name="Backup", base_url="https://example.invalid/v1",
                model="backup", api_key="secret", enabled=True,
                timeout_seconds=20, max_output_tokens=8192)


class CompensationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.registry = ExternalModelRegistry(root)
        self.registry.save(dict(
            enabled=False, models=[model()],
            astr_followup_enabled=True,
            astr_followup_model_id="model_backup",
            astr_followup_tasks=["memory_generation"],
            astr_followup_timeout=30,
        ))
        self.api = APIClient()
        self.registry._clients["model_backup"] = self.api
        self.plugin = MemosMemoryPlugin.__new__(MemosMemoryPlugin)
        self.plugin._external_models = self.registry
        self.plugin._llm_runtime = PluginLLMRuntime()
        self.plugin._llm_compensation = LLMCompensationStore(root / "queue.db")
        self.plugin._log_event = lambda *args: None

    async def asyncTearDown(self):
        await self.plugin._llm_runtime.close()
        await self.registry.close()
        self.temp.cleanup()

    async def test_only_selected_task_uses_one_external_attempt(self):
        astr = AstrProvider(asyncio.TimeoutError())
        result = await self.plugin._plugin_llm_text_chat(
            astr, prompt="write diary", timeout=1, label="episode_extract"
        )
        self.assertEqual(result.completion_text, "api result")
        self.assertEqual(astr.calls, 1)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.calls[0]["request_max_retries"], 0)
        self.assertFalse(self.plugin._llm_compensation.list())

    async def test_preliminary_route_can_skip_followup_and_compensation(self):
        astr = AstrProvider(asyncio.TimeoutError())
        with self.assertRaises(asyncio.TimeoutError):
            await self.plugin._plugin_llm_text_chat(
                astr, prompt="large preliminary request", timeout=1,
                label="episode_extract", allow_followup=False,
                enqueue_compensation=False,
            )
        self.assertEqual(astr.calls, 1)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.plugin._llm_compensation.list())

    async def test_transport_gets_one_visible_astr_retry_before_followup(self):
        astr = AstrProvider(ConnectionError("Connection error."))
        result = await self.plugin._plugin_llm_text_chat(
            astr, prompt="write diary", timeout=1, label="episode_extract"
        )
        self.assertEqual(result.completion_text, "api result")
        self.assertEqual(astr.calls, 2)
        self.assertEqual(len(self.api.calls), 1)

    async def test_transport_without_followup_keeps_compensation(self):
        self.registry.save(dict(
            enabled=False, models=[model()], astr_followup_enabled=False,
            astr_followup_model_id="model_backup",
            astr_followup_tasks=["memory_generation"],
            astr_followup_timeout=30,
        ))
        astr = AstrProvider(ConnectionError("Connection error."))
        token = _ACTIVE_SOURCE_BATCH.set("batch_transport")
        try:
            with self.assertRaises(LLMRouteExhaustedError) as caught:
                await self.plugin._plugin_llm_text_chat(
                    astr, prompt="write diary", timeout=1, label="episode_extract"
                )
        finally:
            _ACTIVE_SOURCE_BATCH.reset(token)
        self.assertEqual(caught.exception.failure_kind, "transport")
        self.assertEqual(astr.calls, 2)
        rows = self.plugin._llm_compensation.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_batch_id"], "batch_transport")

    async def test_disabled_task_does_not_followup(self):
        astr = AstrProvider(asyncio.TimeoutError())
        with self.assertRaises(asyncio.TimeoutError):
            await self.plugin._plugin_llm_text_chat(
                astr, prompt="other", timeout=1, label="profile_llm"
            )
        self.assertEqual(self.api.calls, [])

    async def test_two_failed_routes_keep_durable_source_request(self):
        self.api.error = asyncio.TimeoutError()
        astr = AstrProvider(asyncio.TimeoutError())
        token = _ACTIVE_SOURCE_BATCH.set("batch_123")
        try:
            with self.assertRaisesRegex(RuntimeError, "补偿单"):
                await self.plugin._plugin_llm_text_chat(
                    astr, prompt="private words", timeout=1, label="episode_extract"
                )
        finally:
            _ACTIVE_SOURCE_BATCH.reset(token)
        rows = self.plugin._llm_compensation.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_batch_id"], "batch_123")
        self.assertNotIn("private words", str(rows))
        self.assertEqual(self.plugin._llm_compensation.get(rows[0]["id"])["request"]["prompt"], "private words")

    async def test_compensation_checkpoint_survives_failure_and_clears_on_resolve(self):
        store = self.plugin._llm_compensation
        record_id = store.enqueue(
            task="memory_generation", label="episode_extract",
            source_batch_id="batch_checkpoint", primary_error="timeout",
            fallback_error="timeout", request={"prompt": "source-backed"},
        )
        payload = {
            "version": 1,
            "cache_key": "source-hash",
            "shards": {"0:0": {"episodes": [{"episode_key": "e1"}]}},
        }
        store.checkpoint(record_id, payload)
        store.finish(record_id, "failed", error="render rejected")
        self.assertEqual(store.load_checkpoint(record_id), payload)
        store.resolve_batch("batch_checkpoint")
        self.assertEqual(store.load_checkpoint(record_id), {})

    async def test_registry_settings_reload_and_pool_stays_off(self):
        other = ExternalModelRegistry(Path(self.temp.name))
        self.assertFalse(other.enabled)
        self.assertTrue(other.astr_followup_enabled)
        self.assertEqual(other.astr_followup_tasks, ["memory_generation"])
        self.assertIsNotNone(other.followup_provider("memory_generation"))
        self.assertIsNone(other.followup_provider("profile"))
        await other.close()

    async def test_legacy_registry_and_compensation_migrate_without_overwrite(self):
        with tempfile.TemporaryDirectory() as stable_name:
            legacy = Path(self.temp.name)
            stable = Path(stable_name)
            LLMCompensationStore(legacy / "llm_compensation.db").enqueue(
                task="memory_generation", label="episode_extract",
                source_batch_id="batch_migrate", primary_error="transport",
                fallback_error="disabled", request={"prompt": "source-backed"},
            )
            result = migrate_external_model_state(legacy, stable)
            self.assertIn("external_models.json", result["copied"])
            migrated = ExternalModelRegistry(stable)
            self.assertTrue(migrated.astr_followup_enabled)
            self.assertEqual(migrated.astr_followup_tasks, ["memory_generation"])
            migrated_rows = LLMCompensationStore(stable / "llm_compensation.db").list()
            self.assertEqual(migrated_rows[0]["source_batch_id"], "batch_migrate")
            await migrated.close()

    async def test_other_xinchao_task_can_be_selected_independently(self):
        self.registry.save(dict(
            enabled=False, models=[model()], astr_followup_enabled=True,
            astr_followup_model_id="model_backup",
            astr_followup_tasks=["xinchao_other"],
            astr_followup_timeout=30,
        ))
        self.registry._clients["model_backup"] = self.api
        result = await self.plugin._plugin_llm_text_chat(
            AstrProvider(asyncio.TimeoutError()), prompt="assess", timeout=1,
            label="xinchao_body_assess",
        )
        self.assertEqual(result.completion_text, "api result")
        self.assertEqual(len(self.api.calls), 1)

    async def test_compensation_deduplicates_same_failed_request(self):
        store = self.plugin._llm_compensation
        args = dict(task="memory_generation", label="episode_extract",
                    source_batch_id="batch_123", primary_error="timeout",
                    fallback_error="timeout", request={"prompt": "one"})
        self.assertEqual(store.enqueue(**args), store.enqueue(**args))
        self.assertEqual(len(store.list()), 1)

    async def test_compensation_progress_survives_store_reopen(self):
        store = self.plugin._llm_compensation
        record_id = store.enqueue(
            task="memory_generation", label="episode_extract",
            source_batch_id="batch_progress", primary_error="timeout",
            fallback_error="timeout", request={"prompt": "one"},
        )
        self.assertTrue(store.start(record_id))
        store.progress(
            record_id, stage="regenerating", current=1, total=2,
            message="正在重建第一篇",
        )
        reopened = LLMCompensationStore(store.path)
        row = reopened.get(record_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["progress_stage"], "interrupted")
        self.assertIn("重载", row["progress_message"])

    async def test_model_recovery_list_exposes_output_preview_not_request(self):
        store = self.plugin._llm_compensation
        record_id = store.enqueue(
            task="creative", label="xinchao_dream",
            source_batch_id="", primary_error="timeout",
            fallback_error="timeout", request={"prompt": "private prompt"},
        )
        self.assertTrue(store.start(record_id))
        store.finish(
            record_id, "model_recovered", result_text="recovered answer",
            message="模型输出已取回",
        )
        row = store.list()[0]
        self.assertTrue(row["has_result"])
        self.assertEqual(row["result_preview"], "recovered answer")
        self.assertNotIn("request", row)
        self.assertNotIn("private prompt", json.dumps(row, ensure_ascii=False))

    async def test_committed_fallback_replays_by_replacing_in_place(self):
        class Archive:
            def batch_info(self, batch_id):
                return {"status": "committed", "batch_id": batch_id}

            def source_turns(self, batch_id):
                return [{"role": "assistant", "content": "original"}]

            def episodes_for_batch(self, batch_id):
                return [{"episode_id": "episode_one", "source_batch_id": batch_id}]

        self.plugin._episodes = Archive()
        invoked = []

        async def rebuild(record_id, batch_id, info, turns, episodes):
            invoked.append((record_id, batch_id, len(turns), len(episodes)))
            self.plugin._llm_compensation.resolve_batch(batch_id)
            return {"id": record_id, "status": "restored", "replaced": ["memos/original"]}

        self.plugin._rebuild_committed_compensation_batch = rebuild
        record_id = self.plugin._llm_compensation.enqueue(
            task="memory_generation", label="episode_extract",
            source_batch_id="batch_one", primary_error="timeout",
            fallback_error="timeout", request={"prompt": "source-backed"},
        )
        result = await self.plugin._restore_llm_compensation(record_id)
        self.assertEqual(result["status"], "restored")
        self.assertEqual(invoked, [(record_id, "batch_one", 1, 1)])
        self.assertEqual(self.plugin._llm_compensation.get(record_id)["status"], "restored")

    async def test_memory_compensation_retries_lazy_init_before_reading_archive(self):
        class Archive:
            def batch_info(self, batch_id):
                return {"status": "committed", "batch_id": batch_id}

            def source_turns(self, batch_id):
                return [{"role": "assistant", "content": "original"}]

            def episodes_for_batch(self, batch_id):
                return [{"episode_id": "episode_one", "source_batch_id": batch_id}]

        archive = Archive()
        self.plugin._episodes = None
        init_calls = []

        async def ensure_init():
            init_calls.append(True)
            self.plugin._episodes = archive
            return True

        async def rebuild(record_id, batch_id, info, turns, episodes):
            self.plugin._llm_compensation.resolve_batch(batch_id)
            return {"id": record_id, "status": "restored", "replaced": ["memos/original"]}

        self.plugin._ensure_init = ensure_init
        self.plugin._rebuild_committed_compensation_batch = rebuild
        record_id = self.plugin._llm_compensation.enqueue(
            task="memory_generation", label="episode_extract",
            source_batch_id="batch_lazy_init", primary_error="timeout",
            fallback_error="timeout", request={"prompt": "source-backed"},
        )
        result = await self.plugin._restore_llm_compensation(record_id)
        self.assertEqual(result["status"], "restored")
        self.assertEqual(init_calls, [True])

    async def test_committed_batch_regenerates_once_then_replaces_all(self):
        self.plugin.rp_time_timezone = "Asia/Shanghai"
        self.plugin.raw_archive_prompt_view_max_chars = 1200
        generated_calls = []
        saved = []
        confirmed = []

        async def generate(messages, text, count, **kwargs):
            generated_calls.append((len(messages), count, kwargs))
            return [
                {"episode_key": "day_one", "event_date": "2026-09-26",
                 "scene_start_turn": 0, "scene_end_turn": 1,
                 "_episode_extraction_mode": "llm_primary", "_render_fallback": False},
                {"episode_key": "day_two", "event_date": "2026-09-27",
                 "scene_start_turn": 2, "scene_end_turn": 3,
                 "_episode_extraction_mode": "llm_primary", "_render_fallback": False},
            ]

        async def save(old, diary, **kwargs):
            preview_id = "preview_" + str(len(saved) + 1)
            saved.append((old["episode_id"], diary["episode_key"], kwargs))
            return {"preview_id": preview_id}

        async def confirm(preview_id):
            confirmed.append(preview_id)
            return {"status": "confirmed", "preview_id": preview_id}

        self.plugin._generate_evidence_first_diaries = generate
        self.plugin._save_inplace_repair_preview = save
        self.plugin._confirm_fallback_repair = confirm
        self.plugin._rollback_diary_rewrite = lambda **kwargs: None
        record_id = self.plugin._llm_compensation.enqueue(
            task="memory_generation", label="episode_extract",
            source_batch_id="batch_two", primary_error="timeout",
            fallback_error="timeout", request={"prompt": "source-backed"},
        )
        self.plugin._llm_compensation.start(record_id)
        turns = [
            {"turn_index": index, "role": "user" if index % 2 == 0 else "assistant",
             "content": f"turn {index}", "event_ts": 1 + index,
             "event_timezone": "Asia/Shanghai"}
            for index in range(4)
        ]
        episodes = [
            {"episode_id": "old_one", "memo_name": "memos/one",
             "source_batch_id": "batch_two", "occurred_at": "2026-09-26",
             "scene_start_turn": 0, "scene_end_turn": 1},
            {"episode_id": "old_two", "memo_name": "memos/two",
             "source_batch_id": "batch_two", "occurred_at": "2026-09-27",
             "scene_start_turn": 2, "scene_end_turn": 3},
        ]
        result = await self.plugin._rebuild_committed_compensation_batch(
            record_id, "batch_two", {"source_kind": "eod"}, turns, episodes,
        )
        self.assertEqual(result["status"], "restored")
        self.assertEqual(len(generated_calls), 1)
        self.assertEqual([item[:2] for item in saved], [
            ("old_one", "day_one"), ("old_two", "day_two"),
        ])
        self.assertEqual(confirmed, ["preview_1", "preview_2"])

    def test_compensation_shards_keep_complete_exchanges(self):
        messages = []
        for index in range(7):
            messages.extend([
                {"role": "user", "content": "问" * (20 + index)},
                {"role": "assistant", "content": "答" * (30 + index)},
            ])
        shards = self.plugin._compensation_shards(
            messages, max_messages=4, max_chars=500,
        )
        self.assertEqual(
            [item for shard in shards for item in shard],
            list(range(len(messages))),
        )
        for shard in shards:
            self.assertEqual(len(shard) % 2, 0)
            self.assertEqual(messages[shard[0]]["role"], "user")
            self.assertEqual(messages[shard[-1]]["role"], "assistant")

    def test_cross_shard_evidence_budget_preserves_all_source_links(self):
        evidence = []
        for index in range(60):
            evidence.append({
                "kind": "commitment" if index % 7 == 0 else "event",
                "detail": f"必须证据 {index}", "tier": "must_write",
                "confidence": 0.8 + (index % 3) * 0.05,
                "grounded": True, "turn_indexes": [index],
            })
        for index in range(53):
            evidence.append({
                "kind": "event", "detail": f"支持证据 {index}",
                "tier": "supporting", "confidence": 0.8,
                "grounded": True, "turn_indexes": [60 + index],
            })
        result = self.plugin._budget_compensation_evidence(
            evidence, source_chars=14000,
        )
        counts = {
            tier: sum(1 for item in result if item["tier"] == tier)
            for tier in ("must_write", "supporting", "archive_only")
        }
        self.assertEqual(len(result), 113)
        self.assertEqual(counts["must_write"], 10)
        self.assertEqual(counts["supporting"], 4)
        self.assertEqual(counts["archive_only"], 99)
        self.assertTrue(all(item["tier_locked"] for item in result))
        self.assertEqual(
            {item["turn_indexes"][0] for item in result}, set(range(113)),
        )

    def test_cross_shard_budget_archives_duplicate_without_losing_trace(self):
        evidence = [{
            "kind": "commitment", "detail": "我答应以后会记得这件事",
            "tier": "must_write", "confidence": 0.9, "grounded": True,
            "turn_indexes": [2, 3],
        }, {
            "kind": "commitment", "detail": "我答应以后会记得这件事",
            "tier": "must_write", "confidence": 0.8, "grounded": True,
            "turn_indexes": [2, 3],
        }, {
            "kind": "event", "detail": "后来一起喝了茶",
            "tier": "supporting", "confidence": 0.8, "grounded": True,
            "turn_indexes": [4, 5],
        }]
        result = self.plugin._budget_compensation_evidence(
            evidence, source_chars=800,
        )
        duplicates = [
            item for item in result
            if item.get("compensation_selection") == "duplicate_archive"
        ]
        self.assertEqual(len(result), 3)
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["tier"], "archive_only")
        self.assertEqual(
            sum(item["tier"] == "must_write" for item in result), 1,
        )

    def test_compensation_render_projection_is_compact_and_non_destructive(self):
        evidence = [{
            "kind": "event", "detail": "细节" * 180,
            "quote": "原话" * 100, "tier": "must_write",
            "confidence": 0.9, "grounded": True, "turn_indexes": [index],
            "tier_locked": True,
        } for index in range(12)]
        evidence.extend({
            "kind": "event", "detail": f"仅存档 {index}", "quote": "",
            "tier": "archive_only", "confidence": 0.8, "grounded": True,
            "turn_indexes": [20 + index], "tier_locked": True,
        } for index in range(3))
        before = [dict(item) for item in evidence]
        projected = self.plugin._compensation_render_evidence(evidence)
        self.assertLessEqual(len(projected), 12)
        self.assertTrue(all(len(item["detail"]) <= 180 for item in projected))
        self.assertTrue(all(len(item["quote"]) <= 72 for item in projected))
        self.assertTrue(all(item["tier"] != "archive_only" for item in projected))
        self.assertEqual(evidence, before)

    async def test_sharded_compensation_maps_dates_and_global_turns(self):
        self.plugin.rp_time_timezone = "Asia/Shanghai"
        self.plugin.raw_archive_prompt_view_max_chars = 1200
        self.plugin.scene_split_gap_seconds = 10800
        self.plugin._scene_splitter = None
        self.plugin._compensation_progress = lambda *args, **kwargs: None
        extraction_calls = 0
        render_evidence_counts = []

        async def extract(messages, text, target, cap, candidates, **kwargs):
            nonlocal extraction_calls
            extraction_calls += 1
            del text, target, cap, candidates, kwargs
            return ([{
                "episode_key": "shard",
                "event_date": "",
                "time_label": "下午",
                "time_basis": "conversation_now",
                "scene_anchor": "连续相处",
                "scene_start_turn": 0,
                "scene_end_turn": len(messages) - 1,
                "memory_type": "daily_life",
                "evidence": [{
                    "kind": "event", "actor": "assistant",
                    "detail": "我记得这一段相处中的关键变化",
                    "quote": "", "turn_indexes": [0, len(messages) - 1],
                    "confidence": 0.9, "grounded": True,
                    "tier": "supporting",
                }],
                "affect_before": "平静", "affect_after": "亲近",
                "state_change": "关系更自然", "long_effect": "留下熟悉感",
                "trigger_hint": "再次说起这件事", "retrieval_key": "共同相处",
                "entities": ["两人"], "unresolved": [], "tags": [],
                "importance": 3,
            }], "llm_primary", [], {"total_ms": 1, "stage_ms": {}})

        async def render(messages, text, count, **kwargs):
            del messages, text, count
            diary = dict(kwargs["prebuilt_episodes"][0])
            render_evidence_counts.append(len(diary.get("evidence") or []))
            diary.update({
                "content": "我把这一天的相处重新写成了一篇完整而克制的日记。",
                "_render_fallback": False,
                "_episode_extraction_mode": kwargs["prebuilt_extraction_mode"],
            })
            return [diary]

        self.plugin._extract_episode_blueprints_resilient = extract
        self.plugin._generate_evidence_first_diaries = render
        day_one = datetime(2026, 9, 26, 8, tzinfo=timezone.utc).timestamp()
        day_two = datetime(2026, 9, 27, 8, tzinfo=timezone.utc).timestamp()
        messages = []
        for _index in range(2):
            messages.extend([
                {"role": "user", "content": "第一天", "event_ts": day_one,
                 "event_timezone": "Asia/Shanghai"},
                {"role": "assistant", "content": "第一天回应", "event_ts": day_one,
                 "event_timezone": "Asia/Shanghai"},
            ])
        for _index in range(12):
            messages.extend([
                {"role": "user", "content": "第二天提问" * 70, "event_ts": day_two,
                 "event_timezone": "Asia/Shanghai"},
                {"role": "assistant", "content": "第二天回应" * 70, "event_ts": day_two,
                 "event_timezone": "Asia/Shanghai"},
            ])
        old_rows = [
            {"occurred_at": "2026-09-26", "scene_start_turn": 0,
             "scene_end_turn": 3, "memory_type": "daily_life"},
            {"occurred_at": "2026-09-27", "scene_start_turn": -1,
             "scene_end_turn": -1, "memory_type": "daily_life"},
        ]
        record_id = self.plugin._llm_compensation.enqueue(
            task="memory_generation", label="episode_extract",
            source_batch_id="batch_shard_cache", primary_error="timeout",
            fallback_error="timeout", request={"prompt": "source-backed"},
        )
        diaries = await self.plugin._generate_compensation_diaries_sharded(
            record_id, old_rows, messages, list(range(len(messages))),
            source_kind="eod",
        )
        first_call_count = extraction_calls
        cached_diaries = await self.plugin._generate_compensation_diaries_sharded(
            record_id, old_rows, messages, list(range(len(messages))),
            source_kind="eod",
        )
        self.assertEqual([item["event_date"] for item in diaries], [
            "2026-09-26", "2026-09-27",
        ])
        self.assertEqual(
            [(item["scene_start_turn"], item["scene_end_turn"]) for item in diaries],
            [(0, 3), (4, len(messages) - 1)],
        )
        self.assertTrue(all(
            item["_episode_extraction_mode"] == "llm_sharded_recovery"
            for item in diaries
        ))
        self.assertGreater(first_call_count, 0)
        self.assertEqual(extraction_calls, first_call_count)
        self.assertEqual(
            [item["event_date"] for item in cached_diaries],
            ["2026-09-26", "2026-09-27"],
        )
        self.assertTrue(render_evidence_counts)
        self.assertLessEqual(max(render_evidence_counts), 4)
        self.assertGreaterEqual(
            len(cached_diaries[1].get("evidence") or []),
            render_evidence_counts[-1],
        )

    async def test_sharded_compensation_adaptively_splits_failed_shard(self):
        self.plugin.rp_time_timezone = "Asia/Shanghai"
        self.plugin.raw_archive_prompt_view_max_chars = 1800
        self.plugin.scene_split_gap_seconds = 10800
        self.plugin._scene_splitter = None
        self.plugin._compensation_progress = lambda *args, **kwargs: None
        call_sizes = []
        active_calls = 0
        max_active_calls = 0

        async def extract(messages, text, target, cap, candidates, **kwargs):
            nonlocal active_calls, max_active_calls
            del text, target, cap, candidates, kwargs
            call_sizes.append(len(messages))
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            try:
                await asyncio.sleep(0.005)
                if len(messages) > 2:
                    return ([{
                        "episode_key": "local", "scene_start_turn": 0,
                        "scene_end_turn": len(messages) - 1, "evidence": [],
                    }], "local_grounded_recovery", ["timeout"], {})
                return ([{
                    "episode_key": "small", "event_date": "2026-09-27",
                    "time_label": "晚上", "time_basis": "conversation_now",
                    "scene_anchor": "一段完整问答", "scene_start_turn": 0,
                    "scene_end_turn": len(messages) - 1,
                    "memory_type": "daily_life",
                    "evidence": [{
                        "kind": "event", "actor": "assistant",
                        "detail": "我记得这一轮里发生的具体小事",
                        "quote": "", "turn_indexes": [0, len(messages) - 1],
                        "confidence": 0.9, "grounded": True,
                        "tier": "supporting",
                    }],
                    "affect_before": "平静", "affect_after": "放松",
                    "state_change": "", "long_effect": "",
                    "trigger_hint": "再谈这件事", "retrieval_key": "完整问答",
                    "entities": [], "unresolved": [], "tags": [],
                    "importance": 3,
                }], "llm_primary", [], {})
            finally:
                active_calls -= 1

        async def render(messages, text, count, **kwargs):
            del messages, text, count
            diary = dict(kwargs["prebuilt_episodes"][0])
            diary.update({
                "content": "我把几轮相处收束成了一篇完整而自然的日记。",
                "_render_fallback": False,
                "_episode_extraction_mode": kwargs["prebuilt_extraction_mode"],
            })
            return [diary]

        self.plugin._extract_episode_blueprints_resilient = extract
        self.plugin._generate_evidence_first_diaries = render
        stamp = datetime(2026, 9, 27, 8, tzinfo=timezone.utc).timestamp()
        messages = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"turn-{index}-" + "内容" * 90,
                "event_ts": stamp,
                "event_timezone": "Asia/Shanghai",
            }
            for index in range(8)
        ]
        diaries = await self.plugin._generate_compensation_diaries_sharded(
            "record", [{
                "occurred_at": "2026-09-27", "scene_start_turn": 0,
                "scene_end_turn": 7, "memory_type": "daily_life",
            }], messages, list(range(20, 28)), source_kind="eod",
        )
        self.assertTrue(any(size > 2 for size in call_sizes))
        self.assertGreaterEqual(call_sizes.count(2), 3)
        self.assertLessEqual(max_active_calls, 2)
        self.assertEqual(len(diaries), 1)
        self.assertEqual(diaries[0]["scene_start_turn"], 20)
        self.assertEqual(diaries[0]["scene_end_turn"], 27)
        evidence_indexes = {
            index
            for item in diaries[0].get("evidence") or []
            for index in item.get("turn_indexes") or []
        }
        self.assertTrue(evidence_indexes.issubset(set(range(20, 28))))
        self.assertIn(20, evidence_indexes)
        self.assertIn(27, evidence_indexes)

    async def test_committed_batch_restores_prior_items_when_commit_fails(self):
        self.plugin.rp_time_timezone = "Asia/Shanghai"
        self.plugin.raw_archive_prompt_view_max_chars = 1200
        rolled_back = []

        async def generate(*args, **kwargs):
            return [
                {"episode_key": "one", "event_date": "2026-09-26",
                 "scene_start_turn": 0, "scene_end_turn": 1,
                 "_episode_extraction_mode": "llm_primary", "_render_fallback": False},
                {"episode_key": "two", "event_date": "2026-09-27",
                 "scene_start_turn": 2, "scene_end_turn": 3,
                 "_episode_extraction_mode": "llm_primary", "_render_fallback": False},
            ]

        async def save(old, diary, **kwargs):
            return {"preview_id": "preview_" + old["episode_id"]}

        async def confirm(preview_id):
            if preview_id.endswith("two"):
                raise RuntimeError("second write failed")
            return {"status": "confirmed", "preview_id": preview_id}

        async def rollback(*, preview_id):
            rolled_back.append(preview_id)
            return {"restored": 1, "errors": []}

        self.plugin._generate_evidence_first_diaries = generate
        self.plugin._save_inplace_repair_preview = save
        self.plugin._confirm_fallback_repair = confirm
        self.plugin._rollback_diary_rewrite = rollback
        record_id = self.plugin._llm_compensation.enqueue(
            task="memory_generation", label="episode_extract",
            source_batch_id="batch_rollback", primary_error="timeout",
            fallback_error="timeout", request={"prompt": "source-backed"},
        )
        self.plugin._llm_compensation.start(record_id)
        turns = [
            {"turn_index": index, "role": "user", "content": str(index),
             "event_ts": index + 1, "event_timezone": "Asia/Shanghai"}
            for index in range(4)
        ]
        episodes = [
            {"episode_id": "one", "memo_name": "memos/one",
             "source_batch_id": "batch_rollback", "occurred_at": "2026-09-26",
             "scene_start_turn": 0, "scene_end_turn": 1},
            {"episode_id": "two", "memo_name": "memos/two",
             "source_batch_id": "batch_rollback", "occurred_at": "2026-09-27",
             "scene_start_turn": 2, "scene_end_turn": 3},
        ]
        with self.assertRaisesRegex(RuntimeError, "second write failed"):
            await self.plugin._rebuild_committed_compensation_batch(
                record_id, "batch_rollback", {"source_kind": "eod"}, turns, episodes,
            )
        self.assertEqual(rolled_back, ["preview_one"])
        self.assertNotEqual(
            self.plugin._llm_compensation.get(record_id)["status"], "restored",
        )


class SyncHashTests(unittest.TestCase):
    def test_same_timestamp_but_edited_body_is_reindexed(self):
        existing = {"memos/a": (100.0, "old-hash")}
        self.assertTrue(_memo_requires_sync("memos/a", 100.0, "new-hash", existing))
        self.assertFalse(_memo_requires_sync("memos/a", 100.0, "old-hash", existing))
        self.assertTrue(_memo_requires_sync("memos/b", 100.0, "new-hash", existing))

    def test_portable_source_range_does_not_claim_unrelated_turns(self):
        class Archive:
            def batch_info(self, batch_id):
                return {"batch_id": batch_id}

            def source_turns(self, batch_id):
                return [
                    {"turn_index": i, "role": "assistant" if i % 2 else "user",
                     "content": f"source turn {i}"}
                    for i in range(6)
                ]

        meta = dict(source_batch_id="batch_test", source_turn_start=2,
                    source_turn_end=5, episode_key="second_scene")
        encoded = base64.urlsafe_b64encode(json.dumps(meta).encode()).decode().rstrip("=")
        memo = dict(name="memos/second", createTime="2026-09-23T11:00:00Z",
                    updateTime="2026-09-23T11:00:00Z",
                    content="2026年9月23日 · 傍晚\n我记得傍晚的谈话。\n"
                            f"<!-- memos-memory:importance=4;manual=0;type=plot_fact;"
                            f"occurred_at=2026-09-23;time_basis=conversation_now;meta64={encoded} -->")
        plugin = MemosMemoryPlugin.__new__(MemosMemoryPlugin)
        plugin._episodes = Archive()
        plugin.rp_time_timezone = "Asia/Shanghai"
        plugin._auto_importance = lambda text: 3
        episode, _ = plugin._legacy_episode_payload(memo)
        self.assertEqual(episode["_portable_source_batch_id"], "batch_test")
        self.assertEqual((episode["scene_start_turn"], episode["scene_end_turn"]), (2, 5))
        self.assertEqual([e["turn_indexes"][0] for e in episode["evidence"]], [3, 5])


class CompensationHttpTests(unittest.TestCase):
    def test_page_list_origin_and_write_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = LLMCompensationStore(Path(temp) / "queue.db")
            record_id = store.enqueue(
                task="memory_generation", label="episode_extract",
                source_batch_id="batch_one", primary_error="timeout",
                fallback_error="timeout", request={"prompt": "private request"},
            )
            async def restore(_):
                return {"status": "restored"}

            plugin = SimpleNamespace(_llm_compensation=store, _webui=None,
                                     _start_llm_compensation=restore)
            handler = _make_handler(
                plugin, "dashboard", "console", "xinchao", "production",
                "access", "forgetting", "models", compensation_html="COMPENSATION_PAGE",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address

                def request(method, path, payload=None, origin=None):
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    headers = {"Content-Type": "application/json"}
                    if origin:
                        headers["Origin"] = origin
                    conn.request(method, path, body=json.dumps(payload) if payload else None,
                                 headers=headers)
                    response = conn.getresponse()
                    data = response.read().decode("utf-8")
                    status = response.status
                    conn.close()
                    return status, data

                status, page = request("GET", "/compensation")
                self.assertEqual((status, page), (200, "COMPENSATION_PAGE"))
                status, body = request("GET", "/api/compensation")
                self.assertEqual(status, 200)
                self.assertIn(record_id, body)
                self.assertNotIn("private request", body)
                status, _ = request("POST", "/api/compensation/dismiss",
                                    {"id": record_id}, origin="https://foreign.invalid")
                self.assertEqual(status, 403)
                self.assertEqual(store.get(record_id)["status"], "pending")
                status, _ = request("POST", "/api/compensation/restore", {"id": record_id})
                self.assertEqual(status, 503)
                self.assertEqual(store.get(record_id)["status"], "pending")
                status, _ = request("POST", "/api/compensation/dismiss", {"id": record_id})
                self.assertEqual(status, 200)
                self.assertEqual(store.get(record_id)["status"], "dismissed")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
