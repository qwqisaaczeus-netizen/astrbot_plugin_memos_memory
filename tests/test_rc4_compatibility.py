import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_generation_v2_test3 import archive,response
from test_generation_v2_test4 import Model
from astrbot_plugin_memos_memory.generation_v2.sources import load_batch,split_batch,shard_payload,SourceError
from astrbot_plugin_memos_memory.generation_v2.quote_grounding import resolve_quotes
from astrbot_plugin_memos_memory.generation_v2.recovery import control
from astrbot_plugin_memos_memory.generation_v2.coordinator import Coordinator
from astrbot_plugin_memos_memory.generation_v2.literary import DiaryStore
from astrbot_plugin_memos_memory.generation_v2.scheduler import Scheduler,Route
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.store import TaskSpec,encoded,ConflictError

class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source.db';archive(self.source)
        self.original=self.source.read_bytes()
        self.store=DiaryStore(self.root/'runtime.db')
        self.service=Coordinator(self.store,Scheduler(self.store),self.source)
        self.addAsyncCleanup(self.service.close)
        self.model=Model();self.route=Route('model','astr','fixture','fixture',AstrAdapter(self.model))
        self.plugin=SimpleNamespace(_generation_v2_service=self.service,_compress_locks={},runtime_state_dir=self.root)
        self.batch=load_batch(self.source,'batch','scope');self.shard=split_batch(self.batch,4000)[0]
        self.payload=shard_payload(self.batch,self.shard);self.payload['extraction_schema']='quote-v2'

    def result(self,kind='behavior'):
        obj=response(self.payload);obj['facts'][0]['kind']=kind
        return obj

    async def rejected(self):
        job=await self.service.start('batch','scope',self.route,job_cap=10,_launch=False)
        spec=TaskSpec(job+':extract:0',job,'episode_extract','scope','evidence-quotes-v2','grounded-evidence-v1','explicit-routes-v1',time.time()+60)
        self.store.create(spec,{'prompt':encoded(self.payload),'contexts':[],'system_prompt':'fixture'})
        self.store.attach(spec.task_id,{'primary':self.route.digest,'backup':None,'timeout':180,'same_fault_domain':False},10)
        owner=self.store.claim(spec.task_id,self.route.digest)
        self.store.finish(spec.task_id,owner,'output_rejected',{'validation':'rejected'}, {'text':encoded(self.result())})
        self.service._stage(job,'awaiting_recovery')
        return job

    async def test_aliases_audited_without_changing_citations(self):
        for kind in ('behavior','request','action','question',' EVENT ','事件'):
            obj=self.result(kind);result=resolve_quotes(encoded(obj),self.batch,self.shard)
            self.assertEqual(result['facts'][0]['kind'],'event')
            self.assertEqual(result['facts'][0]['basis'],obj['facts'][0]['basis'])
            self.assertEqual(result['facts'][0]['citations'][0]['quote'],obj['facts'][0]['citations'][0]['quote'])
            self.assertTrue(result['compatibility_repairs'])

    async def test_unknown_and_wrong_quote_still_rejected(self):
        with self.assertRaises(SourceError):resolve_quotes(encoded(self.result('unverifiable')),self.batch,self.shard)
        obj=self.result();obj['facts'][0]['citations'][0]['quote']='invented quotation'
        with self.assertRaises(SourceError):resolve_quotes(encoded(obj),self.batch,self.shard)

    async def test_revalidation_and_deadline_renewal_preserve_budget(self):
        job=await self.rejected()
        before=self.store.job(job)['used']
        first=await control(self.plugin,{'job_id':job,'action':'revalidate','confirm_local_revalidation':True})
        self.assertEqual(first['accepted_parts'],[0]);self.assertEqual(first['model_calls'],0)
        second=await control(self.plugin,{'job_id':job,'action':'revalidate','confirm_local_revalidation':True})
        self.assertEqual(second['already_verified'],[0]);self.assertEqual(self.store.job(job)['used'],before)
        with self.store.connect() as db:db.execute('UPDATE source_receipts SET deadline=1 WHERE job_id=?',(job,))
        with patch('astrbot_plugin_memos_memory.generation_v2.recovery.frozen_routes',return_value=(self.route,None,None)):
            result=await control(self.plugin,{'job_id':job,'action':'resume','renew_deadline':True,'confirm_model_calls':True})
        self.assertEqual(result['job_id'],job)
        self.assertEqual((await self.service.wait(job))['stage'],'draft_ready')
        self.assertEqual(len(self.model.calls),3)
        self.assertEqual(self.store.job(job)['used'],before+3)
        self.assertEqual(self.original,self.source.read_bytes())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM local_revalidations').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT outcome FROM attempts WHERE task_id=?',(job+':extract:0',)).fetchone()[0],'output_rejected')

    async def test_revalidation_requires_confirmation(self):
        job=await self.rejected()
        with self.assertRaises(ValueError):await control(self.plugin,{'job_id':job,'action':'revalidate'})

    async def test_typography_repair_still_passes_grounding(self):
        obj=self.result('event')
        citation=obj['facts'][0]['citations'][0]
        original=citation['quote']
        citation['quote']='\n'+original+'\n'
        result=resolve_quotes(encoded(obj),self.batch,self.shard)
        self.assertEqual(result['facts'][0]['citations'][0]['quote'],original)
        self.assertEqual(result['compatibility_repairs'][0]['method'],'typography')

    async def test_completed_write_cache_survives_deadline_renewal(self):
        job=await self.service.start('batch','scope',self.route,job_cap=10)
        self.assertEqual((await self.service.wait(job))['stage'],'draft_ready')
        used=self.store.job(job)['used'];calls=len(self.model.calls)
        with self.store.connect() as db:
            db.execute('UPDATE source_receipts SET deadline=1 WHERE job_id=?',(job,))
            db.execute('DELETE FROM diary_drafts WHERE job_id=?',(job,))
        self.service._stage(job,'awaiting_recovery')
        with patch('astrbot_plugin_memos_memory.generation_v2.recovery.frozen_routes',return_value=(self.route,None,None)):
            await control(self.plugin,{'job_id':job,'action':'resume','renew_deadline':True,'confirm_model_calls':True})
        self.assertEqual((await self.service.wait(job))['stage'],'draft_ready')
        self.assertEqual(self.store.job(job)['used'],used)
        self.assertEqual(len(self.model.calls),calls)
