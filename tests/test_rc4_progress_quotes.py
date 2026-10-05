import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_generation_v2_test3 import archive
from astrbot_plugin_memos_memory.call_policy import defaults
from astrbot_plugin_memos_memory.generation_v2.sources import load_batch,split_batch,SourceError
from astrbot_plugin_memos_memory.generation_v2.quote_grounding import resolve_quotes
from astrbot_plugin_memos_memory.generation_v2.transport import invoke,Collector,ProgressGate
from astrbot_plugin_memos_memory.generation_v2.scheduler import Scheduler


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        path=Path(self.temp.name)/'source.db'; archive(path,['早上我答应回来。晚上我答应回来。','我会等你。'])
        self.batch=load_batch(path,'batch','scope'); self.shard=split_batch(self.batch)[0]
        self.first,self.second=[s.key for s in self.shard.owned]
        self.obj={'facts':[{'id':'f','kind':'promise','basis':'explicit','claim':'答应回来',
            'citations':[{'span_id':self.first,'quote':'我答应回来。','before':'晚上'}]}],
            'omissions':[{'span_id':self.second,'reason':'只记录承诺方，回应原文保留'}]}

    def test_exact_disambiguation_and_source_time(self):
        result=resolve_quotes(json.dumps(self.obj),self.batch,self.shard)
        citation=result['facts'][0]['citations'][0]
        self.assertEqual(citation['start'],10)
        self.assertEqual(citation['event_ts'],self.batch.turns[0].event_ts)
        self.assertEqual(result['narratives'],[])

    def test_ambiguous_rejected(self):
        del self.obj['facts'][0]['citations'][0]['before']
        with self.assertRaises(SourceError):resolve_quotes(json.dumps(self.obj),self.batch,self.shard)

    def test_foreign_span_rejected(self):
        self.obj['facts'][0]['citations'][0]['span_id']='foreign'
        with self.assertRaises(SourceError):resolve_quotes(json.dumps(self.obj),self.batch,self.shard)

    def test_missing_coverage_rejected(self):
        self.obj['omissions']=[]
        with self.assertRaises(SourceError):resolve_quotes(json.dumps(self.obj),self.batch,self.shard)

    def test_invented_quote_rejected(self):
        self.obj['facts'][0]['citations'][0]['quote']='明天结婚'
        with self.assertRaises(SourceError):resolve_quotes(json.dumps(self.obj),self.batch,self.shard)

    def test_webui_release_labels_and_cache_keys(self):
        root=Path(__file__).resolve().parents[1]
        for page in root.glob('*.html'):
            self.assertNotIn('6.1.0-rc3',page.read_text(encoding='utf-8'))
            self.assertNotIn('610rc3',page.read_text(encoding='utf-8'))
        self.assertIn('6.1.0',(root/'assets/workbench.js').read_text(encoding='utf-8'))


class SlowStream:
    def __init__(self, chunks):self.chunks=iter(chunks);self.closed=False
    def __aiter__(self):return self
    async def __anext__(self):
        try: delay,data=next(self.chunks)
        except StopIteration:raise StopAsyncIteration
        await asyncio.sleep(delay)
        return SimpleNamespace(model_dump=lambda:data)
    async def close(self):self.closed=True

def chunk(text='',reasoning='',finish=None):
    return {'choices':[{'delta':{'content':text,'reasoning_content':reasoning},'finish_reason':finish}]}


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def provider(self,stream):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=stream))))
        client.with_options=lambda **kw:client
        return SimpleNamespace(client=client,provider_config={},
            _prepare_chat_payload=AsyncMock(return_value=({'model':'deepseek-v4-pro','messages':[]},[])))

    async def test_progress_can_cross_original_timeout(self):
        stream=SlowStream([(.01,chunk(reasoning='thinking')),(.2,chunk('hello')),(.2,chunk(' world',finish='stop'))])
        out=await invoke(self.provider(stream),{'prompt':'test'},.15,{**defaults('episode_extract'),'idle_timeout':.5})
        self.assertEqual(out.completion_text,'hello world');self.assertTrue(stream.closed)

    async def test_empty_heartbeats_do_not_extend(self):
        stream=SlowStream([(.01,chunk(reasoning='thinking'))]+[(.01,chunk())]*20)
        with self.assertRaises(TimeoutError) as error:
            await invoke(self.provider(stream),{'prompt':'test'},.3,{**defaults('episode_extract'),'idle_timeout':.1})
        self.assertEqual(error.exception.diagnostics['timeout_phase'],'progress_idle')
        self.assertTrue(stream.closed)

    async def test_no_first_output_times_out(self):
        stream=SlowStream([(.01,chunk())]*20)
        with self.assertRaises(TimeoutError) as error:
            await invoke(self.provider(stream),{'prompt':'test'},.1,defaults('episode_extract'))
        self.assertEqual(error.exception.diagnostics['timeout_phase'],'first_progress')
        self.assertTrue(stream.closed)

    async def test_partial_timeout_no_automatic_repeat(self):
        primary=SimpleNamespace(digest='one');backup=SimpleNamespace(digest='two')
        history=[{'route_id':'one','outcome':'awaiting_recovery','metadata':json.dumps(
            {'kind':'timeout','diagnostics':{'reasoning_chars':100}})}]
        self.assertIsNone(Scheduler._next(primary,backup,history))

    async def test_cancel_closes_sdk_stream(self):
        stream=SlowStream([(.01,chunk('hello')),(10,chunk('world'))])
        task=asyncio.create_task(invoke(self.provider(stream),{'prompt':'test'},1,defaults('episode_extract')))
        await asyncio.sleep(.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(stream.closed)

    async def test_direct_sse_progress_uses_same_gate(self):
        from astrbot_plugin_memos_memory.direct_llm import OpenAICompatibleTextClient
        async def lines():
            for delay,data in [(.01,chunk(reasoning='thinking')),(1.2,chunk('hello',finish='stop'))]:
                await asyncio.sleep(delay)
                yield ('data: '+json.dumps(data)+'\n').encode()
                yield b'\n'
            yield b'data: [DONE]\n'
            yield b'\n'
        response=SimpleNamespace(status=200,content=lines())
        class ResponseContext:
            async def __aenter__(self):return response
            async def __aexit__(self,*args):pass
        captured={}
        def post(*args,**kwargs):
            captured.update(kwargs)
            return ResponseContext()
        provider=object.__new__(OpenAICompatibleTextClient)
        provider.model='deepseek-v4-pro';provider.temperature=.6;provider.api_key='fixture'
        with patch.object(OpenAICompatibleTextClient,'_session',new=AsyncMock(return_value=SimpleNamespace(post=post))), \
             patch.object(OpenAICompatibleTextClient,'_chat_url',return_value='http://example.invalid'):
            out=await invoke(provider,{'prompt':'test'},1,{**defaults('episode_extract'),'idle_timeout':2})
        self.assertEqual(out.completion_text,'hello')
        self.assertIsNone(captured['timeout'].total)
        self.assertIsNone(captured['timeout'].sock_read)
