import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_memos_memory.call_policy import defaults, validate_overrides
from astrbot_plugin_memos_memory.external_models import ExternalModelRegistry
from astrbot_plugin_memos_memory.generation_v2.model_probe import probe, provider_options, call_records
from astrbot_plugin_memos_memory.generation_v2.transport import invoke, ResponseFailure
from astrbot_plugin_memos_memory.generation_v2.scheduling_store import SchedulingStore
from astrbot_plugin_memos_memory.generation_v2.scheduler import Scheduler


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.registry = ExternalModelRegistry(Path(self.temp.name))
        self.addAsyncCleanup(self.registry.close)
        self.store = SchedulingStore(Path(self.temp.name)/'probe.db')
        self.scheduler = Scheduler(self.store)
        self.addAsyncCleanup(self.scheduler.close)
        self.provider = SimpleNamespace(meta=lambda: SimpleNamespace(id='astr-one'),
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='ok',raw_completion=None)))
        self.plugin = SimpleNamespace(_external_models=self.registry,
            context=SimpleNamespace(get_provider_by_id=lambda x:self.provider if x=='astr-one' else None,
                                    get_all_providers=lambda:[self.provider]),
            _generation_v2_service=SimpleNamespace(store=self.store,scheduler=self.scheduler))
        self.body = dict(confirm_model_calls=True,provider_id='astr-one',task='episode_extract',
            request_id='probe-test-001',prompt='hello',timeout=5,policy={'max_tokens':512})

    async def test_single_astr_and_policy_is_temporary(self):
        self.registry.enabled=True
        before=copy.deepcopy(self.registry.task_call_policies)
        self.assertEqual(provider_options(self.plugin)[0]['value'],'astr-one')
        result=await probe(self.plugin,self.body)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['attempts'],1)
        self.provider.text_chat.assert_awaited_once()
        self.assertEqual(self.provider.text_chat.call_args.kwargs['max_tokens'],512)
        self.assertEqual(self.registry.task_call_policies,before)
        records=call_records(self.plugin)
        self.assertEqual(records[0]['provider'],'astr-one')
        self.assertEqual(records[0]['outcome'],'success')
        with self.assertRaises(ValueError): await probe(self.plugin,self.body)
        self.provider.text_chat.assert_awaited_once()

    async def test_failure_never_retries(self):
        self.provider.text_chat.side_effect=TimeoutError()
        result=await probe(self.plugin,self.body)
        self.assertFalse(result['success'])
        self.assertEqual(result['attempts'],1)
        self.provider.text_chat.assert_awaited_once()

    async def test_capacity_rejects_without_dispatch(self):
        self.body['policy']['context_window_tokens']=100
        result=await probe(self.plugin,self.body)
        self.assertFalse(result['success'])
        self.assertEqual(result['diagnostics']['context_window_tokens'],100)
        self.provider.text_chat.assert_not_awaited()

    async def test_consent_required(self):
        self.body['confirm_model_calls']=False
        with self.assertRaises(ValueError): await probe(self.plugin,self.body)
        self.provider.text_chat.assert_not_awaited()

    async def test_context_setting_persists_when_omitted(self):
        model=dict(id='one',name='One',model='offline',base_url='https://example.invalid',
                   api_key='fake',enabled=True,context_window_tokens=65536)
        self.registry.save({'models':[model]})
        del model['context_window_tokens']; del model['api_key']
        self.registry.save({'models':[model]})
        self.assertEqual(self.registry.model('one')['context_window_tokens'],65536)
        loaded=ExternalModelRegistry(Path(self.temp.name)); self.addAsyncCleanup(loaded.close)
        self.assertEqual(loaded.model('one')['context_window_tokens'],65536)

    async def test_astr_capacity_before_sdk_call(self):
        create=AsyncMock()
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        p=SimpleNamespace(client=client,provider_config={},_prepare_chat_payload=AsyncMock(
            return_value=({'model':'deepseek-v4-pro','messages':[{'role':'user','content':'hello'}]},[])))
        with self.assertRaises(ResponseFailure):
            await invoke(p,{'prompt':'hello'},5,{**defaults('episode_extract'),'context_window_tokens':100})
        create.assert_not_awaited()

    async def test_context_validation(self):
        for value in (-1,True,1.5,2097153):
            with self.assertRaises(ValueError):
                validate_overrides({'episode_extract':{'context_window_tokens':value}})

    async def test_selected_external_only_and_model_capacity(self):
        self.registry.save({'models':[dict(id='one',name='One',model='offline',
            base_url='https://example.invalid',api_key='fake',enabled=True,
            context_window_tokens=8192,max_output_tokens=1024)]})
        self.body['provider_id']='external:one'
        self.body['policy']['max_tokens']=2048
        fake=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='external ok')))
        with patch.object(self.registry,'_client',return_value=fake):
            result=await probe(self.plugin,self.body)
        self.assertTrue(result['success'],result)
        fake.text_chat.assert_awaited_once()
        self.provider.text_chat.assert_not_awaited()
        self.assertEqual(result['diagnostics']['max_tokens'],1024)
        self.assertEqual(result['diagnostics']['context_window_tokens'],8192)

    async def test_external_capacity_blocks_before_network(self):
        self.registry.save({'models':[dict(id='one',name='One',model='offline',
            base_url='https://example.invalid',api_key='fake',enabled=True,
            context_window_tokens=100)]})
        self.body['provider_id']='external:one'
        result=await probe(self.plugin,self.body)
        self.assertFalse(result['success'])
        self.assertEqual(result['error_kind'],'context_limit')
        self.assertEqual(result['diagnostics']['context_window_tokens'],100)
