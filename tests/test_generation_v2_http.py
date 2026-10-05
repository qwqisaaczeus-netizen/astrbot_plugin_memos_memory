"""Loopback HTTP verification. No external endpoint or user credential used."""
import unittest

from aiohttp import web

from astrbot_plugin_memos_memory.direct_llm import OpenAICompatibleTextClient
from astrbot_plugin_memos_memory.generation_v2.adapters import DirectAdapter, classify_error


class DirectHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        async def endpoint(request):
            self.requests.append(await request.json())
            if self.status != 200:
                return web.json_response({'error': 'fixture'}, status=self.status,
                                         headers={'Retry-After': '7'})
            return web.json_response({'choices': [{'message': {'content': 'fixture'}}],
                                      'usage': {'prompt_tokens': 12, 'completion_tokens': 3}})
        app = web.Application()
        app.router.add_post('/chat/completions', endpoint)
        self.server = web.AppRunner(app)
        await self.server.setup()
        site = web.TCPSite(self.server, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        if port == 8689:
            await self.server.cleanup()
            self.skipTest('excluded port')
        self.client = OpenAICompatibleTextClient('test')
        self.client.configure(base_url=f'http://127.0.0.1:{port}', model='fixture',
                              api_key='fixture-not-a-real-key', max_retries=2, timeout=3)

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.cleanup()

    async def test_success_usage_and_request(self):
        result = await DirectAdapter(self.client).call(prompt='source', timeout=180, max_tokens=1000)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result.usage, {'prompt_tokens':12, 'completion_tokens':3})
        self.assertEqual(result.effective_timeout, 180)
        self.assertEqual(self.requests[0]['messages'][-1]['content'], 'source')
        self.assertEqual(self.requests[0]['max_tokens'], 1000)

    async def test_429_no_hidden_retry(self):
        self.status = 429
        with self.assertRaises(Exception) as caught:
            await DirectAdapter(self.client).call(prompt='p')
        self.assertEqual(len(self.requests), 1)
        failure = classify_error(caught.exception)
        self.assertEqual(failure.kind, 'rate_limit')
        self.assertEqual(failure.retry_after, 7)

    async def test_503_no_hidden_retry(self):
        self.status = 503
        with self.assertRaises(Exception) as caught:
            await DirectAdapter(self.client).call(prompt='p')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(classify_error(caught.exception).kind, 'overload')

    async def test_authentication_not_transport(self):
        self.status = 401
        with self.assertRaises(Exception) as caught:
            await DirectAdapter(self.client).call(prompt='p')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(classify_error(caught.exception).kind, 'authentication')
