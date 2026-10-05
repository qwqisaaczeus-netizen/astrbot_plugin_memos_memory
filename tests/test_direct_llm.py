from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_memos_memory.direct_llm import (
    DirectLLMEmptyFinalError,
    OpenAICompatibleTextClient,
    SecretValueStore,
    validate_api_base_url,
)


class DirectLLMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response_status = 200
        self.response_payload = None

        async def handler(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in header.decode("latin1").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            body = await reader.readexactly(length) if length else b""
            self.requests.append((header, json.loads(body or b"{}")))
            payload = json.dumps(self.response_payload or {
                "choices": [{"message": {"content": '{"ok":true}'}}]
            }).encode("utf-8")
            writer.write(
                f"HTTP/1.1 {self.response_status} {'OK' if self.response_status == 200 else 'Unavailable'}\r\nContent-Type: application/json\r\n".encode("ascii")
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + payload
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.client = OpenAICompatibleTextClient("test")
        self.client.configure(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="test-model",
            api_key="secret-token",
            max_retries=0,
        )

    async def asyncTearDown(self):
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_chat_uses_openai_compatible_endpoint_and_returns_text(self):
        response = await self.client.text_chat(
            prompt="hello", system_prompt="system", timeout=2, max_tokens=64,
        )
        self.assertEqual(response.completion_text, '{"ok":true}')
        header, body = self.requests[0]
        self.assertIn(b"POST /v1/chat/completions HTTP/1.1", header)
        self.assertIn(b"Authorization: Bearer secret-token", header)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"][1]["content"], "hello")

    async def test_test_call_reports_direct_provider_and_latency(self):
        result = await self.client.test(timeout=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "direct:test-model")
        self.assertGreaterEqual(result["latency_ms"], 0)

    async def test_default_output_budget_supports_long_memory_rendering(self):
        await self.client.text_chat(prompt="long diary", timeout=2)
        self.assertEqual(self.requests[-1][1]["max_tokens"], 8192)

    async def test_reasoning_only_reply_reports_budget_exhaustion(self):
        self.response_payload = {
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 2048,
                      "completion_tokens_details": {"reasoning_tokens": 2048}},
        }
        with self.assertRaises(DirectLLMEmptyFinalError) as raised:
            await self.client.text_chat(prompt="assess", timeout=2)
        self.assertEqual(raised.exception.finish_reason, "length")
        self.assertEqual(raised.exception.reasoning_tokens, 2048)

    async def test_runtime_one_attempt_does_not_double_retry_direct_api(self):
        self.client.max_retries = 1
        self.response_status = 503
        with self.assertRaises(RuntimeError):
            await self.client.text_chat(
                prompt="retry once", timeout=2, request_max_retries=1,
            )
        self.assertEqual(len(self.requests), 1)

    def test_secret_store_round_trip_and_url_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SecretValueStore(Path(temp) / "secret.json")
            store.save("abc")
            self.assertEqual(store.load(), "abc")
            store.save("")
            self.assertFalse((Path(temp) / "secret.json").exists())
        self.assertEqual(validate_api_base_url("https://example.com/v1/"), "https://example.com/v1")
        with self.assertRaises(ValueError):
            validate_api_base_url("https://user:pass@example.com/v1")


if __name__ == "__main__":
    unittest.main()
