import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from test_generation_v2_test3 import archive, response
from astrbot_plugin_memos_memory.generation_v2.literary import DiaryStore, DiaryWriter, WritingPolicy, quality
from astrbot_plugin_memos_memory.generation_v2.production import EvidencePipeline
from astrbot_plugin_memos_memory.generation_v2.scheduler import Scheduler, Route
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.publishing import Publisher, UnknownWrite, utc_time, MemosPublisherAdapter
from astrbot_plugin_memos_memory.generation_v2.projection import EvidenceIndex
from astrbot_plugin_memos_memory.generation_v2.sources import SourceError
from astrbot_plugin_memos_memory.generation_v2.store import ConflictError


PROSE='I carried our small promise home quietly. It was not an answer to every worry, but I no longer had to pretend that waiting meant nothing to me.'


class Model:
    def __init__(self):
        self.calls=[]
        self.bad_first=False
        self.reject_review=False
        self.bad_all=False
    async def text_chat(self,*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
        p=json.loads(prompt); self.calls.append(p)
        if 'owned' in p: value=response(p)
        elif 'facts' in p:
            value={'narratives':[{'theme':'One experience','rationale':'continuous experience',
                'fact_ids':[f['id'] for f in p['facts']]}]}
        elif 'draft' in p:
            value={'approved':not self.reject_review,'issues':[] if not self.reject_review else
                   [{'code':'unsupported','detail':'unsupported emotion','fact_ids':p['narrative']['facts'][0:1] and [p['narrative']['facts'][0]['id']]}]}
        else:
            bad=self.bad_all or (self.bad_first and 'previous_draft' not in p)
            value={'drafts':[{'index':g['index'],'title':'An ordinary promise','body':'they said nothing' if bad else PROSE,
                             'fact_ids':[f['id'] for f in g['facts']]} for g in p['narratives']]}
        return SimpleNamespace(completion_text=json.dumps(value))


class Remote:
    capabilities_verified=True
    def __init__(self): self.memos={}; self.calls=0; self.lost=False; self.fail=False; self.wrong_date=False
    async def get(self,name): return self.memos.get(name)
    async def create(self,name,content,event_time):
        self.calls+=1
        if self.fail: raise TimeoutError()
        memo={'name':name,'content':content,'createTime':'2030-01-01T00:00:00Z' if self.wrong_date else event_time}
        self.memos[name]=memo
        if self.lost: raise TimeoutError()
        return memo


class QualityTests(unittest.TestCase):
    def test_empty(self): self.assertTrue(quality({'title':'x','body':''},'',WritingPolicy())['blocking'])
    def test_first_person(self): self.assertIn('first_person_missing',quality({'title':'x','body':'He sat quietly.'},'',WritingPolicy())['blocking'])
    def test_length(self): self.assertIn('excessive_length',quality({'title':'x','body':'I '+'x'*1801},'',WritingPolicy())['blocking'])
    def test_copy(self): self.assertIn('source_copy_risk',quality({'title':'x','body':PROSE},PROSE,WritingPolicy())['blocking'])
    def test_stage(self): self.assertIn('stage_direction_chain',quality({'title':'x','body':'I (walked across the room and sat quietly at the old wooden table)'},'',WritingPolicy())['blocking'])
    def test_short_not_discarded(self): self.assertFalse(quality({'title':'x','body':'I will keep that promise.'},'',WritingPolicy())['blocking'])
    def test_policy_validation(self):
        with self.assertRaises(ValueError): WritingPolicy(target_min=1000,target_max=100)
    def test_unknown_event_date(self):
        with self.assertRaises(SourceError): utc_time(None)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'source.db'; archive(self.path)
        self.store=DiaryStore(Path(self.tmp.name)/'runtime.db')
        self.scheduler=Scheduler(self.store); self.addAsyncCleanup(self.scheduler.close)
        self.model=Model(); self.route=Route('model','astr','fixture','fixture',AstrAdapter(self.model))
        self.pipeline=EvidencePipeline(self.store,self.scheduler,self.path)
        self.writer=DiaryWriter(self.store,self.scheduler,self.path)
        self.plan=await self.pipeline.extract('batch','scope',self.route)
        self.job=self.plan['job_id']; self.remote=Remote(); self.pub=Publisher(self.store,self.remote,self.path)
        self.index=EvidenceIndex(self.store)

    async def draft(self): return await self.writer.write(self.job,'scope',self.route)
    async def publish(self):
        await self.draft(); self.pub.prepare(self.job,'scope'); return await self.pub.publish(self.job)

    async def test_write_review_and_reuse(self):
        rows=await self.draft(); n=len(self.model.calls)
        await self.draft()
        self.assertEqual(len(self.model.calls),n)
        self.assertEqual(n,4) # extraction + writing + new-prompt review
        self.assertEqual(rows[0]['status'],'qualified')

    async def test_repair_only_failed_draft(self):
        self.model.bad_first=True
        rows=await self.draft()
        self.assertEqual(rows[0]['revision'],1)
        self.assertEqual(rows[0]['status'],'qualified')
        self.assertEqual(sum('previous_draft' in x for x in self.model.calls),1)

    async def test_repair_exhausted_preserves_source(self):
        before=self.path.read_bytes(); self.model.bad_all=True
        with self.assertRaises(SourceError): await self.draft()
        with self.assertRaises(ConflictError): self.pub.prepare(self.job,'scope')
        self.assertEqual(before,self.path.read_bytes()); self.assertEqual(self.remote.calls,0)
        self.assertEqual(self.store.drafts(self.job)[0]['revision'],1)

    async def test_semantic_rejection_blocks(self):
        self.model.reject_review=True
        with self.assertRaises(SourceError): await self.draft()
        with self.assertRaises(ConflictError): self.pub.prepare(self.job,'scope')

    async def test_contract_change_rejected(self):
        await self.draft()
        with self.assertRaises(ConflictError):
            await self.writer.write(self.job,'scope',self.route,policy=WritingPolicy(character='other'))

    async def test_source_changed_blocks_publish(self):
        await self.draft()
        db=sqlite3.connect(self.path); db.execute('UPDATE source_turns SET event_ts=event_ts+1'); db.commit(); db.close()
        with self.assertRaises(SourceError): self.pub.prepare(self.job,'scope')

    async def test_changed_plan_cannot_reuse_qualified_draft(self):
        await self.draft()
        self.plan['narratives'][0]['theme']='different theme'
        self.store.save_plan(self.job,self.plan)
        with self.assertRaises(ConflictError): self.pub.prepare(self.job,'scope')
        with self.assertRaises(ConflictError): await self.draft()

    async def test_unknown_write_reconciles_without_create(self):
        await self.draft(); self.pub.prepare(self.job,'scope'); self.remote.lost=True
        with self.assertRaises(TimeoutError): await self.pub.publish(self.job)
        self.assertEqual(self.index.records('scope'),[])
        self.remote.lost=False
        await self.pub.publish(self.job)
        self.assertEqual(self.remote.calls,1)
        await self.pub.deliver_index(self.job,self.index)
        self.assertEqual(len(self.index.records('scope')),2)
        self.assertEqual(self.index.records('another'),[])

    async def test_unknown_absent_never_blind_recreates(self):
        await self.draft(); self.pub.prepare(self.job,'scope'); self.remote.fail=True
        with self.assertRaises(TimeoutError): await self.pub.publish(self.job)
        self.remote.fail=False
        with self.assertRaises(UnknownWrite): await self.pub.publish(self.job)
        self.assertEqual(self.remote.calls,1)

    async def test_user_edit_conflict_no_overwrite(self):
        await self.publish()
        next(iter(self.remote.memos.values()))['content']='user edit'
        with self.assertRaises(ConflictError): await self.pub.publish(self.job)
        self.assertEqual(self.remote.calls,1)

    async def test_wrong_calendar_date_blocks(self):
        self.remote.wrong_date=True
        with self.assertRaises(UnknownWrite): await self.publish()
        self.assertEqual(self.index.records('scope'),[])

    async def test_calendar_source_date(self):
        await self.publish()
        memo=next(iter(self.remote.memos.values()))
        self.assertEqual(memo['createTime'],utc_time(1790600000))

    async def test_edit_between_remote_and_index_blocks_activation(self):
        await self.publish()
        next(iter(self.remote.memos.values()))['content']='edited'
        with self.assertRaises(ConflictError): await self.pub.deliver_index(self.job,self.index)
        self.assertEqual(self.index.records('scope'),[])

    async def test_redelivery_does_not_reactivate_superseded_generation(self):
        await self.publish(); await self.pub.deliver_index(self.job,self.index)
        with self.store.connect() as db:
            db.execute('UPDATE published_generations SET active=0 WHERE job_id=?',(self.job,))
        await self.pub.deliver_index(self.job,self.index)
        self.assertEqual(self.index.records('scope'),[])

    async def test_two_diaries_partial_remote_write_stays_invisible(self):
        group=self.plan['narratives'][0]
        self.plan['narratives']=[{**group,'fact_ids':[self.plan['facts'][0]['id']]},
                                {**group,'fact_ids':[self.plan['facts'][1]['id']]}]
        self.store.save_plan(self.job,self.plan)
        await self.draft(); self.pub.prepare(self.job,'scope')
        original=self.remote.create
        async def second_fails(*args):
            if self.remote.calls==1: self.remote.fail=True
            return await original(*args)
        self.remote.create=second_fails
        with self.assertRaises(TimeoutError): await self.pub.publish(self.job)
        self.assertEqual(len(self.remote.memos),1)
        self.assertEqual(self.index.records('scope'),[])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM published_generations').fetchone()[0],0)

    async def test_evidence_outside_prose_still_indexed(self):
        self.plan['narratives'][0]['fact_ids']=[self.plan['facts'][0]['id']]
        self.store.save_plan(self.job,self.plan)
        await self.publish(); await self.pub.deliver_index(self.job,self.index)
        rows=self.index.records('scope')
        self.assertEqual(len(rows),2)
        self.assertEqual(sum(json.loads(r['memo_names'])==[] for r in rows),1)

    async def test_index_failure_then_retry_no_model_or_remote(self):
        await self.publish()
        n=len(self.model.calls)
        class Broken:
            async def apply(self,*args): raise OSError('disk')
        with self.assertRaises(OSError): await self.pub.deliver_index(self.job,Broken())
        self.assertEqual(self.index.records('scope'),[])
        await self.pub.deliver_index(self.job,self.index)
        await self.pub.deliver_index(self.job,self.index)
        self.assertEqual(self.remote.calls,1); self.assertEqual(len(self.model.calls),n)
        self.assertEqual(len(self.index.records('scope')),2)

    async def test_state_failure_does_not_republish(self):
        await self.publish(); await self.pub.deliver_index(self.job,self.index)
        class Broken:
            async def apply(self,*args): raise OSError()
        with self.assertRaises(OSError): await self.pub.deliver_state(self.job,Broken())
        self.assertEqual(len(self.index.records('scope')),2); self.assertEqual(self.remote.calls,1)

    async def test_noop_index_cannot_activate(self):
        await self.publish()
        class Noop:
            async def apply(self,*args): pass
            async def verify(self,*args): return False
        with self.assertRaises(ConflictError): await self.pub.deliver_index(self.job,Noop())
        self.assertEqual(self.index.records('scope'),[])

    async def test_local_rollback_preserves_remote_and_source(self):
        before=self.path.read_bytes()
        await self.publish(); await self.pub.deliver_index(self.job,self.index)
        self.pub.rollback_local(self.job)
        self.assertEqual(self.index.records('scope'),[])
        self.assertEqual(len(self.remote.memos),1)
        self.assertEqual(self.path.read_bytes(),before)
        with self.assertRaises(ConflictError): self.pub.rollback_local(self.job)

    async def test_unverified_service_no_write(self):
        self.remote.capabilities_verified=False
        with self.assertRaises(ConflictError): await self.publish()
        self.assertEqual(self.remote.calls,0)

    async def test_concurrent_publication_owner(self):
        await self.draft(); self.pub.prepare(self.job,'scope')
        self.pub._claim(self.job,'worker')
        with self.assertRaises(ConflictError): await self.pub.publish(self.job)
        self.pub.recover_after_restart(self.job)
        await self.pub.publish(self.job)
        self.assertEqual(self.remote.calls,1)

    async def test_restart_unknown_never_recreates(self):
        await self.draft(); self.pub.prepare(self.job,'scope')
        self.pub._status(self.job,0,'sending')
        self.pub.recover_after_restart(self.job)
        with self.assertRaises(UnknownWrite): await self.pub.publish(self.job)
        self.assertEqual(self.remote.calls,0)


class MemosHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_id_and_event_time_wire_contract(self):
        from aiohttp import web
        from astrbot_plugin_memos_memory.memos_client import MemosClient
        seen=[]
        async def create(request):
            data=await request.json(); seen.append((dict(request.query),data))
            return web.json_response({'name':'memos/'+request.query['memoId'],**data})
        app=web.Application(); app.router.add_post('/api/v1/memos',create)
        runner=web.AppRunner(app); await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port=site._server.sockets[0].getsockname()[1]
        client=MemosClient(f'http://127.0.0.1:{port}')
        self.addAsyncCleanup(client.close)
        adapter=MemosPublisherAdapter(client,capabilities_verified=True)
        result=await adapter.create('memos/known','diary','2026-09-27T01:00:00Z')
        self.assertEqual(result['name'],'memos/known')
        self.assertEqual(seen[0][0],{'memoId':'known'})
        self.assertEqual(seen[0][1]['createTime'],'2026-09-27T01:00:00Z')
        self.assertEqual(seen[0][1]['visibility'],'PRIVATE')


if __name__=='__main__': unittest.main()
