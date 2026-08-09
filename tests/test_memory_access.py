import tempfile
import time
import unittest
import sqlite3
from pathlib import Path

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore


def episode(name, date, scene, retrieval, *, entities=None, importance=3,
            state_change="", long_effect="", evidence_quality="diary_derived"):
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
        self.assertEqual(conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"], "7")

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

    async def test_backfill_preserves_legacy_diary_without_source(self):
        item = episode("memos/old", "2020-02-03", "旧车站告别", "红围巾 站台", importance=4)
        self.add(item)
        result = self.store.rebuild_memory_access([item])
        detail = self.store.memory_access_detail("memos/old")
        self.assertEqual(result["updated"], 1)
        self.assertGreater(detail["accessibility"], 0.20)
        self.assertEqual(detail["grounding_bonus"], 0.0)
        self.assertLess(detail["vividness"], 0.10)

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
