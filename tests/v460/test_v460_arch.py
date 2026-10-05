from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory import archive_guard
from astrbot_plugin_memos_memory.archive_guard import ArchiveGuard
from astrbot_plugin_memos_memory.diary_pipeline import DiaryPipeline, _lcs_rolling_hash
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.query_planner import QueryPlan, QueryPlanner
from astrbot_plugin_memos_memory.scene_splitter import SceneCandidate, SceneSplitter
from astrbot_plugin_memos_memory.source_archive import SourceArchive
from astrbot_plugin_memos_memory.traceability import compute_traceability
from astrbot_plugin_memos_memory.vector_generation import GenState, VectorGeneration


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "memory.db")
        self.store = EpisodicStore(self.db_path, None, None)
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def archive(self, messages=None):
        messages = messages or [
            {"role": "user", "content": "我答应明天去公园", "event_ts": 100.0, "event_timezone": "UTC"},
            {"role": "assistant", "content": "我会记住这个约定", "event_ts": 101.0, "event_timezone": "UTC"},
        ]
        return self.store.archive_batch("session-one", messages, "auto")

    def upsert_tiered_episode(self, batch_id):
        return self.store.upsert_episode(
            memo_name="2026-08-08.md",
            episode={
                "scene_start_turn": 0,
                "scene_end_turn": 1,
                "evidence": [
                    {"detail": "答应去公园", "tier": "must_write", "turn_indexes": [0], "grounded": True},
                    {"detail": "记住约定", "tier": "supporting", "turn_indexes": [1], "grounded": True},
                    {"detail": "仅供检索", "tier": "archive_only", "turn_indexes": [0, 1]},
                ],
            },
            card_text="公园约定",
            embedding=None,
            source_batch_id=batch_id,
            source_kind="auto",
            legacy=False,
            evidence_quality="source_grounded",
        )

    async def test_facade_schema_version(self):
        self.assertEqual(EpisodicStore.SCHEMA_VERSION, 15)
        self.assertEqual(SourceArchive.SCHEMA_VERSION, 6)
        row = self.store._connect().execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(row["value"], str(EpisodicStore.SCHEMA_VERSION))

    async def test_source_archive_preserves_complete_text_without_truncation(self):
        text = "开" + ("甲乙丙丁" * 6000) + "终"
        batch_id = self.archive([{"role": "user", "content": text, "event_ts": 1}])
        turns = self.store.source_turns(batch_id)
        self.assertEqual(turns[0]["content"], text)
        self.assertEqual(len(turns[0]["content"]), len(text))

    async def test_source_archive_adds_ordered_chunks_without_changing_turn(self):
        original = "一段必须保持不变的完整原文"
        batch_id = self.archive([{"role": "user", "content": original}])
        row = self.store._connect().execute(
            "SELECT id FROM source_turns WHERE batch_id=?", (batch_id,)
        ).fetchone()
        self.assertEqual(self.store.add_turn_chunks(row["id"], [" 第一 块 ", "第二\n块"]), 2)
        chunks = self.store.turn_chunks_for_turn(row["id"])
        self.assertEqual([x["chunk_text"] for x in chunks], ["第一 块", "第二 块"])
        self.assertEqual(self.store.source_turns(batch_id)[0]["content"], original)

    async def test_episode_repo_persists_all_evidence_tiers(self):
        batch_id = self.archive()
        self.upsert_tiered_episode(batch_id)
        evidence = self.store._eps.evidence_for_memo("2026-08-08.md", limit=10)
        self.assertEqual({item["tier"] for item in evidence}, {"must_write", "supporting", "archive_only"})
        self.assertEqual(self.store._eps.episode_evidence_count_by_tier(), {
            "must_write": 1, "supporting": 1, "archive_only": 1,
        })

    async def test_episode_repo_creates_exact_turn_links(self):
        batch_id = self.archive()
        episode_id = self.upsert_tiered_episode(batch_id)
        rows = self.store._connect().execute(
            "SELECT episode_id,batch_id,turn_index,evidence_index FROM episode_turn_links ORDER BY evidence_index,turn_index"
        ).fetchall()
        self.assertEqual([(r["episode_id"], r["batch_id"], r["turn_index"], r["evidence_index"]) for r in rows], [
            (episode_id, batch_id, 0, 0),
            (episode_id, batch_id, 1, 1),
            (episode_id, batch_id, 0, 2),
            (episode_id, batch_id, 1, 2),
        ])

    async def test_archive_guard_uuid_and_canonical_identity_are_stable(self):
        first = self.store.identity()
        self.store.guard.ensure_identity(self.store._connect(), 5)
        second = self.store.identity()
        self.assertEqual(first["database_uuid"], second["database_uuid"])
        self.assertTrue(first["database_uuid"])
        self.assertEqual(Path(first["canonical_db_path"]), Path(self.db_path).resolve())

    async def test_archive_guard_records_path_conflict(self):
        alternate = str(Path(self.tempdir.name) / "alias.db")
        guard = ArchiveGuard(alternate, self.store._connect, threading.RLock())
        guard.ensure_identity(self.store._connect(), 5)
        conflict = json.loads(self.store.identity()["path_conflict"])
        self.assertEqual(Path(conflict["canonical"]), Path(self.db_path).resolve())
        self.assertEqual(Path(conflict["current"]), Path(alternate).resolve())

    async def test_archive_guard_snapshot_and_self_check(self):
        self.archive()
        snapshot = self.store.snapshot_db("unit")
        self.assertTrue((Path(self.tempdir.name) / "snapshots" / snapshot["file_name"]).is_file())
        conn = self.store._connect()
        previous = {"batches": 9, "turns": 9, "episodes": 9, "turn_links": 9}
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_full_counts_before_check',?)",
                     (json.dumps(previous),))
        conn.commit()
        check = self.store.startup_self_check()
        self.assertEqual(len(check["issues"]), 4)
        self.assertIn("archive counts dropped", self.store.guard._get(conn, "startup_issue"))

    async def test_traceability_gray_red_yellow_and_green(self):
        class Counts:
            def __init__(self, values):
                self.values = values
            def counts(self):
                return self.values

        ep_zero = Counts({"episodes": 0})
        gray = compute_traceability(Counts({"turns": 0}), ep_zero)
        red = compute_traceability(Counts({"turns": 0}), Counts({"episodes": 1}))
        yellow = compute_traceability(Counts({"turns": 2, "turn_terms": 2}), ep_zero)
        green = compute_traceability(Counts({"turns": 2, "turn_terms": 2, "turn_vectors": 1}), ep_zero)
        self.assertEqual([x["status"]["level"] for x in (gray, red, yellow, green)],
                         ["gray", "red", "yellow", "green"])

    async def test_preview_partial_confirmation_retries_only_failed_item(self):
        preview = self.store.save_diary_preview({
            "preview_id": "preview_partial", "episode_id": "ep", "old_memo_name": "old.md",
            "payload": {"new_diaries": [{"content": "one"}, {"content": "two"}]},
        })
        attempts = []
        fail_once = {1}
        def apply_item(item, index):
            attempts.append(index)
            if index in fail_once:
                fail_once.remove(index)
                raise RuntimeError("temporary")
            return f"new-{index}.md"
        rollback_counter = iter((11, 12, 13))
        def rollback(*args):
            return next(rollback_counter)

        first = self.store.confirm_diary_preview(preview["preview_id"], apply_item, rollback)
        second = self.store.confirm_diary_preview(preview["preview_id"], apply_item, rollback)
        self.assertEqual((first["status"], first["succeeded"], first["failed"]), ("partial", 1, 1))
        self.assertEqual((second["status"], second["skipped"], second["failed"]), ("confirmed", 1, 0))
        self.assertEqual(attempts, [0, 1, 1])
        self.assertEqual([x["item_index"] for x in self.store.diary_preview_rollback_targets("preview_partial")], [0, 1])

    async def test_preview_atomic_rollback_mapping_does_not_commit_savepoint(self):
        preview = self.store.save_diary_preview({
            "preview_id": "preview_atomic", "episode_id": "ep", "old_memo_name": "old.md",
            "payload": {"new_diaries": [{"content": "one"}, {"content": "two"}]},
        })
        compensated = []
        def apply_item(item, index):
            return f"new-{index}.md"
        def atomic_rollback(old_name, episode_id, names, note):
            if names[0] == "new-1.md":
                raise RuntimeError("mapping failure")
            return self.store.record_diary_rollback_atomic(
                old_memo_name=old_name, episode_id=episode_id,
                new_memo_names=names, note=note,
            )
        result = self.store.confirm_diary_preview(
            preview["preview_id"], apply_item, atomic_rollback,
            lambda memo_name, index: compensated.append((memo_name, index)),
        )
        self.assertEqual((result["status"], result["succeeded"], result["failed"]),
                         ("partial", 1, 1))
        self.assertEqual(compensated, [("new-1.md", 1)])
        rows = self.store.rollback_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["memo_name"], "new-0.md")

    async def test_preview_prune_retains_pending_and_partial(self):
        previews = self.store._previews
        previews.keep = 1
        for name in ("terminal-old", "terminal-new", "pending", "partial"):
            previews.create({"preview_id": name, "episode_id": "ep", "payload": {"new_diaries": [name]}})
        previews.update_status("terminal-old", "confirmed", True)
        time.sleep(0.002)
        previews.update_status("terminal-new", "discarded")
        previews.update_status("partial", "partial")
        self.assertEqual(previews.prune(), 1)
        self.assertIsNone(previews.get("terminal-old"))
        self.assertIsNotNone(previews.get("terminal-new"))
        self.assertEqual(previews.get("pending")["status"], "pending")
        self.assertEqual(previews.get("partial")["status"], "partial")


class VectorGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "vectors.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.gen = VectorGeneration(False, lambda: self.conn)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_active_and_pending_generation_state_without_sqlite_vec(self):
        first = self.gen.prepare_active("model-a", 3)
        self.assertEqual(self.gen.current_state(), GenState.ACTIVE)
        self.assertEqual(first, self.gen.runtime_generation("model-a", 3))
        returned = self.gen.prepare_active("model-b", 4)
        self.assertEqual(returned, first)
        self.assertEqual(self.gen.current_state(), GenState.PREPARING)
        self.assertEqual(self.gen.pending_generation(), self.gen.runtime_generation("model-b", 4))

    def test_generation_owned_table_names_are_suffixed_and_kind_specific(self):
        generation = self.gen.prepare_active("model-a", 8)
        self.assertEqual(self.gen.table_name("source"), f"vec_source_turns_g{generation}")
        self.assertEqual(self.gen.table_name("card"), f"vec_episode_cards_g{generation}")
        self.assertEqual(self.gen.table_name("chunk"), f"vec_source_chunks_g{generation}")
        self.assertEqual(self.gen.table_name("unknown"), "")
        tables = self.conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'vec_%'").fetchall()
        self.assertEqual(tables, [])


class ArchiveLocationTests(unittest.TestCase):
    @staticmethod
    def make_db(path: Path, db_uuid: str, turns: int = 0, episodes: int = 0) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta(key,value) VALUES('database_uuid',?)", (db_uuid,))
            conn.execute("CREATE TABLE source_turns(id INTEGER PRIMARY KEY, content TEXT)")
            conn.execute("CREATE TABLE source_batches(batch_id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE episodes(id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO source_turns(content) VALUES(?)", [("turn",)] * turns)
            for _ in range(episodes):
                conn.execute("INSERT INTO episodes DEFAULT VALUES")
            conn.commit()
        finally:
            conn.close()

    def test_populated_configured_db_wins_over_empty_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = root / "legacy" / "episodic.db"
            canonical = root / "stable" / "episodic.db"
            configured.parent.mkdir()
            canonical.parent.mkdir()
            self.make_db(configured, "legacy-uuid", turns=4, episodes=2)
            self.make_db(canonical, "canonical-uuid")
            original = archive_guard.resolve_plugin_data_db
            old_cwd = Path.cwd()
            try:
                archive_guard.resolve_plugin_data_db = lambda _name: str(canonical)
                import os
                os.chdir(root)
                selected = archive_guard.migrate_episode_db_location("legacy/episodic.db")
            finally:
                os.chdir(old_cwd)
                archive_guard.resolve_plugin_data_db = original
            self.assertEqual(Path(selected), configured.resolve())

    def test_absolute_path_is_honored_before_database_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "operator-choice.db"
            self.assertEqual(
                Path(archive_guard.migrate_episode_db_location(str(target))),
                target.resolve(),
            )


class DiaryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = DiaryPipeline(SimpleNamespace())

    def test_transcript_risk_orders_verbatim_above_summary(self):
        raw = "他说我们明天去公园，然后我说好，接着他说一言为定。" * 20
        verbatim = raw[:350]
        summary = "我记得那天我们定下了同行的约定，这让我安心。"
        self.assertGreater(self.pipeline.transcript_risk(verbatim, raw),
                           self.pipeline.transcript_risk(summary, raw))
        report = self.pipeline.risk_report(summary, raw)
        self.assertAlmostEqual(
            report.compression_ratio,
            len(summary) / len(raw),
            places=4,
        )
        self.assertLess(report.compression_ratio, 1.0)

    def test_rolling_hash_risk_is_time_bounded_for_large_raw_text(self):
        content = ("我记得那个约定带来的安心。" * 120)[:2000]
        raw = content + ("无关的漫长原文内容" * 12000)
        started = time.perf_counter()
        report = self.pipeline.risk_report(content, raw)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0)
        self.assertGreaterEqual(report.longest_common_ratio, 0.9)

    def test_rolling_hash_finds_same_text_at_different_offsets(self):
        self.assertEqual(
            _lcs_rolling_hash("XYZabcdefUVW", "000abcdef999", cap_left=20, cap_right=20),
            6,
        )

    def test_paraphrased_stage_directions_still_look_like_a_transcript(self):
        raw = "那天我们在院子里谈起旧信，后来一起走过长廊。" * 100
        content = "我记得那天的旧信。" + (
            "（她抬手整理衣袖，又望着门外迟疑了一会儿。）"
            "我想把那时的心情留下来。"
        ) * 16
        report = self.pipeline.risk_report(content, raw)
        self.assertLess(report.source_copy_ratio, 0.1)
        self.assertGreater(report.stage_direction_ratio, 0.2)
        self.assertGreaterEqual(report.risk, 0.42)

    def test_tier_coverage_requires_must_and_excludes_archive_only(self):
        episode = {"evidence": [
            {"tier": "must_write", "detail": "一起去公园"},
            {"tier": "supporting", "detail": "天气很好"},
            {"tier": "archive_only", "detail": "秘密编号七七七"},
        ]}
        coverage, missing = self.pipeline.coverage("我记得一起去公园，天气很好。", episode)
        self.assertEqual((coverage, missing), (1.0, []))
        coverage, missing = self.pipeline.coverage("秘密编号七七七", episode)
        self.assertEqual(coverage, 0.0)
        self.assertEqual(missing, ["一起去公园"])

    def test_first_person_contract_accepts_memory_and_rejects_reports(self):
        self.assertTrue(self.pipeline.first_person_check("我记得那天的约定。 ").passed)
        self.assertEqual(self.pipeline.first_person_check("用户说要去公园。 ").reason,
                         "platform_role_subject")
        self.assertEqual(self.pipeline.first_person_check("她记下了那天的约定。 ").reason,
                         "third_person_report")

    def test_fallback_is_first_person_and_omits_archive_only(self):
        result = self.pipeline.fallback_diary({
            "occurred_at": "2026-08-08", "scene_anchor": "河边",
            "evidence": [
                {"tier": "must_write", "detail": "我们约好再见"},
                {"tier": "supporting", "detail": "支持细节"},
                {"tier": "archive_only", "detail": "隐藏原文"},
            ],
            "affect_before": "紧张", "affect_after": "安心", "long_effect": "我会记住这件事",
        })
        self.assertIn("我记得", result)
        self.assertIn("我们约好再见", result)
        self.assertNotIn("支持细节", result)
        self.assertNotIn("隐藏原文", result)

    def test_fallback_remains_first_person_when_metadata_is_third_person(self):
        result = self.pipeline.fallback_diary({
            "occurred_at": "2026-08-08", "scene_anchor": "天台",
            "evidence": [{"tier": "must_write", "detail": "银色钥匙放进盒子"}],
            "state_change": "关系变得更稳定", "long_effect": "以后会优先确认约定",
            "unresolved": ["钥匙是否还在"],
        })
        self.assertTrue(self.pipeline.first_person_check(result).passed)
        self.assertIn("我也记住了这份变化", result)

    def test_fallback_compacts_large_evidence_instead_of_copying_transcript(self):
        evidence = [
            {
                "tier": "must_write",
                "detail": f"对话中记录：第{index}项关键变化，关系与承诺得到确认。" + ("逐字原话" * 90),
                "quote": "这段引语不应在已有细节时重复追加",
            }
            for index in range(1, 17)
        ]
        raw = "\n".join(item["detail"] for item in evidence)
        result = self.pipeline.fallback_diary({
            "occurred_at": "2026-08-14",
            "scene_anchor": "深夜谈话",
            "evidence": evidence,
            "affect_before": "迟疑",
            "affect_after": "安定",
        })
        self.assertIn("第1项关键变化", result)
        self.assertNotIn("第9项关键变化", result)
        self.assertNotIn("这段引语不应在已有细节时重复追加", result)
        self.assertLess(len(result), 2200)
        self.assertLess(self.pipeline.risk_report(result, raw).risk, 0.42)


class QueryPlannerTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **overrides):
        values = {
            "query_plan_enable": True,
            "query_plan_context_window": 8,
            "query_plan_llm_disambiguate": True,
            "query_plan_llm_confidence_threshold": 0.5,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    async def test_self_contained_query_returns_none(self):
        planner = QueryPlanner(self.plugin())
        self.assertIsNone(planner.rewrite("请检索关于海边旅行计划的完整细节", []))

    async def test_temporal_query_does_not_use_context(self):
        planner = QueryPlanner(self.plugin())
        plan = planner.rewrite("去年生日是什么时候", [{"content": "我们谈过蛋糕"}])
        self.assertEqual(plan.intent, "temporal")
        self.assertFalse(plan.use_context)
        self.assertEqual(plan.context_used_reason, "temporal_query")

    async def test_explicit_calendar_date_uses_shared_temporal_parser(self):
        plugin = self.plugin()
        plugin._request_now = lambda: __import__("datetime").datetime(2026, 8, 9, 12, 0)
        planner = QueryPlanner(plugin)
        plan = planner.rewrite("2025年8月3日那天发生了什么", [{"content": "无关上下文"}])
        self.assertEqual(plan.intent, "temporal")
        self.assertFalse(plan.use_context)
        self.assertTrue(plan.temporal_constraints)

    async def test_ambiguous_reference_records_reason(self):
        planner = QueryPlanner(self.plugin())
        plan = planner.rewrite("那件事后来怎么了", [{"content": "阿青答应去海边"}])
        self.assertEqual(plan.intent, "narrative")
        self.assertTrue(plan.use_context)
        self.assertEqual(plan.context_used_reason, "narrative_entities")
        contextual = planner.rewrite("那件事呢", [{"content": "阿青答应去海边"}])
        self.assertEqual(contextual.context_used_reason, "ambiguous")

    async def test_context_anchors_are_words_not_overlapping_bigrams(self):
        planner = QueryPlanner(self.plugin(character_name="爱莉"))
        plan = planner.rewrite(
            "那件事呢",
            [{"content": "爱莉和我在天台聊了很久，那枚银色钥匙后来放进了盒子。"}],
        )
        self.assertIn("天台", plan.resolved_entities)
        self.assertIn("钥匙", plan.resolved_entities)
        for bad in ("台聊", "聊了", "枚银", "色钥"):
            self.assertNotIn(bad, plan.resolved_entities)

    async def test_narrowed_llm_gate_skips_faceted_plan_and_rewrites_anchorless_specific(self):
        planner = QueryPlanner(self.plugin())
        calls = []
        async def caller(prompt):
            calls.append(prompt)
            return json.dumps({"standalone_query": "海边约定", "resolved_entities": ["阿青"], "confidence": 0.9})
        builder = lambda query, context: query + "|" + context
        faceted = QueryPlan("specific", "短问句", False, "specific_query", 0.1,
                            resolved_entities=["阿青"], raw_standalone="短问句")
        self.assertIs(await planner.maybe_llm_disambiguate(faceted, "ctx", caller, builder), faceted)
        self.assertEqual(calls, [])
        anchorless = QueryPlan("specific", "短问句", False, "specific_query", 0.1,
                               raw_standalone="短问句")
        rewritten = await planner.maybe_llm_disambiguate(anchorless, "ctx", caller, builder)
        self.assertEqual(calls, ["短问句|ctx"])
        self.assertTrue(rewritten.rewritten)
        self.assertEqual(rewritten.search_text, "海边约定")
        self.assertEqual(rewritten.context_used_reason, "specific_query_llm_disambiguated")


class SceneSplitterTests(unittest.TestCase):
    def test_first_scene_always_starts_at_turn_zero(self):
        scenes = SceneSplitter().detect([{"content": "你好"}, {"content": "晚安"}])
        self.assertEqual(scenes[0].start_turn, 0)

    def test_date_change_and_time_gap_attach_to_new_scene(self):
        messages = [
            {"content": "旧场景仍在继续", "event_ts": 100.0, "event_timezone": "UTC"},
            {"content": "新场景完全不同", "event_ts": 90000.0, "event_timezone": "UTC"},
        ]
        scenes = SceneSplitter(gap_seconds=3600).detect(messages)
        self.assertEqual((scenes[1].start_turn, scenes[1].end_turn), (1, 1))
        self.assertIn("date_change", scenes[1].reasons)
        self.assertIn("time_gap", scenes[1].reasons)

    def test_detect_produces_nonoverlapping_full_coverage(self):
        messages = [{"content": f"主题内容第{i}段", "event_ts": i * 20000.0} for i in range(7)]
        scenes = SceneSplitter(gap_seconds=1000, max_scenes=4).detect(messages)
        covered = [turn for scene in scenes for turn in range(scene.start_turn, scene.end_turn + 1)]
        self.assertEqual(covered, list(range(len(messages))))
        self.assertTrue(all(a.end_turn < b.start_turn for a, b in zip(scenes, scenes[1:])))

    def test_validate_reports_large_overlap_and_uncovered_turns(self):
        messages = [{"content": str(i)} for i in range(6)]
        episodes = [{"scene_start_turn": 0, "scene_end_turn": 3},
                    {"scene_start_turn": 1, "scene_end_turn": 3}]
        result = SceneSplitter().validate(
            episodes, messages, [SceneCandidate(0, 3), SceneCandidate(4, 5)]
        )
        self.assertEqual(result["uncovered"], [4, 5])
        self.assertEqual(result["overlaps"][0]["turns"], 3)  # [1,3] 共 3 个闭区间轮次
        self.assertTrue(result["overlaps"][0]["large"])


if __name__ == "__main__":
    unittest.main()
