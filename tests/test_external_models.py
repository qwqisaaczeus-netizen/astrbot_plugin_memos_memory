from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_memos_memory.external_models import ExternalModelRegistry
from astrbot_plugin_memos_memory.llm_runtime import PluginLLMRuntime
from astrbot_plugin_memos_memory.main import MemosMemoryPlugin


def _entry(model_id: str, *, name: str, key: str = "secret") -> dict:
    return {
        "id": model_id,
        "name": name,
        "base_url": "https://example.invalid/v1",
        "model": name.lower(),
        "api_key": key,
        "enabled": True,
        "priority": 10,
        "max_retries": 0,
        "timeout_seconds": 20,
        "temperature": 0.1,
        "max_output_tokens": 8192,
    }


class _FakeClient:
    def __init__(self, *, result: str = "", error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(completion_text=self.result)

    async def close(self):
        return None


class ExternalModelRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = ExternalModelRegistry(Path(self.temp.name))

    async def asyncTearDown(self):
        await self.registry.close()
        self.temp.cleanup()

    async def test_keys_are_separate_and_never_returned(self):
        payload = self.registry.save({
            "enabled": True,
            "fallback_enabled": True,
            "default_model_id": "model_a",
            "models": [_entry("model_a", name="Fast JSON", key="top-secret")],
        })
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["models"][0]["has_api_key"])
        self.assertEqual(payload["models"][0]["api_key"], "")
        state_text = (Path(self.temp.name) / "external_models.json").read_text("utf-8")
        self.assertNotIn("top-secret", state_text)
        self.assertTrue((Path(self.temp.name) / "external_model_secrets" / "model_a.json").exists())

    async def test_blank_key_preserves_saved_secret(self):
        self.registry.save({
            "enabled": True,
            "default_model_id": "model_a",
            "models": [_entry("model_a", name="A", key="keep-me")],
        })
        updated = _entry("model_a", name="Renamed", key="")
        self.registry.save({
            "enabled": True,
            "default_model_id": "model_a",
            "models": [updated],
        })
        self.assertEqual(self.registry._secret("model_a").load(), "keep-me")

    async def test_enabled_pool_requires_a_complete_model(self):
        with self.assertRaisesRegex(ValueError, "至少需要一个"):
            self.registry.save({"enabled": True, "models": []})

    async def test_selected_model_fails_over_inside_one_call(self):
        first = _entry("model_a", name="A", key="a")
        second = _entry("model_b", name="B", key="b")
        second["priority"] = 20
        self.registry.save({
            "enabled": True,
            "fallback_enabled": True,
            "default_model_id": "model_a",
            "models": [first, second],
        })
        bad = _FakeClient(error=asyncio.TimeoutError())
        good = _FakeClient(result='{"ok":true}')
        self.registry._clients = {"model_a": bad, "model_b": good}
        result = await self.registry.text_chat(
            "external:model_a",
            prompt="test", contexts=[], system_prompt="", timeout=5,
            max_tokens=64, request_max_retries=0,
        )
        self.assertEqual(result.completion_text, '{"ok":true}')
        self.assertEqual(len(bad.calls), 1)
        self.assertEqual(len(good.calls), 1)
        health = self.registry.payload()["models"]
        self.assertEqual(health[0]["health"]["failures"], 1)
        self.assertEqual(health[1]["health"]["successes"], 1)

    async def test_per_model_output_limit_is_applied(self):
        item = _entry("model_a", name="A", key="a")
        item["max_output_tokens"] = 4096
        self.registry.save({
            "enabled": True,
            "default_model_id": "model_a",
            "models": [item],
        })
        client = _FakeClient(result="ok")
        self.registry._clients = {"model_a": client}
        await self.registry.text_chat(
            "model_a", prompt="test", contexts=[], system_prompt="", timeout=5,
            max_tokens=20000, request_max_retries=0,
        )
        self.assertEqual(client.calls[0]["max_tokens"], 4096)

    async def test_diary_task_can_use_its_full_deadline(self):
        item = _entry("model_a", name="A", key="a")
        item["timeout_seconds"] = 20
        self.registry.save({
            "enabled": True,
            "default_model_id": "model_a",
            "models": [item],
        })
        client = _FakeClient(result="ok")
        self.registry._clients = {"model_a": client}
        await self.registry.text_chat(
            "model_a", prompt="diary", contexts=[], system_prompt="",
            timeout=120, max_tokens=8192, request_max_retries=1,
            task_label="diary_render",
        )
        self.assertGreater(client.calls[0]["timeout"], 100)

    async def test_retry_count_is_capped_to_one_physical_retry(self):
        item = _entry("model_a", name="A", key="a")
        item["max_retries"] = 99
        payload = self.registry.save({
            "enabled": True,
            "default_model_id": "model_a",
            "models": [item],
        })
        self.assertEqual(payload["models"][0]["max_retries"], 1)

    async def test_failed_state_write_restores_previous_secret_and_memory_state(self):
        self.registry.save({
            "enabled": True,
            "default_model_id": "model_a",
            "models": [_entry("model_a", name="A", key="keep-me")],
        })
        original_write = self.registry._write_locked

        def fail_write():
            raise OSError("disk full")

        self.registry._write_locked = fail_write
        changed = _entry("model_a", name="Changed", key="replace-me")
        with self.assertRaisesRegex(OSError, "disk full"):
            self.registry.save({
                "enabled": False,
                "default_model_id": "model_a",
                "models": [changed],
            })
        self.registry._write_locked = original_write
        self.assertTrue(self.registry.enabled)
        self.assertEqual(self.registry.model("model_a")["name"], "A")
        self.assertEqual(self.registry._secret("model_a").load(), "keep-me")

    async def test_concurrent_saves_do_not_mix_model_state_and_secret(self):
        first = {
            "enabled": True,
            "default_model_id": "model_a",
            "models": [_entry("model_a", name="A", key="key-a")],
        }
        second = {
            "enabled": True,
            "default_model_id": "model_b",
            "models": [_entry("model_b", name="B", key="key-b")],
        }
        await asyncio.gather(
            asyncio.to_thread(self.registry.save, first),
            asyncio.to_thread(self.registry.save, second),
        )
        payload = self.registry.payload()
        self.assertEqual(len(payload["models"]), 1)
        model_id = payload["models"][0]["id"]
        self.assertEqual(payload["default_model_id"], model_id)
        self.assertTrue(payload["models"][0]["has_api_key"])
        self.assertEqual(
            self.registry._secret(model_id).load(),
            {"model_a": "key-a", "model_b": "key-b"}[model_id],
        )

    async def test_provider_option_ids_are_external_and_default_is_first(self):
        self.registry.save({
            "enabled": True,
            "default_model_id": "model_b",
            "models": [
                _entry("model_a", name="A", key="a"),
                _entry("model_b", name="B", key="b"),
            ],
        })
        options = self.registry.provider_options()
        self.assertEqual(options[0]["value"], "")
        self.assertTrue(all(
            not item["value"] or item["value"].startswith("external:")
            for item in options
        ))
        self.assertIn("默认", next(item["label"] for item in options if item["value"] == "external:model_b"))


class LLMRuntimeExternalDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_passes_remaining_timeout_to_direct_provider(self):
        class Provider:
            def __init__(self):
                self.timeout = None

            def meta(self):
                return SimpleNamespace(id="external:test")

            async def text_chat(self, *, prompt, contexts, system_prompt, timeout, request_max_retries):
                self.timeout = timeout
                return SimpleNamespace(completion_text="ok")

        runtime = PluginLLMRuntime()
        provider = Provider()
        result = await runtime.call(provider, prompt="x", timeout=2, label="episode_extract")
        self.assertEqual(result.completion_text, "ok")
        self.assertGreater(provider.timeout, 0)
        self.assertLessEqual(provider.timeout, 2)
        await runtime.close()


class PluginProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_pool_precedes_old_astr_selection_without_overwriting_it(self):
        class Context:
            def __init__(self):
                self.astr = object()
                self.requested = ""

            def get_provider_by_id(self, provider_id):
                self.requested = provider_id
                return self.astr

            def get_using_provider(self, *args):
                return self.astr

        with tempfile.TemporaryDirectory() as temp:
            registry = ExternalModelRegistry(Path(temp))
            registry.save({
                "enabled": True,
                "default_model_id": "model_a",
                "models": [_entry("model_a", name="A", key="a")],
            })
            plugin = MemosMemoryPlugin.__new__(MemosMemoryPlugin)
            plugin.context = Context()
            plugin._external_models = registry
            selected = plugin._resolve_chat_provider("old-astr-provider")
            self.assertEqual(selected.provider_id, "external:model_a")
            self.assertEqual(plugin.context.requested, "")
            registry.enabled = False
            self.assertIs(
                plugin._resolve_chat_provider("old-astr-provider"),
                plugin.context.astr,
            )
            self.assertEqual(plugin.context.requested, "old-astr-provider")
            await registry.close()


class ExternalModelsPageTests(unittest.TestCase):
    def test_page_has_secret_safe_multi_model_controls(self):
        text = (Path(__file__).resolve().parents[1] / "models.html").read_text("utf-8")
        self.assertIn("/api/models/save", text)
        self.assertIn("/api/models/test", text)
        self.assertIn("新增模型", text)
        self.assertIn("外置模型池", text)
        self.assertIn("max_output_tokens", text)
        self.assertIn("toggleModel", text)
        self.assertNotIn("value=\"top-secret\"", text)


if __name__ == "__main__":
    unittest.main()
