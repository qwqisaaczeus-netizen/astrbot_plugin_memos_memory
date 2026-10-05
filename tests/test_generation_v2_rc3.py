import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_generation_v2_test3 import archive
from test_generation_v2_test4 import Model, Remote
from astrbot_plugin_memos_memory.generation_v2.coordinator import Coordinator
from astrbot_plugin_memos_memory.generation_v2.literary import DiaryStore
from astrbot_plugin_memos_memory.generation_v2.scheduler import Scheduler, Route
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.recovery import control, saved_workflow
from astrbot_plugin_memos_memory.generation_v2.operations import mode_settings, readiness, publish_draft
from astrbot_plugin_memos_memory.generation_v2.integration import ensure_handoffs, search_evidence
from astrbot_plugin_memos_memory.generation_v2.publishing import Publisher
from astrbot_plugin_memos_memory.generation_v2.store import ConflictError
from astrbot_plugin_memos_memory.generation_v2.upgrades import stage_upgrades
from astrbot_plugin_memos_memory.generation_v2 import legacy_links, mind_gateway


class RC3Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.source=self.root/'source.db'; archive(self.source)
        self.original=self.source.read_bytes()
        self.store=DiaryStore(self.root/'generation_runtime.db')
        self.service=Coordinator(self.store,Scheduler(self.store),self.source)
        self.addAsyncCleanup(self.service.close)
        self.model=Model(); self.model.meta=lambda:SimpleNamespace(id='model')
        self.route=Route('model','astr','fixture','fixture',AstrAdapter(self.model))
        self.plugin=SimpleNamespace(runtime_state_dir=self.root,_generation_v2_service=self.service,
            _compress_locks={},_episodes=SimpleNamespace(db_path=self.source),_vec=object(),_memos=object(),
            character_name='Fixture',_chat_provider_options=lambda:[{'value':'model'}],
            _resolve_chat_provider=lambda *a,**kw:self.model)
        self.routes=patch('astrbot_plugin_memos_memory.generation_v2.recovery.route_for',return_value=(self.route,None))
        self.routes.start(); self.addCleanup(self.routes.stop)

    async def ready(self):
        job=await self.service.start('batch','scope',self.route)
        self.assertEqual((await self.service.wait(job))['stage'],'draft_ready')
        return job

    async def test_rewrite_reuses_extraction_new_budget_old_immutable(self):
        job=await self.ready(); old=self.store.job(job); workflow=saved_workflow(self.service,job)
        request={'job_id':job,'action':'rewrite','confirm_model_calls':True,'request_id':'rewrite-001','job_cap':8}
        result=await control(self.plugin,request); child=result['job_id']
        self.assertEqual((await self.service.wait(child))['stage'],'draft_ready')
        self.assertEqual(len(self.model.calls),6)
        self.assertEqual(self.store.job(job),old)
        self.assertEqual(saved_workflow(self.service,job),workflow)
        again=await control(self.plugin,request)
        self.assertEqual(again['job_id'],child); self.assertEqual(len(self.model.calls),6)
        self.assertEqual(self.original,self.source.read_bytes())

    async def test_retry_reuses_qualified_without_calls(self):
        job=await self.ready()
        result=await control(self.plugin,{'job_id':job,'action':'retry','confirm_model_calls':True,'request_id':'retry-001','job_cap':8})
        self.assertEqual((await self.service.wait(result['job_id']))['stage'],'draft_ready')
        self.assertEqual(len(self.model.calls),4)

    async def test_review_failure_retry_does_not_rewrite_prose(self):
        original=self.model.text_chat
        async def failing(**kwargs):
            if 'draft' in json.loads(kwargs['prompt']): raise TimeoutError('fixture')
            return await original(**kwargs)
        # Keep explicit controls exposed to the strict adapter.
        async def controlled(*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
            return await failing(prompt=prompt,contexts=contexts,system_prompt=system_prompt,
                request_timeout=request_timeout,request_max_retries=request_max_retries)
        self.model.text_chat=controlled
        job=await self.service.start('batch','scope',self.route)
        self.assertEqual((await self.service.wait(job))['stage'],'awaiting_recovery')
        self.model.text_chat=original
        result=await control(self.plugin,{'job_id':job,'action':'retry','confirm_model_calls':True,'request_id':'review-001','job_cap':8})
        self.assertEqual((await self.service.wait(result['job_id']))['stage'],'draft_ready')
        self.assertEqual(sum('narratives' in c and 'owned' not in c for c in self.model.calls),1)

    async def test_expired_resume_requires_explicit_revision(self):
        job=await self.ready()
        with self.store.connect() as db: db.execute('UPDATE source_receipts SET deadline=1 WHERE job_id=?',(job,))
        with self.assertRaises(ConflictError):
            await control(self.plugin,{'job_id':job,'action':'resume','confirm_model_calls':True})
        self.assertEqual(len(self.model.calls),4)

    async def test_explicit_draft_publish_is_idempotent(self):
        job=await self.ready(); remote=Remote()
        self.plugin.generation_v2_memos_capabilities_confirmed=True
        class Sink:
            async def apply(self,*args): pass
            async def verify(self,*args): return True
        with patch('astrbot_plugin_memos_memory.generation_v2.operations.MemosPublisherAdapter',return_value=remote), \
             patch('astrbot_plugin_memos_memory.generation_v2.operations.ConsumerIndex',return_value=self.service.index), \
             patch('astrbot_plugin_memos_memory.generation_v2.operations.StateConsumer',return_value=Sink()):
            await publish_draft(self.plugin,{'job_id':job,'confirm_delivery':True})
            await self.service.delivery_tasks[job]
            self.assertEqual(self.service.status(job)['stage'],'published')
            await publish_draft(self.plugin,{'job_id':job,'confirm_delivery':True})
            await self.service.delivery_tasks[job]
        self.assertEqual(remote.calls,1); self.assertEqual(len(self.model.calls),4)
        self.assertEqual(self.original,self.source.read_bytes())

    async def test_pause_retains_source_and_blocks_owner(self):
        job=await self.ready(); ensure_handoffs(self.store)
        with self.store.connect() as db:
            db.execute("INSERT INTO auto_handoffs(batch_id,scope,source_kind,job_id,created) VALUES('batch','scope','eod',?,0)",(job,))
        result=await control(self.plugin,{'job_id':job,'action':'cancel'})
        self.assertEqual(result['status'],'cancelled')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM auto_handoffs').fetchone()[0],'paused')
        self.assertEqual(self.original,self.source.read_bytes())

    async def test_one_batch_delivery_confirmation_does_not_enable_automatic(self):
        job=await self.ready();remote=Remote()
        self.plugin.generation_v2_memos_capabilities_confirmed=False
        with self.assertRaises(ConflictError):
            await publish_draft(self.plugin,{'job_id':job,'confirm_delivery':True})
        class Sink:
            async def apply(self,*args):pass
        with patch('astrbot_plugin_memos_memory.generation_v2.operations.MemosPublisherAdapter',return_value=remote), \
             patch('astrbot_plugin_memos_memory.generation_v2.operations.ConsumerIndex',return_value=self.service.index), \
             patch('astrbot_plugin_memos_memory.generation_v2.operations.StateConsumer',return_value=Sink()):
            await publish_draft(self.plugin,{'job_id':job,'confirm_delivery':True,'confirm_memos_contract':True})
            await self.service.delivery_tasks[job]
        self.assertEqual(self.service.status(job)['stage'],'published')
        self.assertFalse(self.plugin.generation_v2_memos_capabilities_confirmed)

    async def test_unready_publisher_retries_init_before_any_remote_write(self):
        job=await self.ready();self.plugin._vec=None
        self.plugin.generation_v2_memos_capabilities_confirmed=True
        from unittest.mock import AsyncMock
        self.plugin._ensure_init=AsyncMock(return_value=False)
        with self.assertRaises(ConflictError):
            await publish_draft(self.plugin,{'job_id':job,'confirm_delivery':True})
        self.plugin._ensure_init.assert_awaited_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM publish_batches').fetchone()[0],0)

    async def test_unsafe_recovery_refused(self):
        job=await self.ready()
        with self.assertRaises(ValueError): await control(self.plugin,{'job_id':job,'action':'retry'})
        pub=Publisher(self.store,Remote(),self.source); pub.prepare(job,'scope')
        with self.assertRaises(ConflictError): await control(self.plugin,{'job_id':job,'action':'cancel'})
        self.assertEqual(len(self.model.calls),4)

    async def test_source_change_prevents_reuse(self):
        job=await self.ready()
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE source_turns SET event_ts=event_ts+10")
        db.close()
        with self.assertRaises(ConflictError):
            await control(self.plugin,{'job_id':job,'action':'retry','confirm_model_calls':True,'request_id':'changed-001','job_cap':8})
        self.assertEqual(len(self.model.calls),4)

    async def test_upgrade_search_rollback_and_delete_boundary(self):
        job=await self.ready(); stage_upgrades(self.source,self.store)
        result=legacy_links.apply(self.store,self.source,'old',job)
        self.assertEqual(result['memos_writes'],0)
        hits=search_evidence(self.plugin,'scope','explicit statement')
        self.assertEqual(hits[0]['memo_name'],'memos/old'); self.assertTrue(hits[0]['_v2_evidence'])
        self.assertEqual(search_evidence(self.plugin,'other','explicit statement'),[])
        legacy_links.rollback(self.store,'old')
        self.assertEqual(search_evidence(self.plugin,'scope','explicit statement'),[])
        legacy_links.apply(self.store,self.source,'old',job)
        with sqlite3.connect(self.source) as db: db.execute('UPDATE episodes SET active=0')
        db.close()
        self.assertEqual(search_evidence(self.plugin,'scope','explicit statement'),[])

    async def test_mind_scheduler_owns_retry_and_persists(self):
        count=0
        async def chat(*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
            nonlocal count
            count+=1
            self.assertEqual(request_max_retries,0)
            if count==1: raise ConnectionError('fixture')
            return SimpleNamespace(completion_text='fine')
        self.model.text_chat=chat
        result=await mind_gateway.call(self.plugin,self.model,prompt='data',contexts=[],system_prompt='',timeout=1,label='xinchao_post')
        self.assertEqual(result.completion_text,'fine'); self.assertEqual(count,2)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],2)

    def test_enable_requires_explicit_confirmations_and_dependencies(self):
        with self.assertRaises(ValueError): mode_settings(self.plugin,{'mode':'automatic'})
        values=mode_settings(self.plugin,{'mode':'automatic','confirm_model_calls':True,'confirm_delivery':True,'confirm_memos_contract':True})
        self.assertTrue(values['generation_v2_enable'])
        self.assertEqual(readiness(self.plugin)['mode'],'compatibility')


if __name__=='__main__': unittest.main()
