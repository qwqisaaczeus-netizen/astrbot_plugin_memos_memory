from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.thread_integration import ThreadIntegrationMixin
from astrbot_plugin_memos_memory.thread_policy import PRESETS, fingerprint, gates, switch


ROOT = Path(__file__).resolve().parents[1]


class _Config(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1


class RC1PolicyTests(unittest.TestCase):
    def test_fresh_install_collects_locally_in_shadow(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIs(schema["thread_memory_enable"]["default"], True)
        self.assertEqual(schema["thread_mode"]["default"], "shadow")
        self.assertEqual(schema["thread_observation_retention_days"]["default"], 180)

    def test_all_presets_are_operational_without_touching_identity_or_providers(self):
        for values in PRESETS.values():
            self.assertIs(values["thread_memory_enable"], True)
            self.assertIs(values["thread_worker_enable"], True)
            self.assertIs(values["thread_projection_enable"], True)
            self.assertIs(values["thread_claim_enable"], True)
            self.assertNotIn("thread_llm_provider_id", values)

    def test_manual_canary_switch_enables_master_and_worker_and_hot_applies(self):
        config = _Config({
            "thread_memory_enable": False,
            "thread_worker_enable": False,
            "thread_mode": "shadow",
            "thread_canary_percent": 10,
            "consistency_mode": "shadow",
        })
        plugin = SimpleNamespace(
            config=config,
            thread_memory_enable=False,
            thread_worker_enable=False,
            thread_mode="shadow",
            thread_canary_percent=10,
            consistency_mode="shadow",
            _episodes=None,
            lifecycle_calls=0,
        )

        def apply_runtime():
            plugin.lifecycle_calls += 1
            return True

        plugin._apply_thread_runtime_policy = apply_runtime
        result = switch(plugin, {"thread_mode": "canary"}, source="test", manual=True)
        self.assertEqual(result["changed"]["thread_mode"], "canary")
        self.assertIs(result["changed"]["thread_memory_enable"], True)
        self.assertIs(result["changed"]["thread_worker_enable"], True)
        self.assertEqual(plugin.lifecycle_calls, 1)
        self.assertEqual(config.saved, 1)

    def test_worker_only_uses_catchup_cadence_for_a_healthy_backlog(self):
        delay = ThreadIntegrationMixin._thread_worker_next_delay
        self.assertEqual(delay(300, processed=10, batch_size=10, errors=0, pending=90), 2.0)
        self.assertEqual(delay(300, processed=9, batch_size=10, errors=0, pending=90), 300.0)
        self.assertEqual(delay(300, processed=10, batch_size=10, errors=1, pending=90), 300.0)
        self.assertEqual(delay(300, processed=10, batch_size=10, errors=0, pending=0), 300.0)

    def test_threads_webui_exposes_collection_retention_without_provider_text_input(self):
        html = (ROOT / "threads.html").read_text(encoding="utf-8")
        self.assertIn('id="policy-retention"', html)
        self.assertIn("thread_observation_retention_days:Number", html)
        self.assertIn("不自动上传", html)

    def test_gate_samples_are_isolated_to_the_current_character_scope(self):
        class _Episodes:
            def __init__(self, rows):
                self.rows = rows
                self.calls = []

            def thread_query_observations(self, **kwargs):
                self.calls.append(kwargs)
                scope_id = kwargs.get("scope_id")
                return [row for row in self.rows if row["scope_id"] == scope_id]

        config = dict(PRESETS["balanced"])
        plugin = SimpleNamespace(config=config, character_name="role-a")
        for key, value in config.items():
            setattr(plugin, key, value)
        policy = fingerprint(plugin)
        now = __import__("time").time()
        plugin._episodes = _Episodes([
            {"scope_id": "role-a", "created_ts": now, "metrics": {"mode": "canary", "policy_fingerprint": policy, "injected": True, "extra_llm_calls": 0, "extra_recall_calls": 0}},
            {"scope_id": "role-b", "created_ts": now, "metrics": {"mode": "canary", "policy_fingerprint": policy, "injected": True, "extra_llm_calls": 0, "extra_recall_calls": 0}},
        ])

        result = gates(plugin)
        self.assertEqual(result["scope_id"], "role-a")
        self.assertEqual(result["online_samples"], 1)
        self.assertEqual(result["online_injected"], 1)
        self.assertEqual(plugin._episodes.calls, [{"scope_id": "role-a", "limit": 500}])


class RC1RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_hot_policy_start_and_stop_leaves_no_worker_task(self):
        class _Plugin(ThreadIntegrationMixin):
            def __init__(self, loop):
                self.thread_memory_enable = True
                self.thread_worker_enable = True
                self._episodes = object()
                self._terminating = False
                self._thread_build_task = None
                self._webui = SimpleNamespace(_loop=loop)
                self.started = __import__("asyncio").Event()
                self.stopped = __import__("asyncio").Event()

            async def _thread_build_loop(self):
                self.started.set()
                try:
                    await __import__("asyncio").Event().wait()
                finally:
                    self.stopped.set()

        asyncio = __import__("asyncio")
        plugin = _Plugin(asyncio.get_running_loop())
        self.assertTrue(plugin._apply_thread_runtime_policy())
        await asyncio.wait_for(plugin.started.wait(), timeout=1)
        self.assertIsNotNone(plugin._thread_build_task)

        plugin.thread_worker_enable = False
        self.assertTrue(plugin._apply_thread_runtime_policy())
        await asyncio.wait_for(plugin.stopped.wait(), timeout=1)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertIsNone(plugin._thread_build_task)


class RC1ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EpisodicStore(str(Path(self.tmp.name) / "ep.db"), 3, "unit")
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_injection_is_counted_only_after_append_confirmation(self):
        thread_store = self.store._threads
        thread_store.record_thread_query_observation({
            "request_id": "req-ok",
            "scope_id": "role",
            "query_text": "还记得吗",
            "evidence": [{"text": "candidate"}],
            "injection_preview": "candidate",
            "metrics": {
                "mode": "canary",
                "selected": True,
                "selected_for_append": True,
                "would_inject": True,
                "injected": False,
                "append_confirmed": False,
                "append_status": "pending",
            },
        })
        before = thread_store.list_thread_query_observations(limit=1)[0]
        self.assertIs(before["metrics"]["injected"], False)
        self.assertIs(before["metrics"]["append_confirmed"], False)
        self.assertEqual(before["shadow"], 1)

        self.assertTrue(thread_store.finalize_thread_query_observation(
            "req-ok",
            injected=True,
            append_status="appended",
            actual_text="visible block",
            actual_evidence=[{"text": "visible block"}],
        ))
        after = thread_store.list_thread_query_observations(limit=1)[0]
        self.assertIs(after["metrics"]["injected"], True)
        self.assertIs(after["metrics"]["append_confirmed"], True)
        self.assertEqual(after["metrics"]["append_status"], "appended")
        self.assertEqual(after["injection_preview"], "visible block")
        self.assertEqual(after["evidence"], [{"text": "visible block"}])
        self.assertEqual(after["shadow"], 0)

    async def test_append_failure_never_becomes_a_real_injection(self):
        thread_store = self.store._threads
        thread_store.record_thread_query_observation({
            "request_id": "req-fail",
            "metrics": {"mode": "canary", "selected": True, "injected": False},
        })
        self.assertTrue(thread_store.finalize_thread_query_observation(
            "req-fail", injected=False, append_status="append_failed"
        ))
        row = thread_store.list_thread_query_observations(limit=1)[0]
        self.assertIs(row["metrics"]["injected"], False)
        self.assertIs(row["metrics"]["append_confirmed"], False)
        self.assertEqual(row["metrics"]["outcome"], "fail_open")
        self.assertEqual(row["shadow"], 1)


if __name__ == "__main__":
    unittest.main()
