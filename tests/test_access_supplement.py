from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.retrieval_optimizer import parse_temporal_constraint


def make_episode(name: str, date_text: str, text: str) -> dict:
    event_ts = time.mktime(time.strptime(date_text, "%Y-%m-%d"))
    return {
        "memo_name": name,
        "occurred_at": date_text + " 下午",
        "event_ts": event_ts,
        "time_basis": "explicit_diary",
        "memory_type": "plot_fact",
        "importance": 3,
        "scene_anchor": text,
        "retrieval_key": text,
        "state_change": "",
        "long_effect": "",
        "trigger_hint": text,
        "entities": ["爱莉"] if "爱莉" in text else [],
        "unresolved": [],
        "card_text": text,
        "evidence_quality": "diary_derived",
    }


class AccessSupplementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "episodic.db"), 3, "unit")
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, item: dict) -> None:
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item, card_text=item["card_text"],
            embedding=[1.0, 0.0, 0.0], evidence_quality=item["evidence_quality"],
        )
        self.store.rebuild_memory_access([item])

    @staticmethod
    def constraint(query: str) -> dict:
        return parse_temporal_constraint(query, datetime(2026, 8, 23, 12, 0)).as_dict()

    def test_temporal_slot_recovers_candidate_outside_baseline_pool(self):
        target = make_episode("memos/target", "2025-08-03", "爱莉在天台答应一起看星星")
        baseline = make_episode("memos/baseline", "2026-08-01", "最近一次普通聊天")
        self.add(target)
        self.add(baseline)
        query = "2025年8月3日爱莉在天台答应了什么"
        result = self.store.compute_memory_supplement_appends(
            {"enabled": True, "query": query, "items": [], "date_only_query": False},
            original_selected=[baseline["memo_name"]],
            temporal_constraint=self.constraint(query),
            config={"supplement_temporal_threshold": 0.50},
        )
        self.assertEqual([item["memo_name"] for item in result], [target["memo_name"]])
        self.assertEqual(result[0]["slot"], "T")
        self.assertEqual(result[0]["candidate_source"], "temporal_index")
        self.assertEqual(result[0]["evidence_quality"], "diary_derived")

    def test_bare_date_with_multiple_events_does_not_choose_arbitrarily(self):
        self.add(make_episode("memos/day-a", "2025-08-03", "天台看星星"))
        self.add(make_episode("memos/day-b", "2025-08-03", "厨房做蛋糕"))
        query = "2025年8月3日发生了什么"
        result = self.store.compute_memory_supplement_appends(
            {"enabled": True, "query": query, "items": [], "date_only_query": True},
            original_selected=[], temporal_constraint=self.constraint(query),
            config={"supplement_temporal_threshold": 0.45},
        )
        self.assertEqual(result, [])

    def test_access_slot_allows_old_diary_without_claiming_source_evidence(self):
        target = make_episode("memos/old-diary", "2024-01-02", "纸鹤暗号和秘密约定")
        self.add(target)
        result = self.store.compute_memory_supplement_appends(
            {
                "enabled": True,
                "query": "纸鹤暗号是什么",
                "date_only_query": False,
                "items": [{
                    "memo_name": target["memo_name"], "base_score": 0.82,
                    "access_score": 0.70, "accessibility": 0.45,
                    "cue_support": 0.88, "route_support": 0.35,
                    "exact_cue": True, "cue_grade": "B",
                    "candidate_source": "precise_cue_index",
                    "rescue_reasons": ["entity:纸鹤", "term:暗号"],
                    "access_state": "deep", "source_route": {},
                }],
            },
            original_selected=[], temporal_constraint={}, config={},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["slot"], "A")
        self.assertFalse(result[0]["source_recoverable"])
        self.assertEqual(result[0]["presentation"], "matched_passage_or_episode")

    def test_separate_slots_keep_distinct_same_day_memories_and_exact_dedup_only(self):
        temporal = make_episode("memos/same-day-temporal", "2025-08-03", "爱莉在天台看星星")
        rescue = make_episode("memos/same-day-rescue", "2025-08-03", "爱莉留下纸鹤暗号")
        baseline = make_episode("memos/already", "2025-08-03", "爱莉谈到另一件往事")
        for item in (temporal, rescue, baseline):
            self.add(item)
        query = "2025年8月3日爱莉的天台和纸鹤暗号"
        evaluation = {
            "enabled": True, "query": query, "date_only_query": False,
            "items": [{
                "memo_name": rescue["memo_name"], "base_score": 0.86,
                "access_score": 0.81, "accessibility": 0.60,
                "cue_support": 0.90, "route_support": 0.40,
                "exact_cue": True, "cue_grade": "B",
                "candidate_source": "precise_cue_index",
                "rescue_reasons": ["term:纸鹤", "term:暗号"],
                "access_state": "latent", "source_route": {},
            }],
        }
        result = self.store.compute_memory_supplement_appends(
            evaluation, original_selected=[baseline["memo_name"]],
            temporal_constraint=self.constraint(query),
            config={"supplement_max": 2, "supplement_temporal_threshold": 0.50},
        )
        self.assertEqual({item["slot"] for item in result}, {"T", "A"})
        self.assertEqual(len({item["memo_name"] for item in result}), 2)
        self.assertNotIn(baseline["memo_name"], {item["memo_name"] for item in result})

    def test_weak_candidates_do_not_force_fill(self):
        item = make_episode("memos/weak", "2024-02-02", "无关旧事")
        self.add(item)
        result = self.store.compute_memory_supplement_appends(
            {
                "enabled": True, "query": "今天心情如何", "date_only_query": False,
                "items": [{
                    "memo_name": item["memo_name"], "base_score": 0.10,
                    "access_score": 0.10, "accessibility": 0.40,
                    "cue_support": 0.0, "route_support": 0.0,
                    "exact_cue": False, "cue_grade": "D",
                    "candidate_source": "retrieval_pool", "access_state": "deep",
                    "source_route": {},
                }],
            },
            original_selected=[], temporal_constraint={}, config={},
        )
        self.assertEqual(result, [])

    def test_supplement_breaker_ignores_legacy_canary_rows(self):
        conn = self.store._connect()
        for index in range(3):
            conn.execute(
                """INSERT INTO memory_access_takeover_log(
                   request_id,memo_name,response_used,breaker_trip,route_mode,created_ts)
                   VALUES(?,?,?,?,?,?)""",
                (f"legacy-{index}", f"memos/{index}", 0, 0, "", time.time()),
            )
        conn.commit()
        status = self.store.memory_access_breaker_status(
            config={"route_mode": "supplement", "takeover_breaker_threshold": 3}
        )
        self.assertFalse(status["tripped"])

    def test_schema_twelve_contains_stage_two_audit_fields(self):
        conn = self.store._connect()
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(memory_access_takeover_log)"
            ).fetchall()
        }
        self.assertEqual(version, "15")
        self.assertTrue({
            "route_mode", "slot", "scores_json", "evidence_quality",
            "source_recoverable",
        } <= columns)


if __name__ == "__main__":
    unittest.main()
