"""Installed Astr control-flow test; replaces network operations, not text_chat."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter


class AstrSourceTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        p = ProviderOpenAIOfficial.__new__(ProviderOpenAIOfficial)
        p.api_keys = ['fixture-key']
        p.client = Mock()
        p._prepare_chat_payload = AsyncMock(return_value=({}, 'fixture'))
        p._query = AsyncMock(return_value=SimpleNamespace(completion_text='ok'))
        p._handle_api_error = AsyncMock(return_value=(False, 'fixture-key', [], {}, '', None, False))
        return p

    async def test_real_text_chat_forwards_full_budget_and_disables_sdk_retries(self):
        p = self.provider()
        result = await AstrAdapter(p).call(prompt='fixture', timeout=180)
        self.assertEqual(result.text, 'ok')
        self.assertEqual(p._query.await_count, 1)
        self.assertEqual(p._query.call_args.kwargs['request_timeout'], 180)
        p.client.with_options.assert_called_once_with(api_key='fixture-key', max_retries=0)

    async def test_real_text_chat_does_not_retry_transport(self):
        p = self.provider()
        p._query.side_effect = ConnectionError('fixture')
        with self.assertRaises(ConnectionError):
            await AstrAdapter(p).call(prompt='fixture')
        self.assertEqual(p._query.await_count, 1)
        self.assertEqual(p._handle_api_error.await_count, 1)
