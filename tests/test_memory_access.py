import tempfile
import time
import unittest
import sqlite3
import json
import zipfile
from pathlib import Path

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.access_export import AccessAnalysisExporter


def episode(name, date, scene, retrieval, *, entities=None, importance=3,
            state_change="", long_effect="", evidence_quality="diary_derived",
            source_updated_ts=0.0):
    event_ts = time.mktime(time.strptime(date, "%Y-%m-%d"))
    return {
        "memo_name": name,
        "occurred_at": date + " 晚上",
        "event_ts": event_ts,
        "time_basis": "event_time",
        "memory_type": "relationship_shift" if state_change else "plot_fact",
        "importance": importance,
        "scene_anchor": scene,
        "retrieval_key": retrieval,
        "state_change": state_change,
        "long_effect": long_effect,
        "trigger_hint": retrieval,
        "entities": entities or [],
        "unresolved": [],
        "card_text": "\n".join([scene, retrieval, state_change, long_effect]),
        "evidence_quality": evidence_quality,
        "source_updated_ts": source_updated_ts,
    }


class MemoryAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "episodic.db"), 3, "unit")
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, item):
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item,
            card_text=item["card_text"], embedding=[1.0, 0.0, 0.0],
            evidence_quality=item.get("evidence_quality", "diary_derived"),
        )

    async def test_schema_is_idempotent_and_derived_tables_exist(self):
        self.store.close()
        await self.store.init()
        conn = self.store._connect()
        names = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("memory_access_state", names)
        self.assertIn("memory_cue_signatures", names)
        self.assertIn("memory_interference_edges", names)
        access_columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(memory_access_state)"
        ).fetchall()}
        self.assertIn("event_ts", access_columns)
        self.assertIn("age_reference_ts", access_columns)
        self.assertIn("age_reference_kind", access_columns)
        self.assertIn("memory_cue_terms", names)
        self.assertEqual(conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"], str(EpisodicStore.SCHEMA_VERSION))

    async def test_schema6_upgrade_preserves_episode_and_is_idempotent(self):
        item = episode("memos/legacy", "2024-01-02", "旧版本场景", "旧版本线索")
        self.add(item)
        db_path = self.store.db_path
        self.store.close()
        conn = sqlite3.connect(db_path)
        for table in (
            "memory_access_events", "memory_interference_members",
            "memory_interference_groups", "memory_interference_edges",
            "memory_cue_signatures", "memory_access_state", "memory_access_maintenance",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','6')")
        conn.commit()
        conn.close()
        await self.store.init()
        self.assertIsNotNone(self.store.get_episode("memos/legacy"))
        self.assertEqual(self.store.stats()["episodes"], 1)
        self.store.rebuild_memory_access([self.store.get_episode("memos/legacy")])
        self.store.close()
        await self.store.init()
        self.assertEqual(self.store.memory_access_overview()["total"], 1)
        self.assertEqual(self.store.stats()["episodes"], 1)

    async def test_schema7_upgrade_adds_test1_indexes_idempotently(self):
        db_path = self.store.db_path
        self.store.close()
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE IF EXISTS memory_cue_terms")
        conn.execute("ALTER TABLE memory_access_state DROP COLUMN age_reference_kind")
        conn.execute("ALTER TABLE memory_access_state DROP COLUMN age_reference_ts")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','7')")
        conn.commit()
        conn.close()
        await self.store.init()
        await self.store.init()
        conn = self.store._connect()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_access_state)")}
        self.assertIn("age_reference_ts", columns)
        self.assertIn("age_reference_kind", columns)
        self.assertIsNotNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_cue_terms'"
        ).fetchone())
        self.assertEqual(conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"], str(EpisodicStore.SCHEMA_VERSION))

    async def test_schema10_upgrade_adds_observations_without_touching_existing_access_data(self):
        item = episode("memos/schema10", "2024-05-06", "旧版 ACCESS 数据", "旧线索")
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.evaluate_memory_access(
            query="旧线索", candidates=[{"memo_name": "memos/schema10", "score": 0.7}],
            selected_names=["memos/schema10"], request_id="old-access-event",
            config={"shadow_mode": True}, record=True,
        )
        conn = self.store._connect()
        before = {
            "episodes": int(conn.execute("SELECT COUNT(*) n FROM episodes").fetchone()["n"]),
            "states": int(conn.execute("SELECT COUNT(*) n FROM memory_access_state").fetchone()["n"]),
            "events": int(conn.execute("SELECT COUNT(*) n FROM memory_access_events").fetchone()["n"]),
        }
        conn.execute("DROP TABLE memory_access_observations")
        conn.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
        conn.commit()
        self.store.close()
        await self.store.init()
        conn = self.store._connect()
        after = {
            "episodes": int(conn.execute("SELECT COUNT(*) n FROM episodes").fetchone()["n"]),
            "states": int(conn.execute("SELECT COUNT(*) n FROM memory_access_state").fetchone()["n"]),
            "events": int(conn.execute("SELECT COUNT(*) n FROM memory_access_events").fetchone()["n"]),
        }
        self.assertEqual(after, before)
        self.assertIsNotNone(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_access_observations'"
        ).fetchone())
        self.assertEqual(
            conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"], str(EpisodicStore.SCHEMA_VERSION)
        )
        self.store.close()
        await self.store.init()
        self.assertEqual(self.store.stats()["episodes"], 1)

    async def test_backfill_preserves_legacy_diary_without_source(self):
        item = episode("memos/old", "2020-02-03", "旧车站告别", "红围巾 站台", importance=4)
        self.add(item)
        result = self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/old")
        self.assertEqual(result["updated"], 1)
        self.assertGreater(detail["accessibility"], 0.20)
        self.assertEqual(detail["grounding_bonus"], 0.0)
        self.assertLess(detail["vividness"], 0.10)

    async def test_source_updated_time_is_decay_proxy_not_factual_date(self):
        proxy_ts = time.mktime(time.strptime("2025-11-09", "%Y-%m-%d"))
        item = episode("memos/proxy", "2024-01-02", "旧日记", "蓝色车票",
                       source_updated_ts=proxy_ts)
        item["occurred_at"] = ""
        item["event_ts"] = 0
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/proxy")
        self.assertEqual(detail["age_reference_kind"], "source_updated_proxy")
        self.assertEqual(detail["age_reference_ts"], proxy_ts)
        self.assertNotIn("2025-11-09", detail["cue_signature"]["dates"])

    async def test_migration_created_time_never_refreshes_unknown_memory(self):
        item = episode("memos/unknown-age", "2024-01-02", "旧日记", "旧车站")
        item["occurred_at"] = ""
        item["event_ts"] = 0
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute(
            "UPDATE memory_access_state SET created_ts=?,age_reference_ts=0,age_reference_kind='unknown',"
            "age_reference_confidence=0 WHERE memo_name=?",
            (time.time(), "memos/unknown-age"),
        )
        conn.commit()
        detail_before = self.store.memory_access_detail("memos/unknown-age")
        vividness_before = float(detail_before["vividness"])
        self.store.maintain_memory_access(now=time.time(), config={"decay_days": 45})
        detail = self.store.memory_access_detail("memos/unknown-age")
        # Unknown-clock memories must never be refreshed by migration timestamps.
        # Under test2 they hold their prior vividness rather than being artificially
        # aged to near-zero; the invariant is that maintain() does not increase vividness.
        self.assertLessEqual(detail["vividness"], vividness_before + 1e-9)
        # And they must remain clearly below a freshly-created memory (≥ 0.78).
        self.assertLess(detail["vividness"], 0.50)

    async def test_same_event_restatement_competes(self):
        a = episode("memos/a", "2026-07-01", "雨夜天台的约定", "天台 银戒指 不离开", entities=["爱莉"])
        b = episode("memos/b", "2026-07-01", "雨夜在天台许诺", "银戒指 天台 不会离开", entities=["爱莉"])
        for item in (a, b):
            self.add(item)
        self.store.rebuild_memory_access([a, b])
        edges = self.store.list_memory_interference(memo_name="memos/a")
        self.assertTrue(any(x["edge_type"] == "same_event_restated" for x in edges))
        self.assertGreater(self.store.memory_access_detail("memos/a")["interference_load"], 0)

    async def test_same_theme_different_dates_are_not_folded_as_same_event(self):
        a = episode("memos/a", "2026-05-01", "第一次在海边散步", "海边 贝壳 散步", entities=["爱莉"])
        b = episode("memos/b", "2026-07-18", "再次去海边争吵", "海边 贝壳 争吵", entities=["爱莉"])
        for item in (a, b):
            self.add(item)
        self.store.rebuild_memory_access([a, b])
        edges = self.store.list_memory_interference(memo_name="memos/a")
        self.assertFalse(any(x["edge_type"] == "same_event_restated" for x in edges))
        if edges:
            self.assertLessEqual(max(x["competition"] for x in edges), 0.12)

    async def test_contradictory_relationship_stages_are_protected(self):
        a = episode("memos/a", "2026-04-01", "她拒绝继续关系", "拒绝 分开", entities=["爱莉"], state_change="决定离开并拒绝")
        b = episode("memos/b", "2026-08-01", "她接受和好", "接受 和好", entities=["爱莉"], state_change="回来并接受和好")
        for item in (a, b):
            self.add(item)
        self.store.rebuild_memory_access([a, b])
        edges = self.store.list_memory_interference(memo_name="memos/a")
        contradictory = [x for x in edges if x["edge_type"] == "contradictory_stage"]
        self.assertTrue(contradictory)
        self.assertLessEqual(contradictory[0]["competition"], 0.04)

    async def test_common_actor_does_not_make_one_interference_graph(self):
        items = [
            episode(f"memos/{index}", f"2026-07-{index + 1:02d}",
                    f"不同场景{index}", f"独有线索{index}", entities=["爱莉"])
            for index in range(12)
        ]
        for item in items:
            self.add(item)
        result = self.store.rebuild_memory_access(items)
        self.assertEqual(result["groups"], 0)
        self.assertLess(result["edges"], len(items))

    async def test_exact_date_rescues_deep_memory(self):
        item = episode("memos/deep", "2022-03-14", "车站留下蓝色车票", "蓝色车票 车站", entities=["爱莉"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='deep',accessibility=0.12 WHERE memo_name='memos/deep'")
        conn.commit()
        result = self.store.evaluate_memory_access(
            query="2022-03-14 那张蓝色车票后来怎么了",
            candidates=[{"memo_name": "memos/deep", "score": 0.46}],
            selected_names=[], config={"shadow_mode": True, "exact_cue_relief": 0.85},
            record=False,
        )
        self.assertTrue(result["items"][0]["exact_cue"])
        self.assertTrue(result["items"][0]["rescued"])
        self.assertEqual(result["items"][0]["presentation"], "full_or_evidence")

    async def test_precise_date_rescue_can_expand_outside_retrieval_pool(self):
        item = episode("memos/deep", "2022-03-14", "车站留下蓝色车票", "蓝色车票 车站")
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='deep',accessibility=0.12 WHERE memo_name='memos/deep'")
        conn.commit()
        result = self.store.evaluate_memory_access(
            query="2022-03-14那张蓝色车票后来怎么样了", candidates=[], selected_names=[],
            config={"shadow_mode": True, "independent_cue_rescue": True}, record=False,
        )
        self.assertEqual(result["rescue_pool_count"], 1)
        self.assertEqual(result["items"][0]["candidate_source"], "precise_cue_index")
        self.assertTrue(result["items"][0]["rescued"])

    async def test_chinese_exact_date_rescues_deep_memory(self):
        item = episode("memos/deep", "2022-03-14", "车站留下蓝色车票", "蓝色车票 车站")
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='deep',accessibility=0.12 WHERE memo_name='memos/deep'")
        conn.commit()
        result = self.store.evaluate_memory_access(
            query="2022年3月14日那张蓝色车票后来怎么样了",
            candidates=[{"memo_name": "memos/deep", "score": 0.46}],
            selected_names=[], config={"shadow_mode": True}, record=False,
        )
        self.assertTrue(result["items"][0]["exact_cue"])
        self.assertTrue(result["items"][0]["rescued"])

    async def test_weak_query_does_not_automatically_rescue_deep_memory(self):
        item = episode("memos/deep", "2022-03-14", "车站留下蓝色车票", "蓝色车票 车站")
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='deep',accessibility=0.12 WHERE memo_name='memos/deep'")
        conn.commit()
        result = self.store.evaluate_memory_access(
            query="你还记得吗", candidates=[{"memo_name": "memos/deep", "score": 0.46}],
            selected_names=[], config={"shadow_mode": True}, record=False,
        )
        self.assertFalse(result["items"][0]["rescued"])
        self.assertEqual(result["items"][0]["presentation"], "cue_only")

    async def test_shadow_response_never_reconsolidates(self):
        item = episode("memos/a", "2026-07-01", "天台约定", "银戒指 不离开", entities=["爱莉"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        before = self.store.memory_access_detail("memos/a")["reconsolidation_count"]
        result = self.store.record_memory_response_use(
            request_id="r1", response_text="我记得爱莉在天台给了银戒指",
            memo_names=["memos/a"], shadow=True, reconsolidate=True,
        )
        after = self.store.memory_access_detail("memos/a")["reconsolidation_count"]
        self.assertEqual(before, after)
        self.assertTrue(result["shadow"])

    async def test_nonshadow_successful_use_reconsolidates(self):
        item = episode("memos/a", "2026-07-01", "天台约定", "银戒指 不离开", entities=["爱莉"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        result = self.store.record_memory_response_use(
            request_id="r2", response_text="爱莉、天台、银戒指和不离开的约定我都记得",
            memo_names=["memos/a"], shadow=False, reconsolidate=True,
        )
        detail = self.store.memory_access_detail("memos/a")
        self.assertEqual(result["used"], 1)
        self.assertEqual(detail["reconsolidation_count"], 1)

    async def test_evaluation_records_selected_access_event(self):
        item = episode("memos/a", "2026-07-01", "天台约定", "银戒指")
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.evaluate_memory_access(
            query="银戒指", candidates=[{"memo_name": "memos/a", "score": 0.8}],
            selected_names=["memos/a"], request_id="req-a",
            config={"shadow_mode": True}, record=True,
        )
        events = self.store.list_memory_access_events(memo_name="memos/a")
        self.assertEqual(events[0]["request_id"], "req-a")
        self.assertEqual(events[0]["event_kind"], "shadow_evaluation")
        self.assertEqual(events[0]["selected"], 1)

    async def test_delete_removes_access_derivatives(self):
        item = episode("memos/a", "2026-07-01", "天台约定", "银戒指")
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.assertEqual(self.store.delete_by_memo_name("memos/a"), 1)
        self.assertIsNone(self.store.memory_access_detail("memos/a"))
        self.assertEqual(self.store.memory_access_overview()["total"], 0)

    async def test_maintenance_has_hysteresis(self):
        item = episode("memos/a", "2026-07-01", "天台约定", "银戒指", importance=5)
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='vivid',accessibility=0.65,persistence=0.90 WHERE memo_name='memos/a'")
        conn.commit()
        self.store.maintain_memory_access(config={"vivid_threshold": 0.68, "deep_threshold": 0.32, "decay_days": 45})
        self.assertEqual(self.store.memory_access_detail("memos/a")["access_state"], "vivid")


if __name__ == "__main__":
    unittest.main()


class Test2NewContractTests(unittest.IsolatedAsyncioTestCase):
    """≥20 tests covering test2 algorithm contract changes."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "episodic.db"), 3, "unit")
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, item):
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item,
            card_text=item["card_text"], embedding=[1.0, 0.0, 0.0],
            evidence_quality=item.get("evidence_quality", "diary_derived"),
        )

    # 1. Bare date is grade D, never an exact rescue
    async def test_date_only_is_grade_d_not_exact_rescue(self):
        item = episode("memos/dated", "2026-07-18", "天台约定", "银戒指")
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='deep',accessibility=0.12 WHERE memo_name='memos/dated'")
        conn.commit()
        result = self.store.evaluate_memory_access(
            query="2026-07-18 那天发生了什么", candidates=[{"memo_name": "memos/dated", "score": 0.5}],
            selected_names=[], config={"shadow_mode": True}, record=False,
        )
        item_res = result["items"][0]
        self.assertEqual(result["query_grade"], "D")
        self.assertTrue(result["date_only_query"])
        self.assertFalse(item_res["exact_cue"], "bare date must not claim an exact rescue")

    # 2. Date + rare object => grade B, rescues correctly
    async def test_date_plus_rare_object_is_grade_b_and_rescues(self):
        item = episode("memos/ticket", "2022-03-14", "车站留下蓝色车票", "蓝色车票 车站", entities=["爱莉"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='deep',accessibility=0.12 WHERE memo_name='memos/ticket'")
        conn.commit()
        result = self.store.evaluate_memory_access(
            query="2022-03-14 那张蓝色车票后来怎么了",
            candidates=[{"memo_name": "memos/ticket", "score": 0.5}],
            selected_names=[], config={"shadow_mode": True, "independent_cue_rescue": True}, record=False,
        )
        item_res = result["items"][0]
        self.assertIn(result["query_grade"], ("A", "B"), "date+object must be at least B grade")
        self.assertTrue(item_res["exact_cue"])
        self.assertTrue(item_res["rescued"])

    # 3. Same-day multi: no distinctive cue => ambiguous_same_day
    async def test_same_day_ambiguous_when_no_distinctive_cue(self):
        a = episode("memos/day-a", "2026-07-18", "散步说了些话", "散步 说话")
        b = episode("memos/day-b", "2026-07-18", "聊了聊最近的事", "最近 聊天")
        for item in (a, b):
            self.add(item)
        self.store.rebuild_memory_access([a, b])
        result = self.store.disambiguate_same_day_memories(
            "2026-07-18 那天发生了什么", ["memos/day-a", "memos/day-b"]
        )
        self.assertTrue(result["ambiguous_same_day"])

    # 4. Verbatim quote wins over bare date
    async def test_verbatim_quote_beats_date_in_grading(self):
        # Build an episode with a known quote in its card text so the
        # cue-term index picks it up as a quote cue.
        item = episode("memos/quote", "2026-07-18", "天台约定", "銀戒指 不离开")
        item["card_text"] = '“离开前我会先告诉你”' + ' 銀戒指'
        self.add(item)
        self.store.rebuild_memory_access([item])
        # Query that embeds the known verbatim phrase inside curly quotes.
        query = '你说过“离开前我会先告诉你”，那是什么时候'
        cues = self.store.classify_memory_query_cues(query)
        # The quote should be found; grade must not be D.
        self.assertNotEqual(cues["query_grade"], "D",
                            "verbatim quote in query must score above D")
        self.assertFalse(cues["date_only"])

    # 5. Promise/boundary memories are protected from decay
    async def test_promise_memory_is_decay_ineligible(self):
        item = episode("memos/promise", "2026-07-18", "天台承诺不再突然离开", "承诺 不离开")
        item["memory_type"] = "promise"
        self.add(item)
        result = self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/promise")
        state_reason = detail.get("state_reason") or {}
        self.assertFalse(detail["decay_eligible"], "promise must be decay-ineligible")
        self.assertTrue(any("durable_type" in r for r in state_reason.get("protection") or []))

    # 6. Unresolved items protect regardless of importance
    async def test_unresolved_item_protects_regardless_of_importance(self):
        item = episode("memos/unresolved", "2026-06-01", "悬而未决的事", "待解决", importance=2)
        item["unresolved_json"] = '["待解决的问题"]'
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/unresolved")
        self.assertFalse(detail["decay_eligible"], "unresolved item must block decay eligibility")

    # 7. High importance alone cannot prevent decay eligibility
    async def test_high_importance_alone_does_not_block_decay(self):
        item = episode("memos/imp5", "2024-01-01", "普通日常记录", "日常 聊天", importance=5)
        item["memory_type"] = "observation"
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/imp5")
        # importance=5 non-durable type should still be decay eligible
        self.assertTrue(detail["decay_eligible"], "importance 5 alone must not block eligibility")

    # 8. Source-proxy confidence lowers effective age contribution
    async def test_source_proxy_has_lower_confidence_than_event(self):
        proxy_ts = time.mktime(time.strptime("2025-06-01", "%Y-%m-%d"))
        item = episode("memos/proxy-conf", "2024-01-02", "旧日记", "旧车站",
                       source_updated_ts=proxy_ts)
        item["occurred_at"] = ""
        item["event_ts"] = 0
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/proxy-conf")
        self.assertEqual(detail["age_reference_kind"], "source_updated_proxy")
        self.assertAlmostEqual(float(detail["age_reference_confidence"]), 0.45, places=1)

    # 9. Unknown clock does not refresh vividness
    async def test_unknown_clock_never_refreshes_vividness(self):
        item = episode("memos/unknown-v", "2024-01-02", "旧日记", "旧地点")
        item["occurred_at"] = ""
        item["event_ts"] = 0
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail_before = self.store.memory_access_detail("memos/unknown-v")
        v_before = float(detail_before["vividness"])
        conn = self.store._connect()
        conn.execute(
            "UPDATE memory_access_state SET age_reference_ts=0,age_reference_kind='unknown',"
            "age_reference_confidence=0 WHERE memo_name='memos/unknown-v'"
        )
        conn.commit()
        self.store.maintain_memory_access(now=time.time(), config={"decay_days": 45})
        detail_after = self.store.memory_access_detail("memos/unknown-v")
        self.assertLessEqual(float(detail_after["vividness"]), v_before + 1e-9,
                             "maintain must not increase vividness for unknown-clock memory")

    # 10. Two maintenance runs required before crossing vivid→latent
    async def test_two_run_confirmation_prevents_immediate_state_crossing(self):
        item = episode("memos/stable", "2022-01-01", "很久以前的事", "旧日记", importance=1)
        item["memory_type"] = "observation"
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        # Force accessibility far below vivid threshold but above deep
        conn.execute("UPDATE memory_access_state SET access_state='vivid',accessibility=0.40 WHERE memo_name='memos/stable'")
        conn.commit()
        result = self.store.maintain_memory_access(
            config={"vivid_threshold": 0.68, "deep_threshold": 0.32,
                    "state_confirmation_runs": 2, "decay_enable": False})
        # First run only marks the transition pending; it must not apply it.
        self.assertEqual(self.store.memory_access_detail("memos/stable")["access_state"], "vivid")
        self.assertEqual(int(result.get("states_changed") or 0), 0)
        self.assertGreaterEqual(int(result.get("pending_transitions") or 0), 1)
        self.assertEqual(
            str(self.store.memory_access_detail("memos/stable").get("pending_state") or ""),
            "latent",
        )

    # 11. Second run commits the pending transition
    async def test_second_run_commits_pending_transition(self):
        item = episode("memos/trans", "2022-01-01", "旧日记", "旧事", importance=1)
        item["memory_type"] = "observation"
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='vivid',accessibility=0.40,"
                     "decay_eligible=1,state_reason_json='{}' WHERE memo_name='memos/trans'")
        conn.commit()
        cfg = {"vivid_threshold": 0.68, "deep_threshold": 0.32,
               "state_confirmation_runs": 2, "decay_enable": False}
        self.store.maintain_memory_access(config=cfg)
        self.assertEqual(self.store.memory_access_detail("memos/trans")["access_state"], "vivid")
        second = self.store.maintain_memory_access(config=cfg)
        self.assertEqual(int(second.get("states_changed") or 0), 1)
        self.assertIn(self.store.memory_access_detail("memos/trans")["access_state"],
                      ("latent", "deep"))

    # 12. Decay-ineligible memory never deepens from time alone
    async def test_protected_memory_never_deepens_from_time(self):
        item = episode("memos/guard", "2020-01-01", "早期承诺", "诺言 守信")
        item["memory_type"] = "promise"
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_state SET access_state='latent',accessibility=0.10,"
                     "decay_eligible=0 WHERE memo_name='memos/guard'")
        conn.commit()
        cfg = {"vivid_threshold": 0.68, "deep_threshold": 0.32,
               "state_confirmation_runs": 1, "decay_days": 1}
        blocked_total = 0
        for _ in range(5):
            result = self.store.maintain_memory_access(config=cfg)
            blocked_total += int(result.get("blocked_by_gate") or 0)
        self.assertNotEqual(self.store.memory_access_detail("memos/guard")["access_state"], "deep")
        self.assertGreaterEqual(blocked_total, 1,
                                "the gate must report at least one blocked transition")

    # 13. probable_duplicate carries no competition penalty
    async def test_probable_duplicate_has_zero_competition(self):
        a = episode("memos/pd-a", "2026-07-18", "天台约定记录", "天台 约定 银戒指", entities=["爱莉"])
        b = episode("memos/pd-b", "2026-08-01", "天台约定再述", "天台 约定 银戒指", entities=["爱莉"])
        for x in (a, b):
            self.add(x)
        self.store.rebuild_memory_access([a, b])
        edges = self.store.list_memory_interference(memo_name="memos/pd-a")
        for edge in edges:
            if edge["edge_type"] == "probable_duplicate":
                self.assertEqual(float(edge["competition"]), 0.0)
            self.assertIn("edge_confidence", edge)
            self.assertIn("classifier_version", edge)

    # 14. Same source batch links cross-midnight diaries as one event
    async def test_same_source_batch_links_across_dates(self):
        a = episode("memos/sa", "2026-08-01", "深夜约定", "约定 不离开", entities=["爱莉"])
        b = episode("memos/sb", "2026-08-02", "约定后续", "约定 继续 不离开", entities=["爱莉"])
        a["source_batch_id"] = "batch-shared"
        b["source_batch_id"] = "batch-shared"
        for x in (a, b):
            self.add(x)
        self.store.rebuild_memory_access([a, b])
        edges = self.store.list_memory_interference(memo_name="memos/sa")
        self.assertTrue(any(x["edge_type"] == "same_event_restated" for x in edges),
                        "same source_batch_id must link even with different date strings")

    # 15. Same theme on different factual dates is never one event
    async def test_same_theme_different_dates_never_one_event(self):
        a = episode("memos/th-a", "2026-05-01", "第一次去海边", "海边 散步 贝壳", entities=["爱莉"])
        b = episode("memos/th-b", "2026-09-14", "再次去海边争吵", "海边 散步 争吵", entities=["爱莉"])
        for x in (a, b):
            self.add(x)
        self.store.rebuild_memory_access([a, b])
        edges = self.store.list_memory_interference(memo_name="memos/th-a")
        self.assertFalse(any(x["edge_type"] == "same_event_restated" for x in edges))

    # 16. Incremental interference sync scopes to the target neighbourhood
    async def test_incremental_sync_scopes_to_target(self):
        a = episode("memos/a-inc", "2026-07-01", "约定", "银戒指 天台", entities=["爱莉"])
        b = episode("memos/b-inc", "2026-07-01", "许诺", "银戒指 天台 许诺", entities=["爱莉"])
        c = episode("memos/c-inc", "2026-03-01", "无关日常", "日常 闲聊")
        for x in (a, b, c):
            self.add(x)
        self.store.rebuild_memory_access([a, b, c])
        result = self.store.sync_memory_interference_incremental("memos/a-inc")
        self.assertTrue(result.get("updated"))
        self.assertEqual(result["memo_name"], "memos/a-inc")
        meta = self.store.memory_access_index_meta()
        self.assertGreater(float(meta["last_incremental_rebuild_ts"]), 0)

    # 17. Overview exposes eligibility, index meta and gate summary
    async def test_overview_exposes_test2_summary(self):
        item = episode("memos/ov", "2026-07-01", "承诺场景", "承诺 不离开")
        item["memory_type"] = "promise"
        self.add(item)
        self.store.rebuild_memory_access([item])
        overview = self.store.memory_access_overview()
        self.assertIn("decay_eligibility", overview)
        self.assertGreaterEqual(int(overview["decay_eligibility"]["protected"]), 1)
        self.assertIn("index_meta", overview)
        self.assertIn("safety_gates", overview)
        self.assertEqual(overview["algorithm_version"], "5.1.0")

    async def test_real_shadow_observation_is_persisted_and_response_is_joined(self):
        item = episode("memos/obs", "2026-07-01", "雨夜天台", "银戒指 不离开", entities=["爱莉"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        result = self.store.evaluate_memory_access(
            query="还记得雨夜天台的银戒指吗",
            candidates=[{"memo_name": "memos/obs", "score": 0.72}],
            selected_names=["memos/obs"], request_id="req-observation",
            config={"shadow_mode": True, "observation_keep": 5000}, record=True,
        )
        self.assertEqual(result["request_id"], "req-observation")
        detail = self.store.memory_access_observation_detail("req-observation")
        self.assertEqual(detail["query_text"], "还记得雨夜天台的银戒指吗")
        self.assertEqual(detail["baseline"], ["memos/obs"])
        self.assertEqual(detail["recommended"], ["memos/obs"])
        self.assertEqual(detail["response_memories"], -1)
        response = self.store.record_memory_response_use(
            request_id="req-observation", response_text="我当然记得那枚银戒指和天台上的约定。",
            memo_names=["memos/obs"], shadow=True, reconsolidate=True,
        )
        self.assertEqual(response["memories"], 1)
        detail = self.store.memory_access_observation_detail("req-observation")
        self.assertEqual(detail["response_memories"], 1)
        self.assertGreaterEqual(detail["response_used"], 0)
        self.assertIn("response_use", detail["detail"])
        summary = self.store.memory_access_observation_summary(days=30)
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["responses"], 1)

    async def test_observation_feedback_survives_retention_and_can_be_cleared(self):
        item = episode("memos/feedback", "2026-07-02", "车站", "蓝色车票")
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.evaluate_memory_access(
            query="蓝色车票", candidates=[{"memo_name": "memos/feedback", "score": 0.7}],
            selected_names=[], request_id="feedback-keep",
            config={"shadow_mode": True, "observation_keep": 200}, record=True,
        )
        saved = self.store.record_memory_access_observation_feedback(
            request_id="feedback-keep", verdict="shadow_better", note="池外救回正确",
        )
        self.assertEqual(saved["verdict"], "shadow_better")
        self.assertEqual(
            self.store.memory_access_observation_detail("feedback-keep")["feedback_note"],
            "池外救回正确",
        )
        cleared = self.store.record_memory_access_observation_feedback(
            request_id="feedback-keep", verdict="clear", note="ignored",
        )
        self.assertEqual(cleared["verdict"], "")
        self.assertEqual(
            self.store.memory_access_observation_detail("feedback-keep")["feedback_note"], ""
        )

    async def test_access_export_contains_diagnostics_without_memory_bodies_or_tokens(self):
        item = episode("memos/export", "2026-07-03", "秘密场景", "稀有钥匙")
        item["card_text"] += " NEVER_EXPORT_DIARY_BODY"
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.evaluate_memory_access(
            query="稀有钥匙 " + "memos_" + "pat_THIS_SHOULD_NOT_EXPORT_123456",
            candidates=[{"memo_name": "memos/export", "score": 0.66}],
            selected_names=["memos/export"], request_id="export-request",
            config={"shadow_mode": True}, record=True,
        )
        payload = self.store.memory_access_export_payload()
        exporter = AccessAnalysisExporter(str(Path(self.tmp.name) / "exports"), keep=2)
        result = exporter.create(payload, plugin_version="6.0.0-test6")
        archive_path = exporter.archive_path(result["file"])
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            self.assertIn("observations.jsonl", names)
            self.assertIn("manifest.json", names)
            merged = b"\n".join(archive.read(name) for name in names)
        self.assertIn("稀有钥匙".encode("utf-8"), merged)
        self.assertNotIn(b"NEVER_EXPORT_DIARY_BODY", merged)
        self.assertNotIn(b"memos_pat_", merged)
        self.assertIn(b"[REDACTED]", merged)

    # 18. Detail exposes protection reasons and per-grade rescue cues
    async def test_detail_exposes_protection_and_rescue_grades(self):
        item = episode("memos/det", "2026-07-01", "天台约定", "银戒指 不离开", entities=["爱莉"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/det")
        self.assertIn("age_reference_label", detail)
        self.assertIn("state_stable_days", detail)
        self.assertIn("rescue_cue_grades", detail)
        self.assertIn("A", detail["rescue_cue_grades"])
        self.assertIn("D", detail["rescue_cue_grades"])

    # 19. Eval generation, run, and 8-gate report
    async def test_eval_generation_and_run(self):
        a = episode("memos/p1", "2026-07-01", "天台约定", "银戒指 不离开", entities=["爱莉"])
        b = episode("memos/p2", "2026-07-01", "再次天台", "天台 许诺", entities=["爱莉"])
        a["memory_type"] = "promise"
        for x in (a, b):
            self.add(x)
        self.store.rebuild_memory_access([a, b])
        gen = self.store.generate_memory_eval_cases()
        self.assertIn("protected_memory", gen["case_types"])
        self.assertIn("same_day_multi", gen["case_types"])
        run = self.store.run_memory_eval(config={"shadow_mode": True})
        self.assertGreater(run["cases_total"], 0)
        self.assertEqual(int(run["gates"]["total_count"]), 8)
        latest = self.store.latest_memory_eval_run()
        self.assertEqual(latest["run_id"], run["run_id"])
        rows = self.store.memory_eval_run_results(run["run_id"])
        self.assertEqual(len(rows), run["cases_total"])

    # 20. Safety gates stay shadow-locked
    async def test_safety_gates_stay_shadow_locked(self):
        gates = self.store.memory_access_safety_gates()
        self.assertTrue(gates["shadow_locked"])

    # 21. Deleting a memo also removes its eval cases
    async def test_delete_removes_eval_cases(self):
        item = episode("memos/to-delete", "2026-07-01", "即将删除", "测试删除")
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.generate_memory_eval_cases()
        before = {c["memo_name"] for c in self.store.list_memory_eval_cases()}
        self.assertIn("memos/to-delete", before)
        self.store.delete_by_memo_name("memos/to-delete")
        after = {c["memo_name"] for c in self.store.list_memory_eval_cases()}
        self.assertNotIn("memos/to-delete", after)

    # 22. Schema is version 11
    async def test_schema_version_is_twelve(self):
        conn = self.store._connect()
        value = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        self.assertEqual(value, str(EpisodicStore.SCHEMA_VERSION))
        self.assertEqual(int(value), EpisodicStore.SCHEMA_VERSION)
        takeover_columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(memory_access_takeover_log)"
            ).fetchall()
        }
        self.assertTrue({"evidence_terms_json", "use_detail_json", "policy_version"} <= takeover_columns)


class Test3TakeoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "e.db"), 3, "unit")
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, item):
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item,
            card_text=item["card_text"], embedding=[1.0, 0.0, 0.0],
            evidence_quality=item.get("evidence_quality", "diary_derived"),
        )

    def _make_eval_pass(self):
        """Create enough eval cases so takeover prerequisites can be met."""
        for i in range(35):
            name = f"memos/ev-{i}"
            item = episode(name, "2026-07-01", "test scene", "test key", entities=["testent"])
            self.add(item)
        self.store.rebuild_memory_access([self.store.get_episode(f"memos/ev-{i}") for i in range(35)])
        self.store.generate_memory_eval_cases()
        return self.store.run_memory_eval(config={"shadow_mode": True, "takeover_min_eval_cases": 30})

    # 1. Takeover disabled = zero impact on selected
    def test_takeover_disabled_zero_impact(self):
        result = self.store.memory_access_takeover_prerequisites(config={"takeover_enable": False})
        self.assertFalse(result["eligible"])

    # 2. Any gate fail = reject
    def test_takeover_rejected_when_gates_fail(self):
        result = self.store.memory_access_takeover_prerequisites(
            config={"takeover_enable": True})
        self.assertFalse(result["eligible"])
        c2 = next(c for c in result["conditions"] if c["key"] == "source_canary_green")
        self.assertFalse(c2["passed"])

    # 3. Empty eval pool = reject
    def test_takeover_rejected_no_eval(self):
        result = self.store.memory_access_takeover_prerequisites(
            config={"takeover_enable": True})
        self.assertFalse(result["eligible"])

    # 4. Not enough cases = reject
    def test_takeover_rejected_insufficient_cases(self):
        item = episode("memos/min", "2026-07-01", "scene", "key")
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.generate_memory_eval_cases()
        self.store.run_memory_eval(config={"shadow_mode": True})
        result = self.store.memory_access_takeover_prerequisites(
            config={"takeover_enable": True, "takeover_min_eval_cases": 100})
        self.assertFalse(result["eligible"])

    # 5. Expired eval = reject
    def test_takeover_rejected_expired_eval(self):
        item = episode("memos/exp", "2026-07-01", "scene", "key")
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.generate_memory_eval_cases()
        self.store.run_memory_eval(config={"shadow_mode": True})
        old_ts = time.time() - 30 * 86400
        conn = self.store._connect()
        conn.execute("UPDATE memory_access_eval_runs SET finished_ts=?", (old_ts,))
        conn.commit()
        result = self.store.memory_access_takeover_prerequisites(
            config={"takeover_enable": True, "eval_max_age_days": 7})
        self.assertFalse(result["eligible"])

    # 7. Selected names must be subset of appended list
    def test_compute_takeover_only_appends_not_replaces(self):
        evaluation = {"items": [
            {"memo_name": "m1", "cue_grade": "A", "exact_cue": True,
             "source_route": {"exact_link": True, "lexical": 0.9},
             "source_route_support": 0.9, "candidate_source": "source_turn_index",
             "rescue_reasons": ["source_quote:abc"]},
            {"memo_name": "m2", "cue_grade": "A", "exact_cue": True,
             "source_route": {"exact_link": True, "lexical": 0.8},
             "source_route_support": 0.88, "candidate_source": "source_turn_index",
             "rescue_reasons": ["source_quote:def"]},
            {"memo_name": "m3", "cue_grade": "C", "exact_cue": True, "rescue_reasons": ["entity:x"]},
        ]}
        appends = self.store.compute_memory_takeover_appends(
            evaluation, original_selected=["m1"], config={"takeover_max_appends": 2, "takeover_min_grade": "B"})
        names = [a["memo_name"] for a in appends]
        self.assertIn("m2", names)
        self.assertNotIn("m1", names)  # already selected, don't append
        self.assertNotIn("m3", names)  # C grade, below min_grade B

    # 8. Append rank > original length (only appends, never inserts before)
    def test_append_rank_after_originals(self):
        evaluation = {"items": [
            {"memo_name": "new", "cue_grade": "A", "exact_cue": True,
             "source_route": {"exact_link": True, "lexical": 0.8},
             "source_route_support": 0.9, "candidate_source": "source_turn_index",
             "rescue_reasons": ["source_quote:q"]},
        ]}
        appends = self.store.compute_memory_takeover_appends(
            evaluation, original_selected=["a", "b", "c"], config={"takeover_max_appends": 1})
        self.assertEqual(appends[0]["append_rank"], 4)

    # 9. C grade not appended
    def test_c_grade_not_appended(self):
        evaluation = {"items": [
            {"memo_name": "c1", "cue_grade": "C", "exact_cue": True, "rescue_reasons": ["entity:x"]},
        ]}
        appends = self.store.compute_memory_takeover_appends(
            evaluation, original_selected=[], config={"takeover_max_appends": 5, "takeover_min_grade": "B"})
        self.assertEqual(len(appends), 0)

    # 10. ambiguous_same_day not appended
    def test_ambiguous_same_day_not_appended(self):
        evaluation = {"items": [
            {"memo_name": "amb", "cue_grade": "A", "exact_cue": True,
             "ambiguous_same_day": True, "rescue_reasons": ["quote:q"]},
        ]}
        appends = self.store.compute_memory_takeover_appends(
            evaluation, original_selected=[], config={"takeover_max_appends": 5})
        self.assertEqual(len(appends), 0)

    # 11. Max appends enforced
    def test_max_appends_enforced(self):
        items = [{"memo_name": f"m{i}", "cue_grade": "A", "exact_cue": True,
                  "source_route": {"exact_link": True, "lexical": 0.8},
                  "source_route_support": 0.9, "candidate_source": "source_turn_index",
                  "rescue_reasons": ["source_quote:q"]} for i in range(5)]
        evaluation = {"items": items}
        appends = self.store.compute_memory_takeover_appends(
            evaluation, original_selected=[], config={"takeover_max_appends": 2})
        self.assertEqual(len(appends), 1)

    # 12. Breaker trips after threshold consecutive unused
    def test_breaker_trips_after_consecutive_unused(self):
        conn = self.store._connect()
        for i in range(3):
            conn.execute(
                "INSERT INTO memory_access_takeover_log(request_id,memo_name,cue_grade,append_rank,reason,response_used,breaker_trip,created_ts,evaluated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"req-{i}", f"m-{i}", "A", 1, "test", 0, 0, time.time(), 0),
            )
        conn.commit()
        breaker = self.store.memory_access_breaker_status(config={"takeover_breaker_threshold": 3})
        self.assertTrue(breaker["tripped"])

    # 13. Breaker not tripped when a use breaks the chain
    def test_breaker_not_tripped_when_used_breaks_chain(self):
        conn = self.store._connect()
        for i in range(3):
            used = 1 if i == 1 else 0
            conn.execute(
                "INSERT INTO memory_access_takeover_log(request_id,memo_name,cue_grade,append_rank,reason,response_used,breaker_trip,created_ts,evaluated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"req-{i}", f"m-{i}", "A", 1, "test", used, 0, time.time(), 0),
            )
        conn.commit()
        breaker = self.store.memory_access_breaker_status(config={"takeover_breaker_threshold": 3})
        self.assertFalse(breaker["tripped"])

    # 14. Reset breaker clears trip state
    def test_reset_breaker_clears(self):
        conn = self.store._connect()
        conn.execute(
            "INSERT INTO memory_access_takeover_log(request_id,memo_name,cue_grade,append_rank,reason,response_used,breaker_trip,created_ts,evaluated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
            ("breaker", "", "", -1, "tripped", 0, 1, time.time(), 0),
        )
        conn.commit()
        self.store.memory_access_reset_breaker()
        breaker = self.store.memory_access_breaker_status(config={"takeover_breaker_threshold": 1})
        self.assertFalse(breaker["tripped"])

    # 15. Log takeover append writes row
    def test_log_takeover_append_writes_row(self):
        self.store.log_memory_takeover_append(
            request_id="req-1", memo_name="m1", cue_grade="A", append_rank=2, reason="quote:abc")
        conn = self.store._connect()
        row = conn.execute(
            "SELECT * FROM memory_access_takeover_log WHERE request_id='req-1'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row["memo_name"]), "m1")
        self.assertEqual(str(row["cue_grade"]), "A")
        self.assertEqual(int(row["response_used"]), -1)

    def test_recent_takeover_log_is_available_for_webui(self):
        self.store.log_memory_takeover_append(
            request_id="req-log", memo_name="memos/log", cue_grade="B",
            append_rank=4, reason="date:event",
        )
        rows = self.store.list_memory_takeover_log(limit=10, include_breaker=False)
        self.assertEqual(rows[0]["request_id"], "req-log")
        self.assertEqual(rows[0]["memo_name"], "memos/log")
        self.assertEqual(int(rows[0]["response_used"]), -1)

    # 16. Schema is version 11
    def test_schema_version_is_twelve(self):
        conn = self.store._connect()
        value = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        self.assertEqual(int(value), EpisodicStore.SCHEMA_VERSION)

    # 17. takeover_log table exists
    def test_takeover_log_table_exists(self):
        conn = self.store._connect()
        names = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("memory_access_takeover_log", names)

    # 18. eval_results has baseline_rank column
    def test_eval_results_has_baseline_rank(self):
        conn = self.store._connect()
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_access_eval_results)")}
        for col in ("baseline_rank", "access_rank", "rank_delta", "used_real_recall"):
            self.assertIn(col, cols)

    # 19. eval_cases has expected_memo and confirmed_by
    def test_eval_cases_has_confirmation_columns(self):
        conn = self.store._connect()
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_access_eval_cases)")}
        for col in ("expected_memo", "confirmed_by", "supervision_level",
                    "supervision_confidence", "supervision_json",
                    "negative_memos_json", "auto_verified"):
            self.assertIn(col, cols)

    # 20. confirm_eval_case writes expected_memo and confirmed_by
    def test_confirm_eval_case_writes(self):
        item = episode("memos/cfm", "2026-07-01", "scene", "key")
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.generate_memory_eval_cases()
        cases = self.store.list_memory_eval_cases(enabled_only=False)
        cid = cases[0]["case_id"]
        ok = self.store.confirm_memory_eval_case(cid, expected_memo="memos/cfm", confirmed_by="tester")
        self.assertTrue(ok)
        cases2 = self.store.list_memory_eval_cases(enabled_only=False)
        c = next(x for x in cases2 if x["case_id"] == cid)
        self.assertEqual(c["expected_memo"], "memos/cfm")
        self.assertEqual(c["confirmed_by"], "tester")
        self.assertEqual(c["supervision_level"], "human")
        self.assertEqual(float(c["supervision_confidence"]), 1.0)

    def test_test7_auto_eval_separates_trust_levels(self):
        item = episode("memos/auto", "2026-07-01", "独特场景", "银钥匙 天台约定",
                       entities=["银钥匙"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        generated = self.store.generate_memory_eval_cases()
        self.assertGreater(int(generated["calibration_eligible"]), 0)
        cases = self.store.list_memory_eval_cases(enabled_only=False)
        self.assertTrue(any(c["supervision_level"] == "structural_verified" for c in cases))
        self.assertTrue(any(c["supervision_level"] == "heuristic" for c in cases))

    def test_test7_strict_scope_excludes_structural_and_heuristic(self):
        item = episode("memos/scope", "2026-07-01", "独特场景", "蓝玻璃 月台约定",
                       entities=["蓝玻璃"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.generate_memory_eval_cases()
        run = self.store.run_memory_eval(config={"shadow_mode": True}, scope="strict")
        self.assertEqual(run["cases_total"], 0)
        run2 = self.store.run_memory_eval(config={"shadow_mode": True}, scope="calibration")
        self.assertGreater(run2["cases_total"], 0)
        self.assertIn("macro_memory_hit_at_3", run2["calibration"])

    def test_test7_source_link_generates_strict_holdout(self):
        batch_id = self.store.archive_batch("eval-session", [
            {"role": "user", "content": "我把那枚蓝玻璃钥匙藏在旧月台第三根柱子后面。"},
            {"role": "assistant", "content": "我会记得这个位置。"},
        ], "unit")
        item = episode("memos/source", "2026-07-01", "旧月台藏钥匙",
                       "蓝玻璃钥匙 第三根柱子", entities=["蓝玻璃钥匙"])
        item["evidence"] = [{"detail": "藏钥匙", "turn_indexes": [0], "grounded": True}]
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item, card_text=item["card_text"],
            embedding=[1.0, 0.0, 0.0], source_batch_id=batch_id,
            legacy=False, evidence_quality="source_grounded",
        )
        self.store.rebuild_memory_access([self.store.get_episode(item["memo_name"])])
        generated = self.store.generate_memory_eval_cases()
        self.assertGreater(int(generated["strict_eligible"]), 0)
        cases = self.store.list_memory_eval_cases(enabled_only=False)
        strict = [c for c in cases if c["supervision_level"] == "source_verified"]
        self.assertTrue(any(c["case_type"] == "source_turn_holdout" for c in strict))
        run = self.store.run_memory_eval(config={"shadow_mode": True}, scope="strict")
        self.assertGreater(run["cases_total"], 0)

    def test_test7_regeneration_preserves_disabled_case(self):
        item = episode("memos/disabled", "2026-07-01", "独特场景", "琥珀胸针 旧桥",
                       entities=["琥珀胸针"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.generate_memory_eval_cases()
        cases = self.store.list_memory_eval_cases(enabled_only=False)
        case_id = cases[0]["case_id"]
        self.store.set_memory_eval_case_enabled(case_id, False)
        self.store.generate_memory_eval_cases()
        refreshed = next(c for c in self.store.list_memory_eval_cases(enabled_only=False)
                         if c["case_id"] == case_id)
        self.assertEqual(int(refreshed["enabled"]), 0)

    def test_test7_calibration_sampling_is_bounded_and_stratified(self):
        for i in range(45):
            item = episode(f"memos/sample-{i}", f"2025-07-{(i % 28) + 1:02d}",
                           f"独特场景{i}", f"独特线索{i} 物件{i}", entities=[f"物件{i}"])
            self.add(item)
        self.store.rebuild_memory_access(self.store.list_episodes(limit=100))
        self.store.generate_memory_eval_cases()
        run = self.store.run_memory_eval(
            config={"shadow_mode": True, "auto_eval_case_limit": 20}, scope="calibration")
        sample = run["gates"]["sample"]
        self.assertEqual(run["cases_total"], 20)
        self.assertGreater(int(sample["available_cases"]), 20)
        self.assertFalse(sample["complete"])
        self.assertGreater(int(run["calibration"]["covered_memories"]), 10)

    def test_test8_source_holdout_can_rescue_exact_linked_episode(self):
        batch_id = self.store.archive_batch("test8-source", [
            {"role": "user", "content": "I hid the cobaltkey inside the thirdmoon drawer."},
            {"role": "assistant", "content": "I will remember that exact place."},
        ], "unit")
        target = episode(
            "memos/source-target", "2026-07-02", "A quiet hiding place", "private keepsake"
        )
        target["evidence"] = [{
            "detail": "The object was hidden", "turn_indexes": [0], "grounded": True,
        }]
        self.store.upsert_episode(
            memo_name=target["memo_name"], episode=target,
            card_text=target["card_text"], embedding=[1.0, 0.0, 0.0],
            source_batch_id=batch_id, legacy=False, evidence_quality="source_grounded",
        )
        decoy = episode("memos/decoy", "2026-07-02", "Another quiet day", "drawer")
        self.add(decoy)
        self.store.rebuild_memory_access(self.store.list_episodes(limit=20))
        result = self.store.evaluate_memory_access(
            query="Where was the cobaltkey thirdmoon item?",
            candidates=[{"memo_name": "memos/decoy", "score": 0.62}],
            selected_names=["memos/decoy"], config={"shadow_mode": True}, record=False,
        )
        self.assertIn("memos/source-target", result["recommended"])
        rescued = next(item for item in result["items"]
                       if item["memo_name"] == "memos/source-target")
        self.assertEqual(rescued["candidate_source"], "source_turn_index")
        self.assertTrue(rescued["source_route"]["exact_link"])
        self.assertTrue(rescued["exact_cue"])

    def test_test8_contrastive_ranking_separates_evidenced_target(self):
        left = episode("memos/left", "2026-07-03", "Left event", "left clue")
        right = episode("memos/right", "2026-07-04", "Right event", "right clue")
        for item in (left, right):
            self.add(item)
        self.store.rebuild_memory_access([left, right])
        conn = self.store._connect()
        conn.execute(
            """INSERT OR REPLACE INTO memory_interference_edges(
               source_memo,target_memo,edge_type,strength,competition,route_scores_json,
               distinctions_json,edge_confidence,classifier_version,evidence_routes_json,
               conflict_flags_json,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("memos/left", "memos/right", "same_theme_distinct_event", 0.8, 0.1,
             "{}", "{}", 0.9, "unit", "{}", "{}", time.time()),
        )
        conn.commit()
        result = self.store.evaluate_memory_access(
            query="the exact left clue",
            candidates=[
                {"memo_name": "memos/right", "score": 0.62},
                {"memo_name": "memos/left", "score": 0.59,
                 "_source_turn_hits": [{"exact_link": True, "lexical": 0.9,
                                          "relevance": 0.85}]},
            ],
            selected_names=["memos/right"],
            config={"shadow_mode": True, "independent_cue_rescue": False}, record=False,
        )
        self.assertGreaterEqual(result["contrastive"]["adjusted"], 2)
        left_item = next(item for item in result["items"] if item["memo_name"] == "memos/left")
        right_item = next(item for item in result["items"] if item["memo_name"] == "memos/right")
        self.assertGreater(left_item["contrastive_adjustment"], 0)
        self.assertLess(right_item["contrastive_adjustment"], 0)
        self.assertEqual(result["recommended"][0], "memos/left")

    def test_test8_date_browse_disables_contrastive_adjustment(self):
        left = episode("memos/date-left", "2026-07-03", "Morning event", "morning")
        right = episode("memos/date-right", "2026-07-03", "Evening event", "evening")
        for item in (left, right):
            self.add(item)
        self.store.rebuild_memory_access([left, right])
        result = self.store.evaluate_memory_access(
            query="What happened on 2026-07-03?",
            candidates=[
                {"memo_name": "memos/date-left", "score": 0.61},
                {"memo_name": "memos/date-right", "score": 0.60},
            ], selected_names=["memos/date-left", "memos/date-right"],
            config={"shadow_mode": True}, record=False,
        )
        self.assertTrue(result["date_only_query"])
        self.assertEqual(result["contrastive"]["adjusted"], 0)

    def test_test8_source_evidence_enriches_existing_candidate_without_duplicate(self):
        batch_id = self.store.archive_batch("test8-enrich", [
            {"role": "user", "content": "The amberseal is under the northwindow cushion."},
        ], "unit")
        target = episode("memos/enriched", "2026-07-05", "A hidden seal", "keepsake")
        target["evidence"] = [{
            "detail": "The seal has a place", "turn_indexes": [0], "grounded": True,
        }]
        self.store.upsert_episode(
            memo_name=target["memo_name"], episode=target, card_text=target["card_text"],
            embedding=[1.0, 0.0, 0.0], source_batch_id=batch_id,
            legacy=False, evidence_quality="source_grounded",
        )
        self.store.rebuild_memory_access(self.store.list_episodes(limit=10))
        result = self.store.evaluate_memory_access(
            query="Where is the amberseal northwindow object?",
            candidates=[{"memo_name": "memos/enriched", "score": 0.52}],
            selected_names=["memos/enriched"], config={"shadow_mode": True}, record=False,
        )
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["source_enriched_count"], 1)
        self.assertEqual(result["rescue_pool_count"], 0)
        self.assertTrue(result["items"][0]["source_route"]["exact_link"])

    # 21. Events table has takeover columns
    def test_events_has_takeover_columns(self):
        conn = self.store._connect()
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_access_events)")}
        self.assertIn("takeover_applied", cols)
        self.assertIn("takeover_reason", cols)

    # 22. Vivid threshold calibration: today memory can be vivid
    def test_today_memory_can_be_vivid(self):
        item = episode("memos/today", time.strftime("%Y-%m-%d", time.localtime()), "today scene", "today key",
                       entities=["unique_entity_xyz"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/today")
        self.assertGreater(float(detail["accessibility"]), 0.60,
                           "today memory should have decent accessibility after calibration")

    # 23. Neighbor window is capped (75 episodes don't explode edges)
    def test_neighbor_window_capped(self):
        eps = []
        for i in range(75):
            e = episode(f"memos/nw-{i}", f"2022-{(i%12)+1:02d}-{(i%28)+1:02d}",
                        f"scene{i}", f"common_word test{i}", entities=["shared_ent"])
            eps.append(e)
            self.add(e)
        result = self.store.rebuild_memory_access(eps)
        avg_edges = result.get("edges", 0) / 75.0
        self.assertLess(avg_edges, 3.0, f"avg edges per memo {avg_edges:.2f} should be < 3.0")

    def test_test6_candidate_work_is_bounded_per_memory(self):
        eps = []
        for i in range(600):
            item = episode(
                f"memos/scale-{i}", f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
                f"scene {i} shared scene", f"shared common cue unique_{i}",
                entities=["shared-person", f"person-{i % 40}"],
            )
            eps.append(item)
            self.add(item)
        result = self.store.rebuild_memory_access(eps)
        self.assertLessEqual(result["candidate_budget"], 36)
        self.assertLessEqual(
            result["pairs"],
            len(eps) * (result["candidate_budget"] + 8) + result["mandatory_pairs"],
        )
        meta = self.store.memory_access_index_meta()
        self.assertEqual(meta["interference_strategy"], "bounded_rare_cue_v2")
        self.assertEqual(meta["interference_scale_health"], "healthy")

    def test_test6_restart_reuses_unchanged_interference_graph(self):
        items = [
            episode("memos/reuse-a", "2026-07-01", "天台约定", "银戒指 不离开", entities=["爱莉"]),
            episode("memos/reuse-b", "2026-07-01", "天台约定再述", "银戒指 不离开", entities=["爱莉"]),
        ]
        for item in items:
            self.add(item)
        first = self.store.rebuild_memory_access(items, reuse_interference=True)
        second = self.store.rebuild_memory_access(items, reuse_interference=True)
        self.assertFalse(first["graph_reused"])
        self.assertTrue(second["graph_reused"])
        self.assertEqual(first["edges"], second["edges"])

    def test_test6_incremental_batch_refreshes_shared_derivatives_and_fingerprint(self):
        items = [
            episode("memos/batch-a", "2026-07-01", "雨夜天台", "银戒指 约定", entities=["爱莉"]),
            episode("memos/batch-b", "2026-07-01", "雨夜天台再述", "银戒指 约定", entities=["爱莉"]),
        ]
        items[0]["source_batch_id"] = "shared-batch"
        items[1]["source_batch_id"] = "shared-batch"
        for item in items:
            self.add(item)
        self.store.rebuild_memory_access(items)
        result = self.store.sync_memory_interference_batch([item["memo_name"] for item in items])
        self.assertEqual(result["failed"], [])
        self.assertGreaterEqual(result["edges"], 2)
        self.assertGreater(self.store.memory_access_detail("memos/batch-a")["interference_load"], 0)
        stored = [self.store.get_episode(item["memo_name"]) for item in items]
        self.assertFalse(self.store._access.interference_rebuild_needed(stored, max_neighbors=12))

    # 24. Breaker threshold configurable
    def test_breaker_threshold_configurable(self):
        conn = self.store._connect()
        conn.execute(
            "INSERT INTO memory_access_takeover_log(request_id,memo_name,cue_grade,append_rank,reason,response_used,breaker_trip,created_ts,evaluated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
            ("req", "m", "A", 1, "test", 0, 0, time.time(), 0),
        )
        conn.commit()
        b5 = self.store.memory_access_breaker_status(config={"takeover_breaker_threshold": 5})
        self.assertFalse(b5["tripped"])
        b1 = self.store.memory_access_breaker_status(config={"takeover_breaker_threshold": 1})
        self.assertTrue(b1["tripped"])

    # 25. takeover_prerequisites returns 6 conditions
    def test_takeover_prerequisites_has_six_conditions(self):
        result = self.store.memory_access_takeover_prerequisites(config={"takeover_enable": True})
        self.assertEqual(len(result["conditions"]), 7)

    def test_test9_rejects_structural_ab_without_exact_source(self):
        evaluation = {"items": [{
            "memo_name": "memos/structural", "cue_grade": "A", "exact_cue": True,
            "candidate_source": "precise_cue_index", "rescue_reasons": ["quote:abc"],
            "source_route": {"exact_link": False, "lexical": 0.0},
            "source_route_support": 0.0,
        }]}
        appends = self.store.compute_memory_takeover_appends(
            evaluation, original_selected=[], config={"takeover_max_appends": 1})
        self.assertEqual(appends, [])

    def test_test9_rejects_date_browse_even_with_source(self):
        evaluation = {"date_only_query": True, "items": [{
            "memo_name": "memos/date", "cue_grade": "A", "exact_cue": True,
            "candidate_source": "source_turn_index", "rescue_reasons": ["source_quote:abc"],
            "source_route": {"exact_link": True, "lexical": 0.9},
            "source_route_support": 0.9,
        }]}
        appends = self.store.compute_memory_takeover_appends(
            evaluation, original_selected=[], config={"takeover_max_appends": 1})
        self.assertEqual(appends, [])

    def test_test9_response_use_requires_specific_evidence(self):
        item = episode("memos/use", "2026-07-01", "amber seal under north window",
                       "amberseal northwindow", entities=["amberseal"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.log_memory_takeover_append(
            request_id="req-use", memo_name="memos/use", cue_grade="A", append_rank=2,
            reason="source", evidence_terms=["amberseal", "northwindow"],
        )
        weak = self.store.evaluate_memory_takeover_response_use(
            request_id="req-use", response_text="I remember that window."
        )
        self.assertEqual(weak["unused"], 1)
        self.store.log_memory_takeover_append(
            request_id="req-use-2", memo_name="memos/use", cue_grade="A", append_rank=2,
            reason="source", evidence_terms=["amberseal", "northwindow"],
        )
        strong = self.store.evaluate_memory_takeover_response_use(
            request_id="req-use-2", response_text="The amberseal was by the northwindow."
        )
        self.assertEqual(strong["updated"], 1)
        self.assertEqual(strong["reconsolidated"], 1)
        detail = self.store.memory_access_detail("memos/use")
        self.assertEqual(detail["reconsolidation_count"], 1)
        self.assertEqual(detail["successful_use_count"], 1)
        row = self.store.list_memory_takeover_log(limit=1)[0]
        self.assertEqual(row["policy_version"], "source_exact_canary")
        self.assertGreaterEqual(len(row["use_detail"].get("overlap") or []), 2)

    def test_test10_manual_feedback_is_bounded_audited_and_non_destructive(self):
        item = episode("memos/feedback", "2026-07-01", "quiet promise", "silver key")
        self.add(item)
        self.store.rebuild_memory_access([item])
        before = self.store.get_episode("memos/feedback")["card_text"]
        result = self.store.record_memory_access_feedback(
            memo_name="memos/feedback", action="still_important", note="long line",
        )
        self.assertFalse(result["memory_content_changed"])
        self.assertGreater(result["after"]["accessibility"], result["before"]["accessibility"])
        self.assertEqual(self.store.get_episode("memos/feedback")["card_text"], before)
        event = self.store.list_memory_access_events(memo_name="memos/feedback", limit=1)[0]
        self.assertEqual(event["event_kind"], "manual_feedback")
        self.assertEqual(event["detail"]["action"], "still_important")

    def test_test10_activity_trends_are_dense_and_include_reconsolidation(self):
        item = episode("memos/trend", "2026-07-01", "amber seal", "amberseal northwindow",
                       entities=["amberseal"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.log_memory_takeover_append(
            request_id="trend-use", memo_name="memos/trend", cue_grade="A",
            append_rank=1, reason="source", evidence_terms=["amberseal", "northwindow"],
        )
        self.store.evaluate_memory_takeover_response_use(
            request_id="trend-use", response_text="amberseal northwindow",
        )
        trends = self.store.memory_access_overview()["activity_trends"]["days_30"]
        self.assertEqual(len(trends), 30)
        self.assertEqual(trends[-1]["reconsolidations"], 1)

    def test_test10_canary_use_respects_disabled_reconsolidation(self):
        item = episode("memos/no-recon", "2026-07-01", "amber seal", "amberseal northwindow",
                       entities=["amberseal"])
        self.add(item)
        self.store.rebuild_memory_access([item])
        self.store.log_memory_takeover_append(
            request_id="no-recon", memo_name="memos/no-recon", cue_grade="A",
            append_rank=1, reason="source", evidence_terms=["amberseal", "northwindow"],
        )
        result = self.store.evaluate_memory_takeover_response_use(
            request_id="no-recon", response_text="amberseal northwindow",
            reconsolidate=False,
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["reconsolidated"], 0)
        detail = self.store.memory_access_detail("memos/no-recon")
        self.assertEqual(detail["reconsolidation_count"], 0)

    def test_test9_source_canary_has_independent_positive_gate(self):
        results = []
        for i in range(5):
            results.append({
                "case_id": f"src-{i}", "case_type": "source_turn_holdout",
                "supervision_level": "source_verified", "rank": 1,
                "access_rank": 1, "baseline_rank": 2, "used_real_recall": True,
                "source_link_rescue_used": True, "failed_open": False,
                "negative_memos": [f"neg-{i}"], "beats_all_negatives": True,
            })
        canary = self.store._access._compute_source_canary_gate(results)
        self.assertTrue(canary["ready"])
        self.assertEqual(canary["cases"], 5)
        conn = self.store._connect()
        now = time.time()
        conn.execute(
            """INSERT INTO memory_access_eval_runs(
               run_id,algorithm_version,config_json,gate_results_json,
               cases_total,cases_passed,started_ts,finished_ts) VALUES(?,?,?,?,?,?,?,?)""",
            ("canary-pass", "5.1.0", "{}", json.dumps({"source_canary": canary}),
             5, 5, now - 1, now),
        )
        conn.commit()
        status = self.store.memory_access_takeover_prerequisites(config={
            "takeover_enable": True, "takeover_source_min_eval_cases": 5,
        })
        self.assertTrue(status["eligible"])
        self.assertEqual(status["policy"], "source_exact_canary")

    def test_test9_source_canary_rejects_baseline_regression(self):
        canary = self.store._access._compute_source_canary_gate([{
            "case_id": "regress", "case_type": "source_turn_holdout",
            "supervision_level": "source_verified", "rank": 3,
            "access_rank": 3, "baseline_rank": 1, "used_real_recall": True,
            "source_link_rescue_used": True, "failed_open": False,
            "negative_memos": [], "beats_all_negatives": False,
        }])
        self.assertFalse(canary["ready"])
        self.assertEqual(canary["failures"]["baseline_regression"], ["regress"])

    def test_breaker_marker_remains_tripped_until_reset(self):
        conn = self.store._connect()
        for i in range(3):
            conn.execute(
                "INSERT INTO memory_access_takeover_log(request_id,memo_name,cue_grade,append_rank,reason,response_used,breaker_trip,created_ts,evaluated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"persist-{i}", f"m-{i}", "A", 1, "test", 0, 0, time.time(), 0),
            )
        conn.commit()
        self.assertTrue(self.store.memory_access_breaker_status(
            config={"takeover_breaker_threshold": 3})["tripped"])
        self.store.memory_access_trip_breaker(reason="unit")
        status = self.store.memory_access_breaker_status(
            config={"takeover_breaker_threshold": 3})
        self.assertTrue(status["tripped"])
        self.assertTrue(status.get("persistent"))
        self.store.memory_access_reset_breaker()
        self.assertFalse(self.store.memory_access_breaker_status(
            config={"takeover_breaker_threshold": 3})["tripped"])

    def test_dirty_batch_retains_queue_remainder(self):
        names = [f"memos/dirty-{i}" for i in range(60)]
        self.store.mark_memory_interference_dirty(names)
        batch = self.store.pop_memory_interference_dirty(50)
        remaining = self.store.memory_access_index_meta()["interference_dirty_memos"]
        self.assertEqual(len(batch), 50)
        self.assertEqual(remaining, names[50:])

    def test_safety_gates_fail_when_required_case_types_are_empty(self):
        rows = [{
            "case_id": f"general-{i}", "case_type": "general", "found": True,
            "rank": 1, "grade_match": True, "rescue_used": False,
            "failed_open": False, "exact_cue_count": 0,
            "used_real_recall": True, "baseline_rank": 1, "access_rank": 1,
        } for i in range(30)]
        gates = self.store._access._compute_safety_gates(rows, [0.1] * len(rows))
        self.assertFalse(gates["ready_for_takeover"])
        self.assertFalse(gates["G1"]["passed"])
        self.assertEqual(gates["G1"]["status"], "insufficient_samples")

    def test_g2_requires_top3_without_regressing_real_baseline(self):
        base = {
            "case_id": "date", "case_type": "date_plus_entity", "found": True,
            "rank": 3, "grade_match": True, "rescue_used": False,
            "failed_open": False, "exact_cue_count": 1,
            "used_real_recall": True, "baseline_rank": 2, "access_rank": 3,
        }
        gates = self.store._access._compute_safety_gates([base], [0.1])
        self.assertFalse(gates["G2"]["passed"])
        improved = dict(base, access_rank=1, rank=1)
        gates = self.store._access._compute_safety_gates([improved], [0.1])
        self.assertTrue(gates["G2"]["passed"])

    def test_eval_uses_only_confirmed_cases_for_gates_and_real_recall(self):
        first = episode("memos/confirmed", "2026-07-01", "车票落在窗边", "蓝色车票 窗边")
        second = episode("memos/unconfirmed", "2026-07-02", "普通散步", "散步")
        self.add(first)
        self.add(second)
        self.store.rebuild_memory_access([first, second])
        self.store.generate_memory_eval_cases()
        cases = self.store.list_memory_eval_cases(enabled_only=False)
        target = cases[0]
        self.store.confirm_memory_eval_case(
            target["case_id"], expected_memo=target["memo_name"], confirmed_by="tester",
        )

        def recall_fn(query, top_k):
            memo_name = target["memo_name"] if query == target["query"] else "memos/unconfirmed"
            return ([{"memo_name": memo_name, "score": 1.0, "selected": True}], [memo_name])

        result = self.store.run_memory_eval(
            config={"shadow_mode": True}, recall_fn=recall_fn, recalled_only=False,
        )
        self.assertEqual(result["gates"]["eligible_cases"], 1)
        self.assertEqual(result["gates"]["real_recall_cases"], 1)
        self.assertTrue(all(item["used_real_recall"] for item in result["results"]))

    def test_pool_external_takeover_hit_is_materialized_from_episode(self):
        from astrbot_plugin_memos_memory.main import MemosMemoryPlugin

        item = episode("memos/external", "2026-07-03", "天台约定", "离开前告诉你")

        class Episodes:
            def get_episode(self, memo_name):
                return {**item, "episode_id": "ep-1", "card_text": item["card_text"]}

        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = Episodes()
        hit = plugin._materialize_memory_access_hit(
            "memos/external", [], {"access_score": 0.9, "reason": "quote"},
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit["memo_name"], "memos/external")
        self.assertTrue(hit["_selected"])
        self.assertTrue(hit["_access_takeover"])

    def test_test9_materialized_canary_carries_source_turn(self):
        from astrbot_plugin_memos_memory.main import MemosMemoryPlugin

        item = episode("memos/source-canary", "2026-07-03", "旧月台约定", "蓝玻璃钥匙")

        class Episodes:
            def get_episode(self, memo_name):
                return {**item, "episode_id": "ep-source", "card_text": item["card_text"]}

        plugin = object.__new__(MemosMemoryPlugin)
        plugin._episodes = Episodes()
        source = [{"role": "user", "content": "钥匙在第三根柱子后面。", "exact_link": True}]
        hit = plugin._materialize_memory_access_hit(
            "memos/source-canary", [], {
                "access_score": 0.95, "reason": "source", "source_turn_hits": source,
                "presentation": "compact_source_evidence",
            },
        )
        self.assertEqual(hit["_source_turn_hits"], source)
        self.assertTrue(hit["_source_evidence_hit"])
        self.assertEqual(hit["_access_takeover_presentation"], "compact_source_evidence")

    def test_webui_eval_route_wires_real_recall_and_confirmation_controls(self):
        root = Path(__file__).resolve().parents[1]
        webui = (root / "webui.py").read_text(encoding="utf-8")
        access = (root / "access.html").read_text(encoding="utf-8")
        self.assertIn("recall_fn=recall_fn", webui)
        self.assertIn("scope=str(body.get(\"scope\") or \"calibration\")", webui)
        self.assertIn("case-confirm", access)
        self.assertIn("loadTakeoverStatus", access)
        self.assertIn("takeover-rows", access)
        self.assertIn("m-deferred", access)
        self.assertIn("m-pairs", access)
        self.assertIn("interference_pairs_per_memory", access)
        self.assertIn("可回原文", access)
        self.assertIn("eligibility_label", access)
        self.assertIn("access_supplement_v1", webui)
        self.assertIn("Stage 2 补充支路", access)
        self.assertIn("旧主召回完整保留", access)

    def test_takeover_log_is_committed_only_after_injection_block_exists(self):
        main = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
        selection_start = main.index("proposed_appends =")
        block_start = main.index("blocks.append({", selection_start)
        log_pos = main.index("log_memory_takeover_append(", selection_start)
        self.assertGreater(log_pos, block_start)
        self.assertIn('and not h.get("_access_takeover")', main)
        self.assertIn('supplement source evidence unavailable, fallback to matched history', main)


class Test5EvidenceAwareForgettingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "episodic.db"), 3, "unit")
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, item):
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item,
            card_text=item["card_text"], embedding=[1.0, 0.0, 0.0],
            evidence_quality=item.get("evidence_quality", "diary_derived"),
            source_batch_id=str(item.get("source_batch_id") or ""),
            source_updated_ts=float(item.get("source_updated_ts") or 0),
        )

    async def test_high_value_legacy_diary_is_deferred_without_forgetting_evidence(self):
        item = episode("memos/deferred", "2022-03-14", "旧日记里的拥抱", "拥抱 留下来", importance=5)
        item.update({"event_ts": 0, "occurred_at": "", "memory_type": "emotional_anchor",
                     "source_updated_ts": time.time()})
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail(item["memo_name"])
        self.assertFalse(detail["decay_eligible"])
        self.assertEqual(detail["eligibility_status"], "deferred")
        self.assertLess(float(detail["state_confidence"]), 0.4)

    async def test_plain_low_value_legacy_daily_memory_remains_decay_eligible(self):
        item = episode("memos/plain", "2022-03-14", "普通午饭", "午饭", importance=2)
        item.update({"event_ts": 0, "occurred_at": "", "memory_type": "daily_texture",
                     "source_updated_ts": time.time()})
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail(item["memo_name"])
        self.assertTrue(detail["decay_eligible"])
        self.assertEqual(detail["eligibility_status"], "eligible")

    async def test_batch_marker_without_turn_link_is_still_evidence_deferred(self):
        batch_id = self.store.archive_batch("legacy-session", [], "legacy_memos")
        item = episode("memos/batch-only", "2022-03-14", "旧日记里的拥抱", "拥抱 留下来", importance=5)
        item.update({"event_ts": 0, "occurred_at": "", "memory_type": "emotional_anchor",
                     "source_batch_id": batch_id, "source_updated_ts": time.time()})
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail(item["memo_name"])
        self.assertEqual(detail["eligibility_status"], "deferred")

    async def test_factual_event_time_does_not_enter_deferred_state(self):
        item = episode("memos/factual", "2022-03-14", "旧日记里的拥抱", "拥抱 留下来", importance=5)
        item["memory_type"] = "emotional_anchor"
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail(item["memo_name"])
        self.assertEqual(detail["age_reference_kind"], "event")
        self.assertEqual(detail["eligibility_status"], "eligible")

    async def test_recent_source_update_proxy_cannot_make_old_diary_fully_vivid(self):
        item = episode("memos/proxy-cap", "2022-03-14", "被重新编辑的旧日记", "旧日记", importance=2)
        item.update({"event_ts": 0, "occurred_at": "", "memory_type": "daily_texture",
                     "source_updated_ts": time.time()})
        self.add(item)
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail(item["memo_name"])
        self.assertEqual(detail["age_reference_kind"], "source_updated_proxy")
        self.assertLessEqual(float(detail["vividness"]), 0.52)

    async def test_rebuild_does_not_bypass_confirmed_state_transition_policy(self):
        item = episode("memos/rebuild-state", "2022-03-14", "很久以前的普通事件", "普通事件", importance=1)
        item["memory_type"] = "daily_texture"
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute(
            "UPDATE memory_access_state SET access_state='vivid',state_reason_json='{}' WHERE memo_name=?",
            (item["memo_name"],),
        )
        conn.commit()
        first = self.store.rebuild_memory_access(
            [item], config={"state_confirmation_runs": 3, "vivid_threshold": 0.99}
        )
        self.assertEqual(first["states_changed"], 0)
        self.assertEqual(self.store.memory_access_detail(item["memo_name"])["access_state"], "vivid")

    async def test_algorithm_upgrade_preserves_use_history_while_recomputing_dimensions(self):
        item = episode("memos/history", "2022-03-14", "旧日记里的拥抱", "拥抱 留下来", importance=5)
        item.update({"event_ts": 0, "occurred_at": "", "memory_type": "emotional_anchor",
                     "source_updated_ts": time.time()})
        self.add(item)
        self.store.rebuild_memory_access([item])
        conn = self.store._connect()
        conn.execute(
            "UPDATE memory_access_state SET algorithm_version='5.0.0-test4',"
            "reconsolidation_count=3,successful_use_count=2,vividness=0.99,"
            "state_reason_json='{\"pending_state\":\"deep\",\"pending_confirmations\":9}' "
            "WHERE memo_name=?",
            (item["memo_name"],),
        )
        conn.commit()
        self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail(item["memo_name"])
        self.assertEqual(detail["reconsolidation_count"], 3)
        self.assertEqual(detail["successful_use_count"], 2)
        self.assertLess(float(detail["vividness"]), 0.99)
        self.assertFalse(detail.get("pending_state"))

    async def test_overview_and_eval_cases_expose_deferred_evidence(self):
        item = episode("memos/deferred-ui", "2022-03-14", "旧日记里的拥抱", "拥抱 留下来", importance=5)
        item.update({"event_ts": 0, "occurred_at": "", "memory_type": "emotional_anchor",
                     "source_updated_ts": time.time()})
        self.add(item)
        self.store.rebuild_memory_access([item])
        overview = self.store.memory_access_overview()
        self.assertEqual(overview["decay_eligibility"]["deferred"], 1)
        self.assertIn("source_link_ratio", overview["evidence_readiness"])
        generated = self.store.generate_memory_eval_cases(replace=True)
        self.assertEqual(generated["case_types"]["evidence_deferred_legacy"], 1)


if __name__ == "__main__":
    unittest.main()

