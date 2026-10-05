import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_memos_memory.call_policy import defaults, resolve, validate_overrides
from astrbot_plugin_memos_memory.generation_v2.transport import Collector, ResponseFailure, invoke
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.scheduler import Route


class PolicyTests(unittest.TestCase):
    def test_default_and_validation(self):
        self.assertTrue(defaults('episode_extract')['stream'])
        self.assertEqual(defaults('diary_write')['thinking'], 'disabled')
        for patch in ({'max_tokens': 0}, {'stream': 1}, {'thinking': 'yes'}, {'idle_timeout': 0}):
            with self.assertRaises(ValueError): validate_overrides({'episode_extract': patch})
        value = validate_overrides({'episode_extract': {'max_tokens': 65536}})
        self.assertEqual(resolve(value, 'episode_extract_compact_retry')['max_tokens'], 65536)

    def test_only_final_text(self):
        c = Collector(defaults('episode_extract'))
        c.feed({'choices': [{'delta': {'reasoning_content': 'private thought'}}]}, True)
        c.feed({'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
                'usage': {'completion_tokens': 20, 'completion_tokens_details': {'reasoning_tokens': 10}}}, True)
        out = c.finish()
        self.assertEqual(out.completion_text, '{"ok":true}')
        self.assertNotIn('private thought', json.dumps(out.call_diagnostics))
        self.assertEqual(out.call_diagnostics['reasoning_tokens'], 10)

    def test_partial_and_empty_are_never_success(self):
        for reason, text in [('length', 'partial'), (None, 'partial'), ('stop', '')]:
            c = Collector(defaults('diary_write'))
            c.feed({'choices': [{'message': {'content': text}, 'finish_reason': reason}]}, False)
            with self.assertRaises(ResponseFailure): c.finish()

    def test_policy_frozen_and_changes_identity(self):
        settings = {'episode_extract': defaults('episode_extract')}
        a = AstrAdapter(object(), policies=settings)
        first = Route('p', 'astr', 'host', 'host', a).digest
        settings['episode_extract']['max_tokens'] = 65536
        b = AstrAdapter(object(), policies=settings)
        self.assertNotEqual(first, Route('p', 'astr', 'host', 'host', b).digest)
        self.assertEqual(a.policies['episode_extract']['max_tokens'], 32768)


class Stream:
    def __init__(self, chunks): self.chunks = iter(chunks); self.closed = False
    def __aiter__(self): return self
    async def __anext__(self):
        try: data = next(self.chunks)
        except StopIteration: raise StopAsyncIteration
        return SimpleNamespace(model_dump=lambda: data)
    async def close(self): self.closed = True


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_real_sse_on_loopback_only(self):
        from aiohttp import web
        from astrbot_plugin_memos_memory.direct_llm import OpenAICompatibleTextClient
        sent = []
        async def handler(request):
            sent.append(await request.json())
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            for delta in ({'reasoning_content': 'not a diary'}, {'content': 'hello '}, {'content': 'world'}):
                await response.write(('data: '+json.dumps({'choices': [{'delta': delta}]})+'\n\n').encode())
            await response.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":12}}\n\ndata: [DONE]\n\n')
            await response.write_eof(); return response
        app = web.Application(); app.router.add_post('/chat/completions', handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        client = OpenAICompatibleTextClient('fixture')
        client.configure(base_url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]),
                         model='deepseek-v4-pro', api_key='fake-local-key')
        try:
            out = await invoke(client, {'prompt': 'test'}, 5, defaults('episode_extract'))
            self.assertEqual(out.completion_text, 'hello world')
            self.assertEqual(len(sent), 1)
            self.assertTrue(sent[0]['stream'])
            self.assertEqual(sent[0]['max_tokens'], 32768)
            self.assertNotIn('top_p', sent[0])
        finally:
            if client._http: await client._http.close()
            await runner.cleanup()

    def provider(self, result):
        create = AsyncMock(return_value=result)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        client.with_options = lambda **kwargs: client
        p = SimpleNamespace(client=client, provider_config={'custom_extra_body': {'temperature': .7}},
            _prepare_chat_payload=AsyncMock(return_value=({'model': 'deepseek-v4-pro', 'messages': []}, [])))
        return p, create

    async def test_astr_captures_explicit_stream_and_budget(self):
        stream = Stream([{'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]}])
        p, create = self.provider(stream)
        result = await invoke(p, {'prompt': 'test', 'contexts': [], 'system_prompt': ''}, 10, defaults('episode_extract'))
        self.assertEqual(result.completion_text, 'ok')
        sent = create.call_args.kwargs
        self.assertTrue(sent['stream'])
        self.assertEqual(sent['max_tokens'], 32768)
        self.assertEqual(sent['extra_body']['thinking'], {'type': 'enabled'})
        self.assertEqual(sent['extra_body']['reasoning_effort'], 'low')
        self.assertNotIn('thinking', p.provider_config['custom_extra_body'])
        self.assertTrue(stream.closed)
        create.assert_awaited_once()

    async def test_nonstream_and_invalid_top_p(self):
        result = SimpleNamespace(model_dump=lambda: {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]})
        p, create = self.provider(result)
        await invoke(p, {'prompt': 'test'}, 10, {**defaults('diary_write'), 'stream': False})
        self.assertFalse(create.call_args.kwargs['stream'])
        p.provider_config['custom_extra_body']['top_p'] = 0
        with self.assertRaises(ValueError): await invoke(p, {'prompt': 'test'}, 10, defaults('diary_write'))
        self.assertEqual(create.await_count, 1)

    async def test_cancel_closes_stream(self):
        class Slow(Stream):
            async def __anext__(self): await asyncio.sleep(5)
        stream = Slow([])
        p, _ = self.provider(stream)
        task = asyncio.create_task(invoke(p, {'prompt': 'test'}, 10, defaults('diary_write')))
        await asyncio.sleep(.01); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(stream.closed)
