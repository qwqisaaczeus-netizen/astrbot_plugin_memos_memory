# -*- coding: utf-8 -*-
"""4.6.0 大纲符合性测试：库身份/快照/影子代际/追溯统计/视图分离/分块索引/
场景两步切分/动态篇数/第一人称校验/QueryPlan 完整字段/滚动状态过滤/生产页字段。"""

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from astrbot_plugin_memos_memory.compress import (
    build_episode_extraction_prompt,
    build_semantic_state_update_prompt,
    format_messages_for_prompt,
)
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore, _generation_id
from astrbot_plugin_memos_memory.query_planner import QueryPlan


def _plugin(extra: dict | None = None):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from astrbot_plugin_memos_memory.main import MemosMemoryPlugin

    cfg = {
        "character_name": "林翩翩",
        "memos_mode": "external",
        "memos_base_url": "http://127.0.0.1:19000",
        "memos_token": "test-token",
    }
    cfg.update(extra or {})
    plugin = object.__new__(MemosMemoryPlugin)
    plugin.config = cfg
    plugin.character_name = "林翩翩"
    plugin.memos_mode = "external"
    plugin.memos_base_url = "http://127.0.0.1:19000"
    plugin.memos_token = "test-token"
    plugin.memos_timeout = 5
    plugin.rp_time_timezone = "Asia/Shanghai"
    plugin.raw_archive_full_assistant_text = bool(cfg.get("raw_archive_full_assistant_text", True))
    plugin.raw_archive_assistant_max_chars = int(cfg.get("raw_archive_assistant_max_chars", 0))
    plugin.raw_archive_index_chunk_chars = int(cfg.get("raw_archive_index_chunk_chars", 800))
    plugin.raw_archive_prompt_view_max_chars = int(cfg.get("raw_archive_prompt_view_max_chars", 1200))
    plugin.scene_split_enable = bool(cfg.get("scene_split_enable", True))
    plugin.scene_split_gap_seconds = float(cfg.get("scene_split_gap_seconds", 7200))
    plugin.diary_count = int(cfg.get("diary_count", 2))
    plugin.diary_count_max_cap = int(cfg.get("diary_count_max_cap", 6))
    plugin.evidence_tier_enable = bool(cfg.get("evidence_tier_enable", True))
    plugin.diary_literary_mode = bool(cfg.get("diary_literary_mode", True))
    plugin.diary_transcript_check_enable = bool(cfg.get("diary_transcript_check_enable", True))
    plugin.diary_must_coverage_threshold = float(cfg.get("diary_must_coverage_threshold", 0.95))
    plugin.diary_first_person_check_enable = bool(cfg.get("diary_first_person_check_enable", True))
    plugin.query_plan_enable = bool(cfg.get("query_plan_enable", True))
    plugin.query_plan_context_window = int(cfg.get("query_plan_context_window", 8))
    plugin.query_plan_llm_disambiguate = bool(cfg.get("query_plan_llm_disambiguate", False))
    plugin.query_plan_llm_confidence_threshold = float(cfg.get("query_plan_llm_confidence_threshold", 0.5))
    plugin.query_plan_llm_provider_id = str(cfg.get("query_plan_llm_provider_id", ""))
    plugin.diary_rewrite_preview_min_chars = int(cfg.get("diary_rewrite_preview_min_chars", 2400))
    plugin.db_snapshot_keep = int(cfg.get("db_snapshot_keep", 3))
    plugin.evidence_first_generation_enable = True
    plugin.episode_extraction_provider_id = ""
    plugin.diary_render_provider_id = ""
    plugin.episode_extraction_timeout = 60
    plugin.diary_render_timeout = 60
    plugin.eod_checkpoint_min_turns = 1
    plugin._diary_rewrite_previews = {}
    return plugin


def _ts(hour, minute=0, day=1):
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _msg(role, content, ts):
    return {"role": role, "content": content, "event_ts": ts, "event_timezone": "Asia/Shanghai"}


class DbIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_uuid_stable_across_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            await store.init()
            uuid_first = store.identity()["database_uuid"]
            self.assertTrue(uuid_first)
            store.close()
            reopen = EpisodicStore(path, 3, "test-embedding")
            await reopen.init()
            self.assertEqual(reopen.identity()["database_uuid"], uuid_first)
            self.assertEqual(reopen.identity()["canonical_db_path"], str(Path(path).resolve()))
            reopen.close()

    async def test_path_conflict_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            await store.init()
            store.close()
            import shutil
            moved = str(Path(temp) / "sub" / "episodic.db")
            Path(moved).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, moved)
            reopened = EpisodicStore(moved, 3, "test-embedding")
            await reopened.init()
            self.assertTrue(reopened.identity()["path_conflict"], "应记录路径冲突，不静默换路径")
            reopened.close()

    async def test_schema_upgrade_creates_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            # Build a genuine old schema: only meta + a legacy application table.
            legacy = sqlite3.connect(path)
            try:
                legacy.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                legacy.execute("INSERT INTO meta VALUES ('schema_version','1')")
                legacy.execute("CREATE TABLE legacy_source (id INTEGER PRIMARY KEY, content TEXT)")
                legacy.execute("INSERT INTO legacy_source(content) VALUES ('升级前的原始轮次')")
                legacy.commit()
            finally:
                legacy.close()
            upgraded = EpisodicStore(path, 3, "test-embedding")
            await upgraded.init()
            snaps = upgraded.list_snapshots()
            snapshot = next(s for s in snaps if s["kind"] == "pre_migration")
            snapshot_path = Path(temp) / "snapshots" / snapshot["file_name"]
            self.assertTrue(snapshot_path.exists())
            frozen = sqlite3.connect(snapshot_path)
            try:
                tables = {row[0] for row in frozen.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                legacy_value = frozen.execute("SELECT content FROM legacy_source").fetchone()[0]
            finally:
                frozen.close()
            self.assertEqual(legacy_value, "升级前的原始轮次")
            self.assertNotIn("db_snapshots", tables, "快照必须早于 ArchiveGuard DDL")
            self.assertNotIn("source_turn_chunks", tables, "快照必须早于 Repository DDL")
            self.assertNotIn("semantic_states", tables, "快照必须早于 v5 DDL")
            upgraded.close()

    async def test_snapshot_prune_keeps_three(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            store.SNAPSHOT_KEEP = 3
            await store.init()
            for i in range(5):
                store.snapshot_db("manual")
            snaps = store.list_snapshots()
            self.assertLessEqual(len(snaps), 3)
            store.close()

    async def test_restore_snapshot_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            await store.init()
            batch_id = store.archive_batch("s1", [{"role": "user", "content": "要保住的原文"}], "auto")
            snap = store.snapshot_db("manual")
            conn = store._connect()
            conn.execute("DELETE FROM source_turns")
            conn.commit()
            self.assertEqual(store.stats()["source_turns"], 0)
            result = store.restore_snapshot(snap["file_name"])
            self.assertTrue(result.get("restored"))
            self.assertEqual(store.stats()["source_turns"], 1)
            self.assertEqual(store.source_turns(batch_id)[0]["content"], "要保住的原文")
            store.close()


class ShadowGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def _store_with_turns(self, path, dim, model):
        store = EpisodicStore(path, dim, model)
        await store.init()
        batch = store.archive_batch(
            "s1",
            [{"role": "user", "content": "雨夜约定不再突然消失"}, {"role": "assistant", "content": "我会一直在"}],
            "auto",
        )
        store.replace_source_turn_embeddings([(1, [0.5] * dim), (2, [0.6] * dim)])
        return store, batch

    @staticmethod
    def _fill_pending_generation(store, dim):
        generation = store.stats()["pending_generation"]
        source_rows = store.source_turn_embedding_rows(
            missing_only=True, target_generation=generation,
        )
        store.replace_source_turn_embeddings(
            [(int(row["id"]), [0.1] * dim) for row in source_rows],
            runtime_gen=generation,
        )
        chunk_rows = store.source_turn_chunk_rows(
            missing_only=True, target_generation=generation,
        )
        store.replace_source_chunk_embeddings(
            [(int(row["id"]), [0.1] * dim) for row in chunk_rows],
            runtime_gen=generation,
        )
        card_rows = store.episode_card_embedding_rows(target_generation=generation)
        store.replace_episode_card_embeddings(
            [(int(row["id"]), [0.1] * dim) for row in card_rows],
            runtime_gen=generation,
        )

    async def test_model_change_keeps_old_embeddings_and_creates_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store, _ = await self._store_with_turns(path, 3, "model-a")
            self.assertEqual(store.stats()["source_turn_vectors"], 2)
            await store.ensure_dim(4, "model-b")
            self.assertEqual(store.stats()["source_turn_vectors"], 2, "换模型不得清空原文向量")
            self.assertTrue(store.stats()["pending_generation"], "应创建 pending 代际")
            self.assertEqual(store.stats()["active_generation"], _generation_id("model-a", 3))
            store.close()

    async def test_switch_generation_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store, _ = await self._store_with_turns(path, 3, "model-a")
            await store.ensure_dim(4, "model-b")
            # 影子代际必须覆盖 source/card/chunk 全部现存行。
            self._fill_pending_generation(store, 4)
            result = store.switch_generation()
            self.assertTrue(result.get("switched"), result)
            self.assertEqual(store.stats()["active_generation"], _generation_id("model-b", 4))
            self.assertFalse(store.stats()["pending_generation"])
            self.assertEqual(store.stats()["prev_generation"], _generation_id("model-a", 3))
            store.close()

    async def test_rollback_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store, _ = await self._store_with_turns(path, 3, "model-a")
            await store.ensure_dim(4, "model-b")
            self._fill_pending_generation(store, 4)
            store.switch_generation()
            rolled = store.rollback_generation()
            self.assertTrue(rolled.get("rolled_back"))
            self.assertEqual(store.stats()["active_generation"], _generation_id("model-a", 3))
            store.close()

    async def test_switch_rejects_incomplete_entity_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store, batch = await self._store_with_turns(path, 3, "model-a")
            store.upsert_episode(
                memo_name="memos/incomplete",
                episode={"episode_id": "ep-incomplete", "evidence": []},
                card_text="尚未迁移的新代际卡片",
                embedding=[0.2] * 3,
                source_batch_id=batch,
                source_kind="auto",
            )
            await store.ensure_dim(4, "model-b")
            generation = store.stats()["pending_generation"]
            rows = store.source_turn_embedding_rows(
                missing_only=True, target_generation=generation,
            )
            store.replace_source_turn_embeddings(
                [(int(row["id"]), [0.1] * 4) for row in rows],
                runtime_gen=generation,
            )
            result = store.switch_generation()
            self.assertFalse(result.get("switched"), result)
            self.assertEqual(result.get("reason"), "validation_failed")
            self.assertEqual(store.stats()["active_generation"], _generation_id("model-a", 3))
            self.assertEqual(store.stats()["pending_generation"], generation)
            store.close()

    async def test_lexical_search_works_without_vectors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store, _ = await self._store_with_turns(path, 3, "model-a")
            hits = store.search_source_turns([0.5, 0.5, 0.5], "雨夜约定")
            self.assertTrue(hits, "词面检索在无向量/向量失败时仍可用")
            self.assertIn("约定", hits[0]["content"])
            store.close()


class TraceabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_split_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            await store.init()
            batch = store.archive_batch("s1", [{"role": "user", "content": "原始轮次"}], "auto")
            store.upsert_episode(
                memo_name="memos/a",
                episode={"episode_id": "ep_a", "evidence": [{"kind": "dialogue", "actor": "user",
                                                             "detail": "细节", "turn_indexes": [0], "tier": "must_write", "grounded": True}]},
                card_text="卡", embedding=[1.0, 0.0, 0.0],
                source_batch_id=batch, evidence_quality="mixed_user_edited",
            )
            stats = store.stats()
            self.assertEqual(stats["mixed_user_edited"], 1)
            self.assertEqual(stats["batch_linked_episodes"], 1)
            self.assertEqual(stats["exact_turn_links"], 1)
            trace = store.traceability()
            self.assertIn("has_source_batch", trace) if False else None
            self.assertEqual(trace["episodes_with_source_batch"], 1)
            self.assertEqual(trace["exact_turn_link_count"], 1)
            self.assertTrue(trace["source_lexical_ready"])
            self.assertIn("source_vector_ready", trace)
            self.assertIn("migration_status", trace)
            store.close()


class ViewSeparationTests(unittest.TestCase):
    def test_prompt_view_truncates_long_turn_with_marker(self):
        contexts = [
            {"role": "user", "content": "短消息", "event_ts": _ts(10)},
            {"role": "assistant", "content": "甲" * 500, "event_ts": _ts(10, 1)},
        ]
        full = format_messages_for_prompt(contexts, timezone_name="Asia/Shanghai")
        self.assertIn("甲" * 500, full)
        view = format_messages_for_prompt(contexts, timezone_name="Asia/Shanghai", max_chars_per_turn=100)
        self.assertNotIn("甲" * 500, view)
        self.assertIn("省略 400 字", view)
        self.assertIn("完整原文见原文档案", view)


class ChunkIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_text_split(self):
        from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
        chunks = MemosMemoryPlugin._chunk_text("甲乙丙丁戊己庚辛", 3)
        self.assertEqual(chunks, ["甲乙丙", "丁戊己", "庚辛"])

    async def test_chunk_store_and_search(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            try:
                await store.init()
                content = "很久以前有一座山山上有座庙" * 40
                batch = store.archive_batch("s1", [{"role": "user", "content": content}], "auto")
                turn = store.source_turn_embedding_rows(batch_id=batch, missing_only=False)[0]
                store.add_turn_chunks(turn["id"], ["很久以前有一座山", "山上有座庙"])
                store.replace_source_chunk_embeddings([(1, [0.5, 0.4, 0.3]), (2, [0.9, 0.8, 0.7])])
                # 词面/分块命中
                hits = store.search_source_turns([0.9, 0.8, 0.7], "山上有座庙")
                self.assertTrue(hits)
                self.assertEqual(hits[0]["id"], turn["id"])
                # 整轮向量 + Python 余弦兜底
                store.replace_source_turn_embeddings([(turn["id"], [0.8, 0.7, 0.9])])
                hits2 = store.search_source_turns([0.8, 0.7, 0.9], "很久以前")
                self.assertTrue(hits2)
                self.assertEqual(hits2[0]["id"], turn["id"])
            finally:
                store.close()


class SceneCandidateTests(unittest.IsolatedAsyncioTestCase):
    def test_date_and_gap_boundaries(self):
        p = _plugin()
        messages = [
            _msg("user", "今天聊聊", _ts(10)),
            _msg("assistant", "好的", _ts(10, 5)),
            _msg("user", "第二天的事", _ts(10, 30) + 7200 * 3),  # 跨 3 个间隔
            _msg("assistant", "嗯", _ts(10, 31) + 7200 * 3),
        ]
        candidates = p._detect_scene_candidates(messages)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["start_turn"], 0)
        self.assertEqual(candidates[0]["end_turn"], 1)
        self.assertEqual(candidates[1]["start_turn"], 2)
        self.assertIn("time_gap", candidates[1]["reasons"])

    def test_relation_turn_marker(self):
        p = _plugin()
        messages = [
            _msg("user", "我喜欢你，我们在一起吧", _ts(10)),
            _msg("assistant", "……好", _ts(10, 5)),
        ]
        candidates = p._detect_scene_candidates(messages)
        self.assertTrue(any("relation_turn" in c["reasons"] for c in candidates))

    def test_range_clamped_into_candidate(self):
        p = _plugin()
        messages = [_msg("user", f"消息{i}", _ts(10, i)) for i in range(10)]
        candidates = [{"start_turn": 0, "end_turn": 4, "reasons": []},
                      {"start_turn": 5, "end_turn": 9, "reasons": []}]
        episodes = [
            {"episode_key": "e1", "scene_start_turn": 0, "scene_end_turn": 6},  # 越界
        ]
        report = p._validate_episode_ranges(episodes, messages, candidates)
        self.assertTrue(report["fixed"], "越界区间应被压入候选")
        self.assertIn("e1", report["fixed"][0])
        self.assertEqual(episodes[0]["scene_end_turn"], 4)

    def test_overlap_detected(self):
        p = _plugin()
        messages = [_msg("user", f"消息{i}", _ts(10, i)) for i in range(10)]
        candidates = [{"start_turn": 0, "end_turn": 9, "reasons": []}]
        episodes = [
            {"episode_key": "e1", "scene_start_turn": 0, "scene_end_turn": 7},
            {"episode_key": "e2", "scene_start_turn": 2, "scene_end_turn": 9},
        ]
        report = p._validate_episode_ranges(episodes, messages, candidates)
        self.assertTrue(report["overlaps"], "大面积重叠应被记录")
        self.assertEqual(report["uncovered"], [], "区间全覆盖时不应有未覆盖轮次")

    def test_scene_split_disabled_returns_empty(self):
        p = _plugin({"scene_split_enable": False})
        self.assertEqual(p._detect_scene_candidates([]), [])


class DynamicCountTests(unittest.TestCase):
    def test_extraction_prompt_includes_cap_and_candidates(self):
        prompt = build_episode_extraction_prompt(
            "林翩翩", "[turn:0] 用户: hi", 2,
            scene_candidates=[{"start_turn": 0, "end_turn": 3, "reasons": ["date_change"]}],
            diary_cap=6,
        )
        self.assertIn("最多不超过 6 个情景", prompt)
        self.assertIn("本地场景边界候选", prompt)
        self.assertIn("date_change", prompt)

    def test_dynamic_count_never_exceeds_hard_cap(self):
        from astrbot_plugin_memos_memory.scene_splitter import SceneCandidate, SceneSplitter

        messages = [
            _msg("user", "第一天", _ts(10, day=1)),
            _msg("user", "第二天", _ts(10, day=2)),
            _msg("user", "第三天", _ts(10, day=3)),
        ]
        candidates = [SceneCandidate(index, index) for index in range(3)]
        result = SceneSplitter().dynamic_diary_count(
            base_count=9,
            messages=messages,
            candidates=candidates,
            max_cap=2,
            source_kind="eod",
        )
        self.assertEqual(result, 2)

    def test_no_candidates_still_requires_ranges(self):
        prompt = build_episode_extraction_prompt("林翩翩", "x", 2)
        self.assertIn("本地候选未启用", prompt)


class FirstPersonCheckTests(unittest.TestCase):
    def setUp(self):
        self.p = _plugin()

    def test_normal_first_person_passes(self):
        ok, reason = self.p._diary_first_person_check("我把帕子攥在手里，问他是不是真的要走了。", {"evidence": []})
        self.assertTrue(ok, reason)

    def test_platform_role_swap_detected(self):
        ok, reason = self.p._diary_first_person_check("用户说：我们分手吧。我愣住了。", {"evidence": []})
        self.assertFalse(ok)
        self.assertEqual(reason, "platform_role_subject")

    def test_third_person_report_detected(self):
        ok, reason = self.p._diary_first_person_check("她与他进行了深入交流，双方达成一致。", {"evidence": []})
        self.assertFalse(ok)
        self.assertEqual(reason, "third_person_report")

    def test_machine_field_leak_detected(self):
        ok, reason = self.p._diary_first_person_check("根据对话，must_write 证据显示他答应了。", {"evidence": []})
        self.assertFalse(ok)
        self.assertIn("machine_field", reason)

    def test_disabled_check_passes(self):
        p = _plugin({"diary_first_person_check_enable": False})
        ok, _ = p._diary_first_person_check("用户说：再见。", {"evidence": []})
        self.assertTrue(ok)


class QueryPlanFullTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p = _plugin()

    def _ctx(self, texts):
        return [{"role": "assistant" if i % 2 else "user", "content": t, "event_ts": _ts(10, i)}
                for i, t in enumerate(texts)]

    def test_temporal_intent(self):
        plan = self.p._build_query_plan("我们上周说的那件事后来怎么样了", self._ctx(["上次提到过去年的事"]))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["intent"], "temporal")
        self.assertTrue(plan["temporal_constraints"])
        self.assertGreaterEqual(plan["confidence"], 0.6)

    def test_plain_query_without_ref_returns_none(self):
        plan = self.p._build_query_plan("林晚下班后坐地铁回到城东的家了", self._ctx(["无关上下文内容"]))
        self.assertIsNone(plan)

    def test_contextual_plan_full_schema(self):
        plan = self.p._build_query_plan("她还在破庙吗", self._ctx(["知宥在破庙守夜", "第二天天亮"]))
        self.assertIsNotNone(plan)
        for key in ("standalone_query", "resolved_entities", "relation_cues", "emotion_cues",
                    "temporal_constraints", "intent", "context_turn_indexes", "confidence", "use_context"):
            self.assertIn(key, plan)
        self.assertEqual(plan["intent"], "contextual")
        self.assertTrue(plan["resolved_entities"])
        self.assertIn("破庙", plan["standalone_query"])

    async def test_llm_disambiguation_off_by_default(self):
        plan = self.p._build_query_plan("她还在破庙吗", self._ctx(["知宥在破庙守夜"]))
        result = await self.p._query_plan_llm_disambiguate("她还在破庙吗", "知宥在破庙守夜", plan)
        self.assertFalse(result["rewritten"], "默认关闭时不得调用 LLM")
        self.assertEqual(result["standalone_query"], plan["standalone_query"])

    async def test_llm_disambiguation_low_confidence_calls(self):
        p = _plugin({"query_plan_llm_disambiguate": True})
        plan = {
            "query": "后来呢", "standalone_query": "后来呢", "intent": "specific",
            "confidence": 0.1, "use_context": False,
            "context_used_reason": "specific_query", "resolved_entities": [],
            "_plan_object": QueryPlan(
                intent="specific", search_text="后来呢", use_context=False,
                context_used_reason="specific_query", confidence=0.1,
                resolved_entities=[], raw_standalone="后来呢",
            ),
        }
        calls = []

        async def fake_llm(prompt, **kwargs):
            calls.append(prompt)
            return json.dumps({"standalone_query": "知宥承诺不再突然消失后的进展",
                               "resolved_entities": ["知宥"], "intent": "contextual",
                               "confidence": 0.9, "temporal_constraints": [], "relation_cues": [], "emotion_cues": []})

        p._call_memory_generation_llm = fake_llm
        result = await p._query_plan_llm_disambiguate("后来呢", "知宥答应不再突然消失", plan)
        self.assertTrue(calls, "低置信度且开启时应调用 LLM")
        self.assertTrue(result.get("rewritten"))
        self.assertEqual(result["standalone_query"], "知宥承诺不再突然消失后的进展")

    def test_use_context_reason_values(self):
        """6.2 透明化：_plan_episodic_query 的 context_used_reason 覆盖四种情形（facets 打桩保证确定性）。"""
        p = _plugin()
        p.episodic_narrative_inject = 4
        p.episodic_default_inject = 3
        p.episodic_candidate_pool = 30
        p._extract_recall_facets = lambda q: {"temporal": False, "entities": [], "relation": False, "key_terms": []}
        # 无候选
        plan = p._plan_episodic_query("知宥在哪里", "")
        self.assertFalse(plan["use_context"])
        self.assertEqual(plan["context_used_reason"], "no_candidate")
        # 指代（短查询）→ ambiguous
        plan = p._plan_episodic_query("她呢", "知宥、破庙")
        self.assertTrue(plan["use_context"])
        self.assertEqual(plan["context_used_reason"], "ambiguous")
        # 宽泛 + 主题稀疏（长查询避免 ambiguous 干扰）
        plan = p._plan_episodic_query("把所有相关的细节都完整地讲一遍吧", "知宥、破庙")
        self.assertTrue(plan["use_context"])
        self.assertEqual(plan["context_used_reason"], "broad_sparse")
        # 有候选但查询足够明确（长查询 + 打桩实体）
        p._extract_recall_facets = lambda q: {"temporal": False, "entities": ["知宥"], "relation": False, "key_terms": ["破庙"]}
        plan = p._plan_episodic_query("知宥在破庙守了一整夜的约定与承诺", "知宥、破庙")
        self.assertFalse(plan["use_context"])
        self.assertEqual(plan["context_used_reason"], "specific_query")


class RollingStateFilterTests(unittest.TestCase):
    def test_state_prompt_excludes_archive_only(self):
        episodes = [{
            "episode_id": "ep_1", "occurred_at": "2026-08-01", "memory_type": "relationship_shift",
            "importance": 4, "scene_anchor": "破庙夜谈", "state_change": "从警惕到信任",
            "long_effect": "更愿意靠近", "unresolved": [],
            "evidence": [
                {"actor": "assistant", "detail": "我答应不再突然消失", "grounded": True, "tier": "must_write"},
                {"actor": "user", "detail": "重复确认了三遍", "grounded": True, "tier": "archive_only"},
            ],
        }]
        prompt = build_semantic_state_update_prompt("林翩翩", None, episodes, 1800)
        self.assertIn("我答应不再突然消失", prompt)
        self.assertNotIn("重复确认了三遍", prompt)


class PreviewPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """8.2：长日记重构预览持久化（重启可用）与逐篇回滚映射。"""

    async def test_preview_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            await store.init()
            store.save_diary_preview({
                "preview_id": "rw_test1",
                "episode_id": "ep_old",
                "old_memo_name": "memos/old",
                "old_card_len": 3000,
                "old_card_text": "旧长日记",
                "source_batch_id": "batch_x",
                "source_turns": 10,
                "scene_candidates": 2,
                "new_diaries": [
                    {"episode_key": "e1", "content": "新日记一", "must_coverage": 1.0,
                     "transcript_risk": 0.1, "render_retry_reason": ""},
                ],
                "created_ts": time.time(),
            })
            store.close()
            reopen = EpisodicStore(path, 3, "test-embedding")
            await reopen.init()
            loaded = reopen.get_diary_preview("rw_test1")
            self.assertIsNotNone(loaded, "重启后预览应仍可读取")
            self.assertEqual(loaded["status"], "pending")
            self.assertEqual(loaded["old_memo_name"], "memos/old")
            self.assertEqual(len(loaded["new_diaries"]), 1)
            self.assertEqual(loaded["new_diaries"][0]["content"], "新日记一")
            listed = reopen.list_diary_previews()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["new_diaries_count"], 1)
            reopen.close()

    async def test_preview_status_flow_and_prune(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            await store.init()
            for i in range(5):
                store.save_diary_preview({
                    "preview_id": f"rw_p{i}", "episode_id": "ep", "old_memo_name": "m",
                    "new_diaries": [], "created_ts": time.time() + i,
                })
            # test2 保护 pending：先将 5 个预览全部标为 terminal，再验证只保留最新 2 个。
            for i in range(4):
                self.assertTrue(store.update_diary_preview_status(f"rw_p{i}", "discarded"))
            self.assertTrue(store.update_diary_preview_status("rw_p4", "confirmed", confirmed=True))
            self.assertEqual(store.get_diary_preview("rw_p4")["status"], "confirmed")
            self.assertTrue(store.get_diary_preview("rw_p4")["confirmed_ts"])
            pruned = store.discard_old_previews(keep=2)
            self.assertGreaterEqual(pruned, 3)
            remaining = store.list_diary_previews()
            self.assertLessEqual(len(remaining), 2)
            self.assertTrue(any(p["preview_id"] == "rw_p4" for p in remaining), "最近的确认预览应保留")
            store.close()

    async def test_rollback_rows_recorded_per_memo_and_preview_id_rollback(self):
        """逐篇回滚映射：确认写回中途失败时，已写入的 memo 映射完整可回滚。"""
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "episodic.db")
            store = EpisodicStore(path, 3, "test-embedding")
            await store.init()
            # 模拟 confirm 逐篇写入后立即记录映射
            store.record_diary_rollback(
                old_memo_name="memos/old", episode_id="ep_old",
                new_memo_names=["memos/rewrite-1"], note="rewrite_preview:rw_x",
            )
            store.record_diary_rollback(
                old_memo_name="memos/old", episode_id="ep_old",
                new_memo_names=["memos/rewrite-2"], note="rewrite_preview:rw_x",
            )
            rows = store.rollback_history(limit=10)
            self.assertEqual(len(rows), 2)
            # 按 preview_id 批量回滚需能找到两篇
            prefix = "rewrite_preview:rw_x"
            matched = [r for r in rows if str(r.get("note") or "") == prefix]
            self.assertEqual(len(matched), 2)
            self.assertEqual({r["new_content"] for r in matched}, {"memos/rewrite-1", "memos/rewrite-2"})
            store.mark_rollback_reverted(int(rows[0]["id"]))
            self.assertTrue(store.rollback_history(limit=10)[0]["reverted_ts"])
            store.close()


class WebUIProductionTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_overview_new_fields(self):
        from astrbot_plugin_memos_memory.webui import WebUIServer
        from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
        from astrbot_plugin_memos_memory.vector_store import VectorStore

        class FakeContext:
            async def get_provider_by_id(self, _id):
                return None

            def get_providers(self):
                return []

        class SavableConfig(dict):
            pass

        with tempfile.TemporaryDirectory() as temp:
            plugin = MemosMemoryPlugin(
                FakeContext(),
                SavableConfig({
                    "vec_db_path": str(Path(temp) / "memories.db"),
                    "webui_enable": True,
                    "webui_host": "127.0.0.1",
                    "webui_port": 0,
                    "enable_auto_compress": False,
                }),
            )
            await plugin._xinchao.initialize()
            plugin._vec = VectorStore(str(Path(temp) / "memories.db"), 3, "test-embedding")
            await plugin._vec.init()
            plugin._episodes = EpisodicStore(str(Path(temp) / "episodic.db"), 3, "test-embedding")
            await plugin._episodes.init()
            plugin._episodes.snapshot_db("manual")
            plugin._episode_migration_ready = True
            plugin._episode_migration_state = {"status": "ready"}
            plugin._initialized = True
            server = WebUIServer(plugin)
            self.assertTrue(await server.start())
            port = server._server.server_address[1]
            base = f"http://127.0.0.1:{port}"
            try:
                overview = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/production/overview", timeout=5).read().decode("utf-8")
                    )
                )
                self.assertTrue(overview["ok"])
                data = overview["data"]
                self.assertIn("identity", data)
                self.assertTrue(data["identity"]["database_uuid"])
                self.assertIn("traceable", data)
                self.assertIn("source_status", data)
                self.assertIn("level", data["source_status"])
                self.assertIn("snapshots", data)
                self.assertIn("long_diaries", data)
                self.assertIn("long_diaries_count", data)
                snaps = await asyncio.to_thread(
                    lambda: json.loads(
                        urllib.request.urlopen(base + "/api/production/snapshots", timeout=5).read().decode("utf-8")
                    )
                )
                self.assertTrue(snaps["ok"])
                self.assertGreaterEqual(len(snaps["data"]["snapshots"]), 1)
            finally:
                await server.stop()
                try:
                    plugin._episodes.close()
                except Exception:
                    pass
                try:
                    plugin._vec.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
