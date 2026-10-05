from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from astrbot_plugin_memos_memory.llm_runtime import (
    LLMCircuitOpenError,
    LLMEmptyFinalError,
    LLMQueueTimeoutError,
    LLMRuntimeClosedError,
    PluginLLMRuntime,
)


class ExplicitRetryProvider:
    def __init__(self, provider_id: str = "explicit") -> None:
        self.provider_id = provider_id
        self.calls = 0
        self.request_retries: list[int | None] = []
        self.fail_with: BaseException | None = None
        self.delay = 0.0
        self.order: list[str] = []
        self.block_started: asyncio.Event | None = None
        self.block_release: asyncio.Event | None = None

    def meta(self):
        return SimpleNamespace(id=self.provider_id)

    async def text_chat(
        self,
        *,
        prompt,
        contexts,
        system_prompt,
        request_max_retries=None,
    ):
        self.calls += 1
        self.request_retries.append(request_max_retries)
        self.order.append(str(prompt))
        if prompt == "block" and self.block_release is not None:
            if self.block_started is not None:
                self.block_started.set()
            await self.block_release.wait()
        elif self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(completion_text=prompt)


class LegacyProvider:
    async def text_chat(self, *, prompt, contexts, system_prompt):
        return SimpleNamespace(completion_text=prompt)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_provider_slots_are_released_after_parallel_calls(self):
        runtime = PluginLLMRuntime(provider_concurrency=6)
        active = 0
        peak = 0

        class CountingProvider(ExplicitRetryProvider):
            async def text_chat(self, **kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.04)
                    return SimpleNamespace(completion_text=kwargs["prompt"])
                finally:
                    active -= 1

        provider = CountingProvider("parallel")
        results = await asyncio.gather(*(
            runtime.call(provider, prompt=str(index), timeout=1,
                         label="episode_extract", optional=False)
            for index in range(12)
        ))
        self.assertEqual(peak, 6)
        self.assertEqual(active, 0)
        self.assertEqual(len(results), 12)
        self.assertEqual(runtime.snapshot()["parallel"]["gate"]["active"], 0)

    async def test_provider_timeout_releases_slot_for_next_memory_call(self):
        runtime = PluginLLMRuntime(provider_concurrency=1)
        provider = ExplicitRetryProvider("timeout-release")
        provider.delay = 0.08
        with self.assertRaises(asyncio.TimeoutError):
            await runtime.call(provider, prompt="slow", timeout=0.02,
                               label="episode_extract", optional=False)
        self.assertEqual(runtime.snapshot()["timeout-release"]["gate"]["active"], 0)
        provider.delay = 0
        result = await runtime.call(provider, prompt="next", timeout=1,
                                    label="diary_render", optional=False)
        self.assertEqual(result.completion_text, "next")

    async def test_astr_request_retry_is_bounded_to_one(self):
        runtime = PluginLLMRuntime()
        provider = ExplicitRetryProvider()
        result = await runtime.call(provider, prompt="ok", timeout=1, label="test")
        self.assertEqual(result.completion_text, "ok")
        self.assertEqual(provider.request_retries, [1])

    async def test_provider_without_new_astr_parameter_remains_compatible(self):
        runtime = PluginLLMRuntime()
        result = await runtime.call(LegacyProvider(), prompt="legacy", timeout=1)
        self.assertEqual(result.completion_text, "legacy")

    async def test_native_reasoning_model_is_not_truncated_by_plugin_output_cap(self):
        seen = []

        class Provider:
            provider_id = "astr-diary"

            async def text_chat(self, *, prompt, contexts, system_prompt,
                                request_timeout=None, max_tokens=None,
                                request_max_retries=None, **kwargs):
                seen.append((max_tokens, request_timeout, request_max_retries))
                return SimpleNamespace(completion_text="ok")

        runtime = PluginLLMRuntime()
        provider = Provider()
        await runtime.call(provider, prompt="ordinary", timeout=1, label="diary_render")
        await runtime.call(provider, prompt="grounded", timeout=1, label="diary_render_grounded")
        await runtime.call(provider, prompt="retry", timeout=1, label="diary_render_timeout_retry")
        self.assertEqual([item[0] for item in seen], [None, None, None])
        self.assertTrue(all(item[1] > 0 and item[2] == 1 for item in seen))

    async def test_external_model_keeps_its_configured_output_budget(self):
        seen = []

        class Provider:
            provider_id = "external:writer"

            async def text_chat(self, *, prompt, contexts, system_prompt,
                                timeout=None, max_tokens=None,
                                request_max_retries=None, task_label=""):
                seen.append((max_tokens, task_label))
                return SimpleNamespace(completion_text="ok")

        await PluginLLMRuntime().call(
            Provider(), prompt="render", timeout=1, label="diary_render"
        )
        await PluginLLMRuntime().call(
            Provider(), prompt="extract", timeout=1, label="episode_extract"
        )
        self.assertEqual(seen, [
            (16384, "diary_render"), (8192, "episode_extract"),
        ])

    async def test_direct_reasoning_post_assessment_has_room_for_final_json(self):
        seen = []

        class Provider:
            provider_id = "direct:thinking-model"

            async def text_chat(self, *, prompt, contexts, system_prompt,
                                timeout=None, max_tokens=None, task_label="",
                                request_max_retries=None):
                seen.append((max_tokens, task_label))
                return SimpleNamespace(completion_text="{}")

        await PluginLLMRuntime().call(
            Provider(), prompt="assess", timeout=1, label="xinchao_post",
            optional=True,
        )
        self.assertEqual(seen, [(8192, "xinchao_post")])

    async def test_legacy_astr_var_kwargs_do_not_fake_capability(self):
        seen = []

        class Provider:
            provider_id = "legacy-astr"

            async def text_chat(self, *, prompt, contexts, system_prompt,
                                request_max_retries=None, **kwargs):
                seen.append((request_max_retries, kwargs))
                return SimpleNamespace(completion_text="ok")

        await PluginLLMRuntime().call(
            Provider(), prompt="render", timeout=1, label="diary_render"
        )
        self.assertEqual(seen, [(1, {})])

    async def test_optional_timeout_isolated_by_task_family(self):
        runtime = PluginLLMRuntime()
        provider = ExplicitRetryProvider("shared")
        provider.fail_with = asyncio.TimeoutError()
        with self.assertRaises(asyncio.TimeoutError):
            await runtime.call(
                provider,
                prompt="first",
                timeout=1,
                label="xinchao_live",
                optional=True,
            )
        with self.assertRaises(asyncio.TimeoutError):
            await runtime.call(
                provider,
                prompt="second-timeout",
                timeout=1,
                label="xinchao_live",
                optional=True,
            )
        calls = provider.calls
        provider.fail_with = None
        result = await runtime.call(
            provider,
            prompt="second",
            timeout=1,
            label="time_insight_refine",
            optional=True,
        )
        self.assertEqual(result.completion_text, "second")
        self.assertEqual(provider.calls, calls + 1)
        snapshot = runtime.snapshot()["shared"]
        self.assertNotEqual(snapshot["state"], "circuit_open")
        self.assertEqual(snapshot["families"]["xinchao_live"]["state"], "degraded")
        self.assertEqual(snapshot["families"]["time_insight"]["state"], "healthy")

    async def test_critical_memory_generation_bypasses_optional_circuit(self):
        runtime = PluginLLMRuntime()
        provider = ExplicitRetryProvider("core")
        provider.fail_with = asyncio.TimeoutError()
        with self.assertRaises(asyncio.TimeoutError):
            await runtime.call(provider, prompt="optional", timeout=1, optional=True)
        provider.fail_with = None
        result = await runtime.call(
            provider,
            prompt="evidence",
            timeout=1,
            label="episode_extract",
            optional=False,
        )
        self.assertEqual(result.completion_text, "evidence")
        self.assertEqual(runtime.snapshot()["core"]["state"], "healthy")

    async def test_interactive_queue_budget_covers_wait_time_without_opening_circuit(self):
        runtime = PluginLLMRuntime(provider_concurrency=1, interactive_queue_timeout=0.02)
        provider = ExplicitRetryProvider("queue")
        provider.delay = 0.08
        occupied = asyncio.create_task(runtime.call(
            provider, prompt="critical", timeout=1, label="episode_extract",
            optional=False, lane="memory_critical",
        ))
        await asyncio.sleep(0.01)
        started = asyncio.get_running_loop().time()
        with self.assertRaises(LLMQueueTimeoutError):
            await runtime.call(
                provider, prompt="live", timeout=1, label="xinchao_live",
                optional=True, lane="interactive_optional",
            )
        self.assertLess(asyncio.get_running_loop().time() - started, 0.07)
        await occupied
        provider.delay = 0.0
        result = await runtime.call(
            provider, prompt="live-retry", timeout=1, label="xinchao_live",
            optional=True, lane="interactive_optional",
        )
        self.assertEqual(result.completion_text, "live-retry")

    async def test_priority_gate_runs_interactive_before_queued_background(self):
        runtime = PluginLLMRuntime(provider_concurrency=1)
        provider = ExplicitRetryProvider("priority")
        provider.block_started = asyncio.Event()
        provider.block_release = asyncio.Event()
        blocker = asyncio.create_task(runtime.call(
            provider, prompt="block", timeout=1, label="episode_extract",
            optional=False, lane="memory_critical",
        ))
        await provider.block_started.wait()
        background = asyncio.create_task(runtime.call(
            provider, prompt="background", timeout=1, label="profile_update",
            optional=True, lane="background_state",
        ))
        await asyncio.sleep(0)
        interactive = asyncio.create_task(runtime.call(
            provider, prompt="interactive", timeout=1, label="xinchao_live",
            optional=True, lane="interactive_optional", queue_timeout=0.5,
        ))
        await asyncio.sleep(0)
        provider.block_release.set()
        await asyncio.gather(blocker, background, interactive)
        self.assertEqual(provider.order, ["block", "interactive", "background"])

    async def test_foreground_lease_defers_background_but_not_interactive(self):
        runtime = PluginLLMRuntime(foreground_lease_seconds=30)
        provider = ExplicitRetryProvider("foreground")
        runtime.begin_foreground(provider, "session")
        background = asyncio.create_task(runtime.call(
            provider, prompt="background", timeout=1, label="profile_update",
            optional=True, lane="background_state",
        ))
        await asyncio.sleep(0.03)
        self.assertEqual(provider.calls, 0)
        live = await runtime.call(
            provider, prompt="live", timeout=1, label="xinchao_live",
            optional=True, lane="interactive_optional",
        )
        self.assertEqual(live.completion_text, "live")
        runtime.end_foreground(provider, "session")
        self.assertEqual((await background).completion_text, "background")

    async def test_close_rejects_late_calls(self):
        runtime = PluginLLMRuntime()
        await runtime.close()
        with self.assertRaises(LLMRuntimeClosedError):
            await runtime.call(ExplicitRetryProvider(), prompt="late", timeout=1)

    async def test_independent_api_facades_share_ten_slot_cap(self):
        runtime = PluginLLMRuntime(provider_concurrency=6, external_concurrency=10)
        first = ExplicitRetryProvider("external:alpha")
        second = ExplicitRetryProvider("direct:mind")
        await runtime.call(first, prompt="one", timeout=1)
        await runtime.call(second, prompt="two", timeout=1)
        self.assertEqual(runtime.snapshot()["external:alpha"]["gate"]["concurrency"], 10)
        self.assertEqual(runtime.snapshot()["direct:mind"]["gate"]["concurrency"], 10)
        self.assertIs(runtime._gate("external:alpha"), runtime._gate("direct:mind"))
        self.assertEqual(PluginLLMRuntime(provider_concurrency=99).provider_concurrency, 6)
        self.assertEqual(PluginLLMRuntime(external_concurrency=99).external_concurrency, 10)

    async def test_failure_telemetry_separates_provider_time_from_queue_time(self):
        samples = []
        runtime = PluginLLMRuntime(telemetry_callback=samples.append)
        provider = ExplicitRetryProvider("slow")
        provider.delay = 0.03
        provider.fail_with = TimeoutError("slow model")
        with self.assertRaises(TimeoutError):
            await runtime.call(provider, prompt="private dialogue", timeout=0.2)
        sample = samples[-1]
        self.assertEqual(sample["outcome"], "timeout")
        self.assertGreaterEqual(sample["provider_ms"], 20)
        self.assertLess(sample["queue_wait_ms"], sample["provider_ms"])
        self.assertEqual(sample["prompt_chars"], len("private dialogue"))
        self.assertEqual(sample["deadline_seconds"], 0.2)
        self.assertNotIn("private dialogue", str(sample))

    async def test_reasoning_only_response_is_not_recorded_as_success(self):
        samples = []
        runtime = PluginLLMRuntime(telemetry_callback=samples.append)

        class Provider:
            provider_id = "reasoning-only"

            async def text_chat(self, *, prompt, contexts, system_prompt):
                return SimpleNamespace(
                    completion_text="", reasoning_content="internal reasoning",
                    raw_completion=SimpleNamespace(
                        choices=[SimpleNamespace(finish_reason="length")],
                        usage=SimpleNamespace(
                            completion_tokens=8192,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=8192),
                        ),
                    ),
                )

        with self.assertRaises(LLMEmptyFinalError):
            await runtime.call(Provider(), prompt="private dialogue", timeout=1)
        self.assertEqual(samples[-1]["outcome"], "empty_final")
        self.assertEqual(samples[-1]["finish_reason"], "length")
        self.assertEqual(samples[-1]["reasoning_tokens"], 8192)
        self.assertNotIn("private dialogue", str(samples[-1]))
        self.assertNotIn("internal reasoning", str(samples[-1]))


if __name__ == "__main__":
    unittest.main()
