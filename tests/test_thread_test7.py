# -*- coding: utf-8 -*-
"""6.0.0-test7 deterministic Canary and context-composer regression tests."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_memos_memory.claim_ledger import ClaimLedger
from astrbot_plugin_memos_memory.context_composer import (
    ThreadContextComposer,
    stable_canary_decision,
)
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
from astrbot_plugin_memos_memory.query_planner import QueryPlanner
from astrbot_plugin_memos_memory.thread_integration import ThreadIntegrationMixin


def node(name: str, eid: str, ts: float, text: str) -> dict:
    return {
        "memo_name": name,
        "episode_id": eid,
        "occurred_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
        "event_ts": ts,
        "memory_type": "relationship",
        "state_change": text,
        "retrieval_key": text,
        "card_text": text,
    }


class CanaryDecisionTests(unittest.TestCase):
    def test_same_session_is_stable_and_seed_changes_distribution(self):
        first = stable_canary_decision(
            scope_id="role", session_id="session-a", seed="seed-a", percent=17, mode="canary"
        )
        second = stable_canary_decision(
            scope_id="role", session_id="session-a", seed="seed-a", percent=17, mode="canary"
        )
        changed = stable_canary_decision(
            scope_id="role", session_id="session-a", seed="seed-b", percent=17, mode="canary"
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first["bucket"], changed["bucket"])

    def test_distribution_and_override_priority(self):
        selected = sum(stable_canary_decision(
            scope_id="role", session_id=f"session-{index}", seed="stable", percent=5, mode="canary"
        )["selected"] for index in range(4000))
        self.assertGreater(selected, 140)
        self.assertLess(selected, 260)
        allowed = stable_canary_decision(
            scope_id="role", session_id="manual", seed="stable", percent=0, mode="canary",
            allowlist="manual", denylist="",
        )
        denied = stable_canary_decision(
            scope_id="role", session_id="manual", seed="stable", percent=100, mode="canary",
            allowlist="manual", denylist="manual",
        )
        shadow = stable_canary_decision(
            scope_id="role", session_id="manual", seed="stable", percent=100, mode="shadow",
            allowlist="manual",
        )
        self.assertTrue(allowed["selected"])
        self.assertEqual((denied["selected"], denied["reason"]), (False, "denylist"))
        self.assertFalse(shadow["selected"])


class ContextComposerTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.plan = {
            "thread_intent": True,
            "current_state_intent": True,
            "evolution_intent": True,
            "prospective_intent": True,
        }

    def result(self) -> dict:
        older = node("memos/old", "ep_old_secret", self.now - 86400 * 10, "最初我们彼此还有些疏远")
        newer = node("memos/new", "ep_new_secret", self.now - 86400, "后来我们愿意更加信任彼此")
        return {
            "routes": {
                "current_claims": [
                    {"claim_id": "clm_secret", "status": "active", "slot_key": "relationship:us", "object": "目前彼此信任，但仍尊重对方的边界"},
                    {"claim_id": "clm_counter", "status": "superseded", "counterevidence": True, "object": "最初彼此疏远"},
                ],
                "transitions": {"transitions": [{
                    "from_object": "最初彼此疏远", "to_object": "后来逐渐建立信任",
                    "transition_type": "supersedes",
                }]},
                "prospective": {"selected": {
                    "item_id": "pro_secret", "description": "下次继续谈那次没有说完的约定",
                    "due_start": self.now + 86400,
                }},
            },
            "dedup": {"new_nodes": [newer, older], "duplicate_count": 1},
        }

    def test_compact_block_has_no_internal_ids_or_algorithm_fields(self):
        result = ThreadContextComposer(max_chars=1400, growth_percent=10).compose(
            self.result(), plan=self.plan, base_memory_chars=9000, preexisting_chars=12000,
        )
        self.assertTrue(result.text)
        self.assertIn("当前现实时间只以 CurrentTimeContext 为准", result.text)
        self.assertIn("[当前仍有效的事实]", result.text)
        self.assertIn("[相关经历脉络，按事件发生时间]", result.text)
        self.assertIn("[尚待发生或解决的事项]", result.text)
        self.assertLess(result.text.index("最初我们"), result.text.index("后来我们"))
        for leaked in ("ep_old_secret", "clm_secret", "pro_secret", "selection_score", "algorithm"):
            self.assertNotIn(leaked, result.text)
        self.assertLessEqual(len(result.text), result.metrics["allowed_chars"])
        self.assertEqual(result.metrics["cross_layer_duplicates_removed"], 1)
        self.assertEqual(
            {item["category"] for item in result.evidence},
            {"claim", "episode", "transition", "prospective"},
        )
        self.assertTrue(all(item["text"] in result.text for item in result.evidence))
        self.assertTrue(all(item.get("source") for item in result.evidence))

    def test_unrelated_and_conflicting_claims_fail_closed_to_empty(self):
        unrelated = ThreadContextComposer().compose(
            {"routes": {}, "dedup": {}}, plan={}, base_memory_chars=5000, preexisting_chars=5000,
        )
        self.assertEqual((unrelated.text, unrelated.metrics["reason"]), ("", "not_thread_relevant"))
        conflict = self.result()
        conflict["routes"]["current_claims"].append({
            "status": "active", "slot_key": "relationship:us", "object": "目前彼此完全疏远",
        })
        guarded = ThreadContextComposer().compose(
            conflict, plan=self.plan, base_memory_chars=8000, preexisting_chars=8000,
        )
        self.assertEqual((guarded.text, guarded.metrics["reason"]), ("", "claim_conflict"))

    def test_supporting_nodes_trim_before_protected_facts(self):
        payload = self.result()
        payload["dedup"]["new_nodes"] = [
            node(f"memos/{i}", f"ep_{i}", self.now - (8 - i) * 3600, "一段较长的独立经历细节" * 16)
            for i in range(7)
        ]
        composed = ThreadContextComposer(max_chars=700, growth_percent=10).compose(
            payload, plan=self.plan, base_memory_chars=6000, preexisting_chars=6000,
        )
        self.assertTrue(composed.text)
        self.assertIn("目前彼此信任", composed.text)
        self.assertIn("下次继续谈", composed.text)
        self.assertIn("supporting_node", composed.metrics["trimmed"])


class QueryEmotionPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = SimpleNamespace(
            query_plan_enable=True,
            query_plan_context_window=8,
            character_name="role-a",
        )
        self.planner = QueryPlanner(self.plugin)

    def test_only_current_turn_emotion_affects_signal(self):
        neutral = self.planner.rewrite(
            "那件事后来怎么样",
            [{"role": "user", "content": "我当时又难过又害怕"}],
        )
        self.assertIsNotNone(neutral)
        self.assertEqual(neutral.emotion_cues, [])
        self.assertEqual(neutral.emotion_signal, 0.0)

        current = self.planner.rewrite(
            "我现在又难过又害怕",
            [{"role": "user", "content": "以前很安心"}],
        )
        self.assertIsNotNone(current)
        self.assertEqual(current.emotion_cues, ["难过", "害怕"])
        self.assertAlmostEqual(current.emotion_signal, 0.42)

    async def test_llm_disambiguation_recomputes_from_current_turn_only(self):
        from astrbot_plugin_memos_memory.query_planner import QueryPlan

        plan = QueryPlan(
            intent="specific",
            search_text="我很难过",
            use_context=False,
            context_used_reason="specific_query",
            confidence=0.1,
            raw_standalone="我很难过",
            emotion_cues=["难过"],
            emotion_signal=0.28,
        )

        async def caller(_prompt):
            return json.dumps({
                "standalone_query": "我很难过，也想起上文的害怕",
                "resolved_entities": [],
                "relation_cues": [],
                "emotion_cues": ["难过", "害怕"],
                "temporal_constraints": [],
                "intent": "specific",
                "confidence": 0.8,
            }, ensure_ascii=False)

        updated = await self.planner.maybe_llm_disambiguate(
            plan, "上文说害怕", caller, lambda *_: "prompt",
        )
        self.assertEqual(updated.emotion_cues, ["难过"])
        self.assertEqual(updated.emotion_signal, 0.28)

    def test_xinchao_and_planner_signal_use_bounded_maximum(self):
        plugin = object.__new__(MemosMemoryPlugin)
        event = SimpleNamespace(get_extra=lambda key, default=None: {
            "activationLevels": {
                "monitor": "0.72",
                "protect": float("nan"),
                "bad": "not-a-number",
                "overflow": 1.4,
            }
        } if key == "xinchao_live_appraisal" else default)
        self.assertEqual(plugin._request_emotion_signal(event), 1.0)
        self.assertEqual(
            plugin._prospective_emotion_signal(
                event, {"emotion_signal": 0.45}
            ),
            1.0,
        )

        quiet = SimpleNamespace(
            get_extra=lambda _key, default=None: default
        )
        self.assertEqual(
            plugin._prospective_emotion_signal(
                quiet, {"emotion_signal": float("inf")}
            ),
            0.0,
        )
        self.assertEqual(
            plugin._prospective_emotion_signal(
                quiet, {"emotion_signal": 0.65}
            ),
            0.65,
        )


class _CanaryPlugin(ThreadIntegrationMixin):
    def __init__(self, store: EpisodicStore):
        self._episodes = store
        self.character_name = "role-a"
        self.thread_memory_enable = True
        self.thread_mode = "canary"
        self.thread_canary_percent = 100
        self.thread_canary_seed = "unit"
        self.thread_canary_allowlist = ""
        self.thread_canary_denylist = ""
        self.thread_canary_max_chars = 1200
        self.thread_canary_growth_percent = 10
        self.thread_canary_timeout_ms = 1000
        self.thread_canary_require_current_time = True
        self.thread_retrieval_lab_enable = True
        self.thread_prospective_enable = True
        self.rp_time_timezone = "Asia/Shanghai"
        self._query_planner = QueryPlanner(self)

    def _request_now(self):
        import datetime
        return datetime.datetime.now()

    async def _eval_recall_query(self, _query):
        raise AssertionError("test7 online path must not rerun mature recall")


class CanaryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.temp.name) / "memory.db"), 3, "unit")
        await self.store.init()
        now = time.time()
        episode = {
            "occurred_at": "2026-08-10 20:00", "event_ts": now - 86400,
            "time_basis": "source_turn", "memory_type": "relationship", "importance": 4,
            "scene_anchor": "天台", "retrieval_key": "我们关系变得亲近而且彼此信任",
            "state_change": "我们关系变得亲近而且彼此信任", "long_effect": "",
            "trigger_hint": "关系 信任", "entities": ["爱莉"], "unresolved": [],
        }
        conn = self.store._connect()
        conn.execute(
            """INSERT INTO source_batches(batch_id,session_id,source_kind,content_hash,
               message_count,first_event_ts,last_event_ts,status,created_ts,updated_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("batch-a", "session-a", "test", "batch-a", 1, now - 86400, now - 86400,
             "archived", now, now),
        )
        conn.commit()
        self.store.upsert_episode(
            memo_name="memos/relationship", episode=episode, card_text="我记得我们逐渐建立了信任。",
            embedding=[1.0, 0.0, 0.0], evidence_quality="source_grounded", source_batch_id="batch-a",
        )
        eid = str(self.store.get_episode("memos/relationship")["episode_id"])
        self.store.thread_enqueue("role-a", eid)
        ClaimLedger(self.store._threads).rebuild_scope("role-a")
        self.plugin = _CanaryPlugin(self.store)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def call(self, **overrides):
        args = {
            "query": "我们现在的关系还是亲近吗",
            "context_text": "",
            "selected_hits": [{"memo_name": "memos/relationship", "selected": True}],
            "query_plan": {
                "thread_intent": True, "current_state_intent": True,
                "evolution_intent": False, "prospective_intent": False,
                "target_entities": [], "target_claim_slots": ["relationship"],
                "requires_counterevidence": True,
            },
            "scope_id": "role-a", "session_id": "session-a", "request_id": "request-a",
            "current_time_valid": True, "base_memory_chars": 6000,
            "preexisting_chars": 12000, "now_ts": time.time(),
        }
        args.update(overrides)
        return await self.plugin._compose_thread_canary_for_request(**args)

    async def test_online_canary_reuses_selected_hits_and_persists_observation(self):
        result = await self.call()
        self.assertTrue(result["text"])
        self.assertTrue(result["metrics"]["selected_for_append"])
        self.assertFalse(result["metrics"]["injected"])
        self.assertFalse(result["metrics"]["append_confirmed"])
        self.assertEqual(result["metrics"]["append_status"], "pending")
        self.assertEqual(result["metrics"]["extra_recall_calls"], 0)
        self.assertTrue(result["evidence"])
        self.assertTrue(
            all(item["text"] in result["text"] for item in result["evidence"])
        )
        rows = self.store.thread_query_observations(scope_id="role-a", limit=10)
        self.assertEqual(rows[0]["request_id"], "request-a")
        self.assertFalse(rows[0]["metrics"]["injected"])
        self.assertEqual(rows[0]["metrics"]["append_status"], "pending")
        self.assertEqual(rows[0]["evidence"], result["evidence"])

    async def test_shadow_and_time_health_gate_never_return_text(self):
        self.plugin.thread_mode = "shadow"
        shadow = await self.call(request_id="request-shadow")
        self.assertEqual(shadow["text"], "")
        self.assertFalse(shadow["metrics"]["injected"])
        self.plugin.thread_mode = "canary"
        missing = await self.call(request_id="request-time", current_time_valid=False)
        self.assertEqual(missing["text"], "")
        self.assertEqual(missing["metrics"]["reason"], "current_time_missing")

    async def test_canary_forwards_plan_emotion_signal_and_explicit_value_wins(self):
        captured = []
        trigger_captured = []
        retrieval_module = __import__(
            "astrbot_plugin_memos_memory.thread_retrieval", fromlist=["ThreadRetrievalLab"]
        )
        prospective_module = __import__(
            "astrbot_plugin_memos_memory.prospective_memory", fromlist=["ProspectiveMemory"]
        )
        original_run = retrieval_module.ThreadRetrievalLab.run
        original_trigger = prospective_module.ProspectiveMemory.trigger

        def capture_run(lab, **kwargs):
            captured.append(kwargs["emotion_signal"])
            return original_run(lab, **kwargs)

        def capture_trigger(engine, *args, **kwargs):
            trigger_captured.append(kwargs["emotion_signal"])
            return original_trigger(engine, *args, **kwargs)

        with patch(
            "astrbot_plugin_memos_memory.thread_retrieval.ThreadRetrievalLab.run",
            new=capture_run,
        ), patch(
            "astrbot_plugin_memos_memory.prospective_memory.ProspectiveMemory.trigger",
            new=capture_trigger,
        ):
            await self.call(
                request_id="request-plan-emotion",
                query_plan={
                    "thread_intent": True, "current_state_intent": False,
                    "evolution_intent": False, "prospective_intent": True,
                    "target_entities": [], "target_claim_slots": ["commitment"],
                    "requires_counterevidence": False, "emotion_signal": 0.65,
                },
            )
            await self.call(
                request_id="request-explicit-emotion", emotion_signal=0.25,
                query_plan={"prospective_intent": True, "emotion_signal": 0.9},
            )
        self.assertEqual(captured, [0.65, 0.25])
        self.assertEqual(trigger_captured, [0.65, 0.25])

    async def test_prospective_switch_blocks_stale_online_rows(self):
        self.plugin.thread_prospective_enable = False
        with patch(
            "astrbot_plugin_memos_memory.prospective_memory.ProspectiveMemory.trigger"
        ) as trigger:
            result = await self.call(
                request_id="request-disabled-prospective",
                query_plan={
                    "thread_intent": False,
                    "current_state_intent": False,
                    "evolution_intent": False,
                    "prospective_intent": True,
                    "target_entities": [],
                    "target_claim_slots": ["commitment"],
                },
            )
        trigger.assert_not_called()
        self.assertFalse(
            any(item.get("category") == "prospective" for item in result["evidence"])
        )

    async def test_shadow_never_returns_candidate_evidence(self):
        self.plugin.thread_mode = "shadow"
        result = await self.call(request_id="request-shadow-evidence")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["evidence"], [])
        rows = self.store.thread_query_observations(scope_id="role-a", limit=10)
        observation = next(
            row for row in rows if row["request_id"] == "request-shadow-evidence"
        )
        self.assertTrue(observation["evidence"])
        self.assertFalse(observation["metrics"]["injected"])

    async def test_canary_old_call_defaults_emotion_signal_to_zero(self):
        captured = []
        original_run = __import__(
            "astrbot_plugin_memos_memory.thread_retrieval", fromlist=["ThreadRetrievalLab"]
        ).ThreadRetrievalLab.run

        def capture_run(lab, **kwargs):
            captured.append(kwargs["emotion_signal"])
            return original_run(lab, **kwargs)

        with patch(
            "astrbot_plugin_memos_memory.thread_retrieval.ThreadRetrievalLab.run",
            new=capture_run,
        ):
            await self.call(request_id="request-legacy")
        self.assertEqual(captured, [0.0])


if __name__ == "__main__":
    unittest.main()
