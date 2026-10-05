import json
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_generation_v2_test3 import archive
from test_generation_v2_test4 import Model, Remote
from astrbot_plugin_memos_memory.generation_v2 import literary, integration
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.coordinator import Coordinator
from astrbot_plugin_memos_memory.generation_v2.scheduler import Route, Scheduler
from astrbot_plugin_memos_memory.generation_v2.publishing import Publisher
from astrbot_plugin_memos_memory.generation_v2.workbench import overview


class TextContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.source=self.root/'source.db'; archive(self.source)
        self.store=literary.DiaryStore(self.root/'generation_runtime.db')
        self.service=Coordinator(self.store,Scheduler(self.store),self.source)
        self.addAsyncCleanup(self.service.close)
        self.model=Model(); self.route=Route('fixture','astr','fixture','fixture',AstrAdapter(self.model))
        self.plugin=SimpleNamespace(runtime_state_dir=self.root,_generation_v2_service=self.service)

    async def run_job(self):
        job=await self.service.start('batch','scope',self.route)
        status=await self.service.wait(job)
        self.assertEqual(status['stage'],'draft_ready',status)
        return job

    async def test_new_prompt_has_grounded_roles_and_time(self):
        await self.run_job()
        writing=next(c for c in self.model.calls if 'narratives' in c)
        facts=writing['narratives'][0]['facts']
        self.assertTrue(facts[0]['citations'])
        for f in facts:
            for c in f['citations']:
                self.assertIn(c['role'],('user','assistant'))
                self.assertIn('recorded_ts',c)
                self.assertIn('timezone',c)
        self.assertIn('Chinese',literary.WRITE_PROMPT)
        self.assertIn('not a list of turns',literary.WRITE_PROMPT)
        self.assertIn('not permission to invent',literary.WRITE_PROMPT)

    async def test_v1_reuses_cached_work_after_upgrade(self):
        with patch.object(literary,'WRITER_VERSION','literary-v1'):
            job=await self.run_job()
        count=len(self.model.calls)
        await self.run_job()
        self.assertEqual(len(self.model.calls),count)
        with self.store.connect() as db:
            contract=json.loads(db.execute('SELECT contract FROM writing_contracts WHERE job_id=?',(job,)).fetchone()[0])
        self.assertEqual(contract['writer'],'literary-v1')
        writing=next(c for c in self.model.calls if 'narratives' in c)
        self.assertNotIn('citations',writing['narratives'][0]['facts'][0])
        remote=Remote(); publisher=Publisher(self.store,remote,self.source)
        publisher.prepare(job,'scope'); await publisher.publish(job)
        self.assertEqual(remote.calls,1)

    async def test_review_rejection_requires_concrete_issue(self):
        original=self.model.text_chat
        async def malformed(*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
            if 'draft' in json.loads(prompt):
                return SimpleNamespace(completion_text='{"approved":false,"issues":[]}')
            return await original(prompt=prompt,contexts=contexts,system_prompt=system_prompt,
                                  request_timeout=request_timeout,request_max_retries=request_max_retries)
        self.model.text_chat=malformed
        job=await self.service.start('batch','scope',self.route)
        status=await self.service.wait(job)
        self.assertEqual(status['stage'],'awaiting_recovery')
        self.assertNotEqual(self.store.drafts(job)[0]['status'],'qualified')

    async def test_workbench_attempt_count_and_no_raw_prompt(self):
        await self.run_job()
        data=overview(self.plugin); job=data['jobs'][0]
        self.assertEqual(job['budget']['used'],4)
        self.assertEqual(len(job['attempts']),4)
        self.assertEqual(job['writer_version'],'literary-v3')
        self.assertTrue(all(a['outcome']=='succeeded' for a in job['attempts']))
        self.assertNotIn('system_prompt',json.dumps(job['attempts']))
        self.assertNotIn('route_id',json.dumps(job['attempts']))

    async def test_quality_runs_off_event_loop(self):
        import threading
        thread=threading.get_ident(); observed=[]; original=literary.quality
        def tracked(*args):
            observed.append(threading.get_ident()); return original(*args)
        with patch.object(literary,'quality',tracked): await self.run_job()
        self.assertTrue(observed)
        self.assertTrue(all(t!=thread for t in observed))

    async def delivery_setup(self):
        job=await self.run_job()
        Publisher(self.store,Remote(),self.source).prepare(job,'scope')
        integration.ensure_handoffs(self.store)
        with self.store.connect() as db:
            db.execute('INSERT INTO auto_handoffs(batch_id,scope,seq,source_kind,job_id,status,created) VALUES(?,?,?,?,?,?,?)',
                       ('batch','scope',2,'eod',job,'awaiting_recovery',1))
        self.plugin.generation_v2_memos_capabilities_confirmed=True
        self.plugin._compress_locks={}
        return job

    async def test_delivery_requires_confirmation(self):
        with self.assertRaises(ValueError): await integration.resume_delivery(self.plugin,{'job_id':'x'})

    async def test_delivery_cannot_start_draft_only_job(self):
        job=await self.run_job(); self.plugin.generation_v2_memos_capabilities_confirmed=True
        with self.assertRaises(integration.ConflictError):
            await integration.resume_delivery(self.plugin,{'job_id':job,'confirm_delivery':True})

    async def test_delivery_duplicate_clicks_share_task(self):
        job=await self.delivery_setup(); gate=asyncio.Event(); seen=[]
        async def fake(*args): seen.append(args); await gate.wait()
        with patch.object(integration,'produce',fake):
            first=await integration.resume_delivery(self.plugin,{'job_id':job,'confirm_delivery':True})
            second=await integration.resume_delivery(self.plugin,{'job_id':job,'confirm_delivery':True})
            task=self.service.delivery_tasks[job]
            gate.set(); await task
        self.assertEqual(first['status'],'delivery_queued')
        self.assertEqual(second['status'],'already_running')
        self.assertEqual(len(seen),1)
        self.assertEqual(len(self.model.calls),4)

    async def test_delivery_close_cancels_owned_task(self):
        job=await self.delivery_setup(); gate=asyncio.Event(); entered=asyncio.Event()
        async def fake(*args): entered.set(); await gate.wait()
        with patch.object(integration,'produce',fake):
            await integration.resume_delivery(self.plugin,{'job_id':job,'confirm_delivery':True})
            task=self.service.delivery_tasks[job]; await entered.wait()
            await self.service.close()
            self.assertTrue(task.cancelled())

    async def test_delivery_preserves_earlier_batch_order(self):
        job=await self.delivery_setup()
        with self.store.connect() as db:
            db.execute('INSERT INTO auto_handoffs(batch_id,scope,source_kind,created) VALUES(?,?,?,?)',('earlier','scope','eod',0))
        with patch.object(integration,'produce',new_callable=AsyncMock) as called:
            await integration.resume_delivery(self.plugin,{'job_id':job,'confirm_delivery':True})
            task=self.service.delivery_tasks[job]
            with self.assertRaises(integration.ConflictError): await task
            called.assert_not_called()


class StorageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.plugin=SimpleNamespace(runtime_state_dir=self.root,_log_event=Mock())

    def corrupt(self):
        (self.root/'generation_runtime.db').write_bytes(b'not a database')

    def test_corrupt_visibility_does_not_inject(self):
        self.corrupt()
        self.assertEqual(integration.visible_hits(self.plugin,[{'memo_name':'memos/x'}],'scope'),[])
        self.plugin._log_event.assert_called_once()

    def test_corrupt_search_does_not_break_chat(self):
        self.corrupt()
        self.assertEqual(integration.search_evidence(self.plugin,'scope','promise'),[])

    def test_corrupt_scope_is_not_guessed(self):
        self.corrupt()
        self.assertEqual(integration.sole_published_scope(self.plugin),'')

    def test_old_installation_without_ledger_keeps_recall(self):
        hits=[{'memo_name':'memos/old'}]
        self.assertEqual(integration.visible_hits(self.plugin,hits,'scope'),hits)

    def test_excessive_output_rejected_before_expensive_comparison(self):
        with patch.object(literary,'SequenceMatcher',side_effect=AssertionError('must not compare')):
            report=literary.quality({'title':'title','body':'I '*1000},'source',literary.WritingPolicy())
        self.assertEqual(report['blocking'],['excessive_length'])
