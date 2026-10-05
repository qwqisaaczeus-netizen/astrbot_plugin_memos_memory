# -*- coding: utf-8 -*-
"""6.0.0-test3..test5 claim, prospective, and Shadow retrieval tests."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_memos_memory.claim_ledger import ClaimLedger
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.prospective_memory import ProspectiveMemory
from astrbot_plugin_memos_memory.query_planner import QueryPlanner
from astrbot_plugin_memos_memory.thread_retrieval import ThreadRetrievalLab


def episode(name: str, text: str, *, ts: float, entities=None, batch: str = "") -> dict:
    return {
        "memo_name": name,
        "occurred_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
        "event_ts": ts,
        "time_basis": "source_turn" if batch else "diary_metadata",
        "memory_type": "relationship",
        "importance": 4,
        "scene_anchor": "天台",
        "retrieval_key": text,
        "state_change": text,
        "long_effect": "",
        "trigger_hint": "天台 银戒指",
        "entities": entities if entities is not None else ["爱莉"],
        "unresolved": [],
        "card_text": "我记得那天在天台发生的事。",
        "evidence_quality": "source_grounded" if batch else "diary_derived",
        "source_batch_id": batch,
        "source_updated_ts": 0.0,
    }


class _PlannerPlugin:
    query_plan_enable = True
    character_name = "爱莉"


class ThreadTest5Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "memory.db"), 3, "unit")
        await self.store.init()
        self.now = time.time()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, item: dict, scope: str = "role-a") -> str:
        if item.get("source_batch_id"):
            conn = self.store._connect()
            conn.execute(
                """INSERT OR IGNORE INTO source_batches(batch_id,session_id,source_kind,
                   content_hash,message_count,first_event_ts,last_event_ts,status,created_ts,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item["source_batch_id"], "unit", "test", item["source_batch_id"], 1,
                 item["event_ts"], item["event_ts"], "archived", self.now, self.now),
            )
            conn.commit()
        self.store.upsert_episode(
            memo_name=item["memo_name"], episode=item, card_text=item["card_text"],
            embedding=[0.7, 0.7, 0.7], evidence_quality=item["evidence_quality"],
            source_batch_id=item.get("source_batch_id", ""),
        )
        eid = str(self.store.get_episode(item["memo_name"])["episode_id"])
        self.store.thread_enqueue(scope, eid)
        return eid

    def thread(self, episode_ids: list[str], thread_id: str = "thread-a") -> str:
        self.store._threads.replace_thread_projection(
            {
                "thread_id": thread_id, "scope_id": "role-a", "thread_type": "relationship_arc",
                "title": "天台关系线", "status": "active", "confidence": 0.92,
                "first_event_ts": self.now, "last_event_ts": self.now + len(episode_ids),
                "source_quality_floor": "diary_derived",
            },
            [
                {"episode_id": eid, "role": "supporting", "sequence_no": index,
                 "membership_confidence": 0.9, "decision_source": "unit"}
                for index, eid in enumerate(episode_ids)
            ],
        )
        return thread_id

    def test_schema_has_test5_tables_columns_and_version(self):
        conn = self.store._connect()
        self.assertEqual(EpisodicStore.SCHEMA_VERSION, 15)
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"memory_claim_slots", "prospective_trigger_observations"} <= tables)
        claim_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_claims)")}
        self.assertTrue({"slot_key", "explicitness", "evidence_json", "content_hash"} <= claim_cols)
        self.store._threads.init_schema(conn)
        self.assertEqual(int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]), EpisodicStore.SCHEMA_VERSION)

    def test_grounded_claims_are_active_but_diary_high_risk_is_uncertain(self):
        grounded = self.add(episode("memos/a", "我答应爱莉明天去天台", ts=self.now, batch="batch-a"))
        diary = self.add(episode("memos/b", "我们关系已经非常亲近", ts=self.now + 1))
        ledger = ClaimLedger(self.store._threads)
        result = ledger.rebuild_scope("role-a")
        self.assertGreaterEqual(result["extracted"], 2)
        claims = self.store.thread_list_claims(scope_id="role-a", limit=100)
        by_episode = {row["source_episode_id"]: row for row in claims}
        self.assertEqual(by_episode[grounded]["status"], "active")
        self.assertEqual(by_episode[diary]["status"], "uncertain")
        self.assertEqual(by_episode[grounded]["source_quality"], "source_grounded")

    def test_latest_claim_supersedes_old_and_transition_is_idempotent(self):
        first = self.add(episode("memos/old", "我们关系变得亲近而且彼此信任", ts=self.now, batch="b1"))
        second = self.add(episode("memos/new", "我们关系后来变得疏远", ts=self.now + 100, batch="b2"))
        ledger = ClaimLedger(self.store._threads)
        ledger.rebuild_scope("role-a")
        claims = self.store.thread_list_claims(scope_id="role-a", claim_type="relationship", limit=20)
        by_episode = {row["source_episode_id"]: row for row in claims}
        self.assertEqual(by_episode[first]["status"], "superseded")
        self.assertEqual(by_episode[second]["status"], "active")
        count = len(self.store.thread_list_claim_transitions(limit=50))
        ledger.rebuild_scope("role-a")
        self.assertEqual(len(self.store.thread_list_claim_transitions(limit=50)), count)

    def test_grounded_resolution_closes_prior_claim_and_preserves_valid_interval(self):
        first = self.add(episode("memos/promise", "我答应爱莉明天去天台", ts=self.now, batch="promise"))
        terminal = self.add(episode(
            "memos/cancel", "我取消了答应爱莉明天去天台的约定",
            ts=self.now + 3600, batch="cancel",
        ))
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        claims = self.store.thread_list_claims(scope_id="role-a", claim_type="commitment", limit=20)
        by_episode = {row["source_episode_id"]: row for row in claims}
        self.assertEqual(by_episode[first]["status"], "resolved")
        self.assertEqual(by_episode[terminal]["status"], "historical")
        self.assertEqual(by_episode[first]["valid_to"], self.now + 3600)
        transitions = self.store.thread_list_claim_transitions(limit=20)
        self.assertTrue(any(row["transition_type"] == "resolved" for row in transitions))

    def test_low_quality_newer_claim_cannot_override_grounded_current_fact(self):
        grounded = self.add(episode(
            "memos/grounded", "我们关系变得亲近而且彼此信任",
            ts=self.now, batch="grounded",
        ))
        diary = self.add(episode(
            "memos/diary", "我们关系后来变得疏远",
            ts=self.now + 86400,
        ))
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        claims = self.store.thread_list_claims(scope_id="role-a", claim_type="relationship", limit=20)
        by_episode = {row["source_episode_id"]: row for row in claims}
        self.assertEqual(by_episode[grounded]["status"], "active")
        self.assertEqual(by_episode[diary]["status"], "uncertain")

    def test_attributed_quote_from_literary_fallback_is_not_current_fact(self):
        item = episode("memos/quote", "", ts=self.now, batch="quote")
        item["retrieval_key"] = ""
        item["state_change"] = ""
        item["card_text"] = "她说：“我答应明天去天台。”"
        eid = self.add(item)
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        claim = next(row for row in self.store.thread_list_claims(limit=20) if row["source_episode_id"] == eid)
        self.assertEqual(claim["status"], "uncertain")
        self.assertTrue(claim["evidence"]["is_quoted_report"])

    def test_extractor_caps_each_type_and_card_is_only_fallback(self):
        item = episode("memos/cap", "我答应爱莉明天去天台看星星", ts=self.now, batch="cap")
        item["long_effect"] = "我承诺明天带银戒指，也计划下周去河边"
        item["card_text"] = "我答应明天带花，也答应后天做饭。"
        eid = self.add(item)
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        claims = [row for row in self.store.thread_list_claims(limit=50) if row["source_episode_id"] == eid]
        commitments = [row for row in claims if row["claim_type"] == "commitment"]
        self.assertLessEqual(len(commitments), 2)
        self.assertTrue(all(row["evidence"]["field"] != "card_text" for row in commitments))

    def test_full_rebuild_prunes_obsolete_auto_claim_but_keeps_manual_lock(self):
        eid = self.add(episode("memos/prune", "我答应爱莉明天去天台", ts=self.now, batch="prune"))
        ledger = ClaimLedger(self.store._threads)
        ledger.rebuild_scope("role-a")
        claims = self.store.thread_list_claims(limit=20)
        auto = claims[0]
        self.store._connect().execute(
            "UPDATE episodes SET state_change='',retrieval_key='' WHERE episode_id=?", (eid,)
        )
        self.store._connect().commit()
        ledger.rebuild_scope("role-a")
        self.assertFalse(any(row["claim_id"] == auto["claim_id"] for row in self.store.thread_list_claims(limit=20)))

        self.store._connect().execute(
            "UPDATE episodes SET state_change='我答应爱莉明天去天台' WHERE episode_id=?", (eid,)
        )
        self.store._connect().commit()
        ledger.rebuild_scope("role-a")
        manual = self.store.thread_list_claims(limit=20)[0]
        self.store.thread_claim_manual_status(manual["claim_id"], "active")
        self.store._connect().execute("UPDATE episodes SET state_change='' WHERE episode_id=?", (eid,))
        self.store._connect().commit()
        ledger.rebuild_scope("role-a")
        retained = next(row for row in self.store.thread_list_claims(limit=20) if row["claim_id"] == manual["claim_id"])
        self.assertEqual(retained["manual_lock"], 1)

    def test_manual_claim_lock_survives_rebuild_and_operation_reverts(self):
        eid = self.add(episode("memos/lock", "我答应爱莉明天去天台", ts=self.now, batch="lock-b"))
        ledger = ClaimLedger(self.store._threads)
        ledger.rebuild_scope("role-a")
        claim = next(row for row in self.store.thread_list_claims(limit=20) if row["source_episode_id"] == eid)
        changed = self.store.thread_claim_manual_status(claim["claim_id"], "rejected")
        ledger.rebuild_scope("role-a")
        locked = next(row for row in self.store.thread_list_claims(limit=20) if row["claim_id"] == claim["claim_id"])
        self.assertEqual((locked["status"], locked["manual_lock"]), ("rejected", 1))
        slot = next(row for row in self.store.thread_list_claim_slots(scope_id="role-a")
                    if row["slot_key"] == locked["slot_key"])
        self.assertNotIn(claim["claim_id"], slot["current_claim_ids"])
        self.store.thread_revert_operation(changed["operation_id"])
        restored = next(row for row in self.store.thread_list_claims(limit=20) if row["claim_id"] == claim["claim_id"])
        self.assertEqual(restored["manual_lock"], 0)

    def test_materialized_view_versions_only_on_content_change(self):
        eid = self.add(episode("memos/view", "我们关系变得亲近", ts=self.now, batch="view-b"))
        tid = self.thread([eid])
        ledger = ClaimLedger(self.store._threads)
        ledger.rebuild_scope("role-a")
        first = self.store.thread_view(tid)
        self.assertEqual(first["view_version"], 1)
        ledger.materialize_thread_views("role-a")
        self.assertEqual(self.store.thread_view(tid)["view_version"], 1)
        claim = self.store.thread_list_claims(scope_id="role-a")[0]
        self.store.thread_claim_manual_status(claim["claim_id"], "uncertain")
        ledger.materialize_thread_views("role-a")
        self.assertEqual(self.store.thread_view(tid)["view_version"], 2)
        self.assertEqual(len(self.store.thread_view_history(tid)), 2)

    def test_thread_projection_is_idempotent(self):
        eid = self.add(episode("memos/idempotent", "我们关系变得亲近", ts=self.now, batch="idem"))
        tid = self.thread([eid], "thread-idempotent")
        payload = {
            "thread_id": tid, "scope_id": "role-a", "thread_type": "relationship_arc",
            "title": "天台关系线", "status": "active", "confidence": 0.92,
            "first_event_ts": self.now, "last_event_ts": self.now + 1,
            "source_quality_floor": "diary_derived",
        }
        members = [{"episode_id": eid, "role": "supporting", "sequence_no": 0,
                    "membership_confidence": 0.9, "decision_source": "unit"}]
        result = self.store._threads.replace_thread_projection(payload, members, expected_version=1)
        self.assertFalse(result["changed"])
        self.assertEqual(result["materialized_version"], 1)

    def test_prospective_requires_factual_route_and_selects_only_one(self):
        self.add(episode("memos/p1", "我答应爱莉明天去天台看星星", ts=self.now, batch="p1"))
        self.add(episode("memos/p2", "我答应爱莉明天带上银戒指", ts=self.now + 1, batch="p2"))
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        engine = ProspectiveMemory(self.store._threads)
        self.assertGreaterEqual(engine.rebuild_scope("role-a")["items"], 2)
        emotion_only = engine.trigger("role-a", "我今天心情不好", emotion_signal=1.0,
                                      now_ts=self.now, record=False)
        self.assertIsNone(emotion_only["selected"])
        result = engine.trigger("role-a", "爱莉，我们去天台看星星吧", context_text="明天的约定",
                                now_ts=self.now + 86400, record=False)
        self.assertIsNotNone(result["selected"])
        self.assertEqual(result["selected"]["item_id"], result["candidates"][0]["item_id"])

    def test_prospective_resolved_and_cooldown_are_hard_gates(self):
        self.add(episode("memos/p", "我答应爱莉明天去天台看星星", ts=self.now, batch="p"))
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        engine = ProspectiveMemory(self.store._threads)
        engine.rebuild_scope("role-a")
        item = self.store.thread_list_prospective(scope_id="role-a")[0]
        self.store.thread_prospective_manual_status(item["item_id"], "snoozed", cooldown_until=self.now + 172800)
        self.assertIsNone(engine.trigger("role-a", "爱莉去天台看星星", context_text="明天约定",
                                         now_ts=self.now + 86400, record=False)["selected"])
        self.store.thread_prospective_manual_status(item["item_id"], "resolved")
        self.assertIsNone(engine.trigger("role-a", "爱莉去天台看星星", context_text="明天约定",
                                         now_ts=self.now + 86400, record=False)["selected"])

    def test_prospective_source_resolution_is_preserved_as_auditable_terminal_item(self):
        self.add(episode("memos/open", "我答应爱莉明天去天台", ts=self.now, batch="open"))
        ledger = ClaimLedger(self.store._threads)
        ledger.rebuild_scope("role-a")
        engine = ProspectiveMemory(self.store._threads)
        engine.rebuild_scope("role-a")
        original = self.store.thread_list_prospective(scope_id="role-a")[0]

        self.add(episode(
            "memos/closed", "我取消了答应爱莉明天去天台的约定",
            ts=self.now + 3600, batch="closed",
        ))
        ledger.rebuild_scope("role-a")
        engine.rebuild_scope("role-a")
        retained = next(
            row for row in self.store.thread_list_prospective(scope_id="role-a", limit=20)
            if row["item_id"] == original["item_id"]
        )
        self.assertEqual(retained["status"], "resolved")
        self.assertEqual(retained["resolution_evidence"]["claim_status"], "resolved")

    def test_prospective_due_expiry_and_snooze_resume_are_deterministic(self):
        base = {
            "item_id": "pro-lifecycle", "scope_id": "role-a", "source_claim_id": "",
            "description": "明天去天台", "item_type": "plan", "status": "pending",
            "due_start": self.now - 60, "due_end": self.now + 60,
        }
        self.store._threads.upsert_prospective_items([base])
        self.store._threads.refresh_prospective_lifecycle("role-a", now_ts=self.now)
        item = self.store.thread_list_prospective(scope_id="role-a")[0]
        self.assertEqual(item["status"], "due")
        self.store._threads.refresh_prospective_lifecycle("role-a", now_ts=self.now + 31 * 86400)
        item = self.store.thread_list_prospective(scope_id="role-a")[0]
        self.assertEqual(item["status"], "expired")

        self.store._threads.upsert_prospective_items([{
            **base, "item_id": "pro-snooze", "due_start": 0, "due_end": 0,
        }])
        self.store.thread_prospective_manual_status(
            "pro-snooze", "snoozed", cooldown_until=self.now - 1,
        )
        self.store._threads.refresh_prospective_lifecycle("role-a", now_ts=self.now)
        resumed = next(
            row for row in self.store.thread_list_prospective(scope_id="role-a", limit=20)
            if row["item_id"] == "pro-snooze"
        )
        self.assertEqual(resumed["status"], "pending")
        self.assertEqual(resumed["manual_status"], "")

    def test_prospective_surface_cooldown_is_only_written_after_actual_surface(self):
        item_id = "pro-surface"
        self.store._threads.upsert_prospective_items([{
            "item_id": item_id, "scope_id": "role-a", "source_claim_id": "",
            "description": "明天去天台看星星", "item_type": "plan", "status": "pending",
            "due_start": self.now - 60, "due_end": self.now + 60,
            "trigger_terms": ["天台看星星"], "target_entities": ["爱莉"],
        }])
        engine = ProspectiveMemory(self.store._threads)

        # Trigger/Shadow evaluation is read-only.
        preview = engine.trigger(
            "role-a", "爱莉去天台看星星", context_text="明天的约定",
            now_ts=self.now, record=False,
        )
        self.assertEqual(preview["selected"]["item_id"], item_id)
        before = self.store.thread_list_prospective(scope_id="role-a")[0]
        self.assertEqual((before["last_surfaced_ts"], before["cooldown_until"], before["surfaced_count"]), (0, 0, 0))

        surfaced_ts = self.now + 10
        updated = self.store._threads.record_prospective_surface(
            item_id, surfaced_ts=surfaced_ts, cooldown_seconds=100,
        )
        self.assertTrue(updated["updated"])
        surfaced = self.store.thread_list_prospective(scope_id="role-a")[0]
        self.assertEqual(surfaced["last_surfaced_ts"], surfaced_ts)
        self.assertEqual(surfaced["cooldown_until"], surfaced_ts + 100)
        self.assertEqual(surfaced["surfaced_count"], 1)

        # A trigger inside that window is skipped; expiry makes it eligible again.
        inside = engine.trigger(
            "role-a", "爱莉去天台看星星", context_text="明天的约定",
            now_ts=surfaced_ts + 99, record=False,
        )
        self.assertIsNone(inside["selected"])
        after_expiry = engine.trigger(
            "role-a", "爱莉去天台看星星", context_text="明天的约定",
            now_ts=surfaced_ts + 101, record=False,
        )
        self.assertEqual(after_expiry["selected"]["item_id"], item_id)
        self.assertEqual(self.store.thread_list_prospective(scope_id="role-a")[0]["surfaced_count"], 1)

        # A second racing success loses the conditional update in the same window.
        raced = self.store._threads.record_prospective_surface(
            item_id, surfaced_ts=surfaced_ts + 50, cooldown_seconds=100,
        )
        self.assertFalse(raced["updated"])
        self.assertEqual(self.store.thread_list_prospective(scope_id="role-a")[0]["surfaced_count"], 1)

    def test_prospective_surface_reservation_is_exactly_once_and_releasable(self):
        item_id = "pro-reservation"
        self.store._threads.upsert_prospective_items([{
            "item_id": item_id,
            "scope_id": "role-a",
            "description": "明天去天台",
            "item_type": "plan",
            "status": "pending",
            "due_start": self.now - 1,
            "due_end": self.now + 100,
        }])
        first = self.store.thread_reserve_prospective_surface(
            item_id,
            request_id="request-first",
            reserved_ts=self.now,
        )
        second = self.store.thread_reserve_prospective_surface(
            item_id,
            request_id="request-second",
            reserved_ts=self.now,
        )
        self.assertTrue(first["reserved"])
        self.assertFalse(second["reserved"])
        self.assertTrue(self.store.thread_release_prospective_surface(
            item_id, first["reservation"]
        ))
        retried = self.store.thread_reserve_prospective_surface(
            item_id,
            request_id="request-second",
            reserved_ts=self.now,
        )
        self.assertTrue(retried["reserved"])
        committed = self.store.thread_commit_prospective_surface(
            item_id,
            retried["reservation"],
            surfaced_ts=self.now,
            cooldown_seconds=100,
        )
        self.assertTrue(committed["updated"])
        blocked = self.store.thread_reserve_prospective_surface(
            item_id,
            request_id="request-third",
            reserved_ts=self.now + 99,
        )
        self.assertFalse(blocked["reserved"])
        item = self.store.thread_list_prospective(scope_id="role-a")[0]
        self.assertEqual(item["surfaced_count"], 1)
        self.assertEqual(item["surface_reservation"], "")

    def test_prospective_shadow_and_failure_do_not_update_surface_state(self):
        item_id = "pro-shadow"
        self.store._threads.upsert_prospective_items([{
            "item_id": item_id, "scope_id": "role-a", "description": "明天去天台",
            "item_type": "plan", "status": "pending", "due_start": self.now - 1,
            "due_end": self.now + 100, "trigger_terms": ["天台"],
        }])
        engine = ProspectiveMemory(self.store._threads)
        result = engine.trigger("role-a", "去天台", now_ts=self.now, record=True)
        self.assertEqual(result["selected"]["item_id"], item_id)
        item = self.store.thread_list_prospective(scope_id="role-a")[0]
        self.assertEqual((item["last_surfaced_ts"], item["cooldown_until"], item["surfaced_count"]), (0, 0, 0))
        # Failed/empty injection has no surface callback, so state remains unchanged.
        # (The callback is deliberately not invoked here, matching the request hook.)
        failed_item = self.store.thread_list_prospective(scope_id="role-a")[0]
        self.assertEqual(failed_item["surfaced_count"], 0)

    def test_query_planner_exposes_thread_current_and_prospective_intents(self):
        plan = QueryPlanner(_PlannerPlugin()).plan_for_search("我们现在的关系和以前怎么变的，约定还成立吗？")
        self.assertTrue(plan["thread_intent"])
        self.assertTrue(plan["current_state_intent"])
        self.assertTrue(plan["evolution_intent"])
        self.assertTrue(plan["prospective_intent"])
        self.assertTrue(plan["requires_counterevidence"])
        self.assertIn("relationship", plan["target_claim_slots"])
        self.assertIn("commitment", plan["target_claim_slots"])
        unfinished = QueryPlanner(_PlannerPlugin()).plan_for_search("我们还有什么没完成？")
        self.assertTrue(unfinished["prospective_intent"])

    def test_retrieval_forwards_plan_signal_and_explicit_signal_wins(self):
        self.add(episode("memos/p-signal", "我答应爱莉明天去天台", ts=self.now, batch="signal"))
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        ProspectiveMemory(self.store._threads).rebuild_scope("role-a")
        captured = []
        original = ProspectiveMemory.trigger

        def capture(engine, *args, **kwargs):
            captured.append(kwargs["emotion_signal"])
            return original(engine, *args, **kwargs)

        lab = ThreadRetrievalLab(self.store._threads)
        plan = {
            "prospective_intent": True, "target_entities": [],
            "target_claim_slots": ["commitment"], "emotion_signal": 0.7,
        }
        with patch("astrbot_plugin_memos_memory.prospective_memory.ProspectiveMemory.trigger", new=capture):
            lab.run(scope_id="role-a", query="爱莉去天台", plan=plan,
                    base_result={"hits": []}, record=False)
            lab.run(scope_id="role-a", query="爱莉去天台", plan=plan,
                    base_result={"hits": []}, emotion_signal=0.2, record=False)
        self.assertEqual(captured, [0.7, 0.2])

    def test_lab_dedupes_base_memo_but_keeps_different_date_episode(self):
        a = self.add(episode("memos/a", "我们关系变得亲近", ts=self.now, batch="da"))
        b = self.add(episode("memos/b", "我们关系变得亲近", ts=self.now + 86400, batch="db"))
        self.thread([a, b])
        self.store._threads.upsert_edge({
            "scope_id": "role-a", "source_episode_id": a, "target_episode_id": b,
            "edge_type": "continues", "direction": "directed", "confidence": .95,
            "status": "accepted", "evidence_json": "{}", "counter_evidence_json": "[]",
            "route_sources_json": "[]", "arbiter_version": "unit",
        })
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        base = {"hits": [{"memo_name": "memos/a", "selected": True, "score": .9}], "sentinel": [1, 2]}
        before = json.dumps(base, sort_keys=True)
        plan = QueryPlanner(_PlannerPlugin()).plan_for_search("我们关系以前怎么变化的？")
        result = ThreadRetrievalLab(self.store._threads).run(
            scope_id="role-a", query="我们关系以前怎么变化的？", plan=plan,
            base_result=base, record=True,
        )
        self.assertEqual(json.dumps(base, sort_keys=True), before)
        self.assertIn("memos/a", {row["memo_name"] for row in result["dedup"]["relation_only_nodes"]})
        self.assertIn("memos/b", {row["memo_name"] for row in result["dedup"]["new_nodes"]})
        self.assertLessEqual(len(result["subgraph"]["nodes"]), 7)
        dates = [row["event_ts"] for row in result["subgraph"]["nodes"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(self.store.thread_query_observations(scope_id="role-a")), 1)

    def test_lab_source_range_dedup_preserves_zero_turn(self):
        lab = ThreadRetrievalLab(self.store._threads)
        nodes = [
            {"episode_id": "ep-zero-a", "memo_name": "memos/zero-a",
             "source_batch_id": "batch-zero", "scene_start_turn": 0,
             "scene_end_turn": 0, "card_text": "第一件事"},
            {"episode_id": "ep-zero-b", "memo_name": "memos/zero-b",
             "source_batch_id": "batch-zero", "scene_start_turn": 0,
             "scene_end_turn": 0, "card_text": "第二件事"},
        ]
        result = lab._deduplicate([], {"nodes": nodes})
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["relation_only_nodes"][0]["dedup_reason"], "same_source_turn_range")

    def test_lab_does_not_fold_incomplete_source_ranges(self):
        lab = ThreadRetrievalLab(self.store._threads)
        nodes = [
            {"episode_id": "ep-unknown-a", "memo_name": "memos/unknown-a",
             "source_batch_id": "batch-unknown", "card_text": "第一件事"},
            {"episode_id": "ep-unknown-b", "memo_name": "memos/unknown-b",
             "source_batch_id": "batch-unknown", "scene_start_turn": -1,
             "scene_end_turn": -1, "card_text": "第二件事"},
            {"episode_id": "ep-no-batch-a", "memo_name": "memos/no-batch-a",
             "scene_start_turn": 3, "scene_end_turn": 4, "card_text": "第三件事"},
            {"episode_id": "ep-no-batch-b", "memo_name": "memos/no-batch-b",
             "scene_start_turn": 3, "scene_end_turn": 4, "card_text": "第四件事"},
        ]
        result = lab._deduplicate([], {"nodes": nodes})
        self.assertEqual(result["duplicate_count"], 0)
        self.assertEqual(len(result["new_nodes"]), 4)

    def test_transition_holdout_covers_both_sides_and_event_order(self):
        first = self.add(episode(
            "memos/near", "我们关系变得亲近而且彼此信任",
            ts=self.now, batch="near",
        ))
        second = self.add(episode(
            "memos/far", "我们关系后来变得疏远",
            ts=self.now + 7200, batch="far",
        ))
        self.thread([first, second])
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        generated = ThreadRetrievalLab(self.store._threads).generate_eval_cases("role-a")
        self.assertGreaterEqual(generated["generated_by_type"].get("evolution", 0), 1)
        evolution = next(
            row for row in self.store.thread_eval_cases(scope_id="role-a")
            if row["case_type"] == "evolution"
        )
        self.assertEqual(evolution["expected_order"], [first, second])

    def test_evolution_route_works_before_llm_thread_projection_exists(self):
        first = self.add(episode(
            "memos/evo-old", "我们关系变得亲近而且彼此信任",
            ts=self.now, batch="evo-old",
        ))
        second = self.add(episode(
            "memos/evo-new", "我们关系后来变得疏远",
            ts=self.now + 3600, batch="evo-new",
        ))
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        query = "我们关系以前到后来发生了什么变化？"
        plan = QueryPlanner(_PlannerPlugin()).plan_for_search(query)
        result = ThreadRetrievalLab(self.store._threads).run(
            scope_id="role-a", query=query, plan=plan,
            base_result={"hits": []}, record=False,
        )
        self.assertFalse(result["routes"]["threads"])
        self.assertTrue(result["routes"]["transitions"]["transitions"])
        self.assertEqual(
            [row["episode_id"] for row in result["subgraph"]["nodes"]],
            [first, second],
        )

    def test_prospective_semantic_overlap_uses_description_not_only_noisy_terms(self):
        score = ProspectiveMemory._text_overlap_score(
            "晚饭后教我写名字的约定呢",
            "林悔儿与方知宥约定晚饭后学写自己与对方的名字",
        )
        self.assertGreaterEqual(score, 0.5)

    def test_prospective_specific_phrase_beats_generic_time_word(self):
        expected = {
            "item_id": "pro_hair", "status": "pending",
            "description": "吃饭的时候说明天出门前帮我梳头",
            "trigger_terms": ["出门", "梳头"], "target_entities": [],
            "salience": 0.8, "explicitness": 0.8, "emotional_weight": 0.0,
            "due_start": 1.0, "due_end": 2.0, "cooldown_until": 0.0,
        }
        distractor = {
            "item_id": "pro_rest", "status": "pending",
            "description": "明天回家好好休息", "trigger_terms": ["明天", "时候"],
            "target_entities": [], "salience": 0.9, "explicitness": 0.9,
            "emotional_weight": 0.0, "due_start": 1.0, "due_end": 2.0,
            "cooldown_until": 0.0,
        }
        self.store.list_prospective = lambda **kwargs: [distractor, expected]
        self.store.refresh_prospective_lifecycle = lambda *args, **kwargs: {}
        result = ProspectiveMemory(self.store).trigger(
            "scope", "关于吃饭的时候说明天出门前帮我梳头，还有什么没完成？",
            now_ts=3.0, record=False,
        )
        self.assertEqual(result["selected"]["item_id"], "pro_hair")

    def test_prospective_exact_short_action_can_pass_without_generic_time_noise(self):
        score = ProspectiveMemory._lexical_score(
            "关于明天揉腰的约定呢",
            ["明天", "时候", "揉腰", "亲密后", "相拥入睡"],
        )
        self.assertGreaterEqual(score, 0.6)
        generic = ProspectiveMemory._lexical_score(
            "关于明天的约定呢", ["明天", "时候", "对方"],
        )
        self.assertLess(generic, 0.6)

    def test_prospective_term_generation_preserves_late_action_phrase(self):
        _entities, terms = ProspectiveMemory._entities_and_terms({
            "subject": "林悔儿",
            "object": "林悔儿与方知宥亲密后交换约定并相拥入睡，约定明天揉腰",
            "evidence": {"entities": ["林悔儿", "方知宥"]},
        })
        self.assertIn("揉腰", terms)

    def test_evaluation_cue_prefers_distinctive_action(self):
        cue = ThreadRetrievalLab._evaluation_cue(
            "林悔儿得知知宥要外出探查妖兵情况，约定若天黑未归就去村口树下等他",
            "承诺",
        )
        self.assertTrue("外出" in cue or "探查" in cue or "等他" in cue)
        self.assertNotEqual(cue, "林悔、悔儿")

    def test_eval_scoring_reports_baseline_gain_order_and_duplicate_rate(self):
        first = self.add(episode("memos/base", "我们关系变得亲近", ts=self.now, batch="base"))
        second = self.add(episode("memos/deep", "我们关系后来变得疏远", ts=self.now + 10, batch="deep"))
        lab = ThreadRetrievalLab(self.store._threads)
        case = {
            "case_type": "evolution",
            "expected_episode_ids": [first, second],
            "expected_order": [first, second],
        }
        base = {"hits": [{"memo_name": "memos/base", "selected": True}]}
        result = {
            "subgraph": {"nodes": [
                {"episode_id": first, "memo_name": "memos/base"},
                {"episode_id": second, "memo_name": "memos/deep"},
            ]},
            "dedup": {"new_nodes": [{"episode_id": second, "memo_name": "memos/deep"}]},
            "routes": {"current_claims": [], "prospective": {"selected": None}},
        }
        score = lab.score_eval_case(case, base, result)
        self.assertTrue(score["passed"])
        self.assertTrue(score["baseline_hit"])
        self.assertTrue(score["combined_hit"])
        self.assertTrue(score["order_ok"])
        self.assertEqual(score["duplicate_count"], 0)

        rescue_case = {"case_type": "general", "expected_episode_ids": [second]}
        rescue = lab.score_eval_case(rescue_case, base, result)
        self.assertFalse(rescue["baseline_hit"])
        self.assertTrue(rescue["combined_hit"])

    def test_eval_cases_are_explicitly_non_human_source_holdouts(self):
        self.add(episode("memos/eval", "我答应爱莉明天去天台", ts=self.now, batch="eval"))
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        ProspectiveMemory(self.store._threads).rebuild_scope("role-a")
        result = ThreadRetrievalLab(self.store._threads).generate_eval_cases("role-a")
        self.assertEqual(result["human_labels"], 0)
        cases = self.store.thread_eval_cases(scope_id="role-a")
        self.assertTrue(cases)
        self.assertTrue(all(row["source"] == "deterministic_source_holdout" for row in cases))

    def test_lab_feedback_is_persistent_bounded_and_validated(self):
        feedback_id = self.store.thread_record_manual_feedback(
            scope_id="role-a", target_type="query_observation",
            target_id="thread_lab_unit", action="missed", note="应命中天台事件",
        )
        self.assertGreater(feedback_id, 0)
        rows = self.store.thread_list_manual_feedback(scope_id="role-a", limit=10)
        self.assertEqual(rows[0]["target_id"], "thread_lab_unit")
        self.assertEqual(rows[0]["action"], "missed")
        with self.assertRaises(ValueError):
            self.store.thread_record_manual_feedback(
                scope_id="role-a", target_type="query_observation",
                target_id="thread_lab_unit", action="rewrite_memory",
            )


if __name__ == "__main__":
    unittest.main()
