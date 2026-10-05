import copy
import json
import unittest

import test_generation_v2_linked as linked
from astrbot_plugin_memos_memory.generation_v2.bindings import final_groups, item_fact_ids, verify_plan_evidence
from astrbot_plugin_memos_memory.generation_v2.literary import apply_review
from astrbot_plugin_memos_memory.generation_v2.local_revision import revise
from astrbot_plugin_memos_memory.generation_v2.publishing import Publisher
from astrbot_plugin_memos_memory.generation_v2.sources import load_batch, SourceError
from astrbot_plugin_memos_memory.generation_v2.store import ConflictError


def sample():
    facts=[{'id':i,'citations':[{'event_ts':t}]} for i,t in [('a',100),('b',200),('c',300),('d',400)]]
    groups=[{'theme':'first','fact_ids':['a','b'],'event_start':100,'event_end':200},
            {'theme':'second','fact_ids':['c','d'],'event_start':300,'event_end':400}]
    plan={'facts':facts,'narratives':groups}
    drafts=[{'index':0,'fact_ids':['a']},{'index':1,'fact_ids':['b','c']}]
    return plan,drafts


class BindingTests(unittest.TestCase):
    def test_memos_second_precision_retains_exact_calendar_second(self):
        item={'name':'memos/test','content':'verified','event_time':'2026-09-26T07:04:44.388346Z'}
        for value in ('2026-09-26T07:04:44Z','2026-09-26T15:04:44+08:00'):
            self.assertTrue(Publisher.matches(item,{'name':item['name'],'content':item['content'],'createTime':value}))
        for value in ('2026-09-26T07:04:43Z','2026-09-26T07:04:45Z','2026-09-27T07:04:44Z',
                      '2026-09-26T07:04:44','invalid'):
            self.assertFalse(Publisher.matches(item,{'name':item['name'],'content':item['content'],'createTime':value}))

    def test_moved_fact_belongs_to_actual_prose(self):
        plan,drafts=sample(); original=copy.deepcopy(plan)
        bound=final_groups(plan,drafts)
        self.assertEqual(plan,original)
        self.assertEqual(bound[0]['fact_ids'],['a'])
        self.assertEqual(bound[1]['fact_ids'],['b','c','d'])
        self.assertEqual(bound[1]['prose_fact_ids'],['b','c'])
        self.assertEqual(bound[1]['evidence_only_fact_ids'],['d'])
        self.assertEqual(bound[1]['rebound_fact_ids'],['b'])
        self.assertEqual((bound[1]['event_start'],bound[1]['event_end']),(200,300))

    def test_unknown_duplicate_empty_ids_block(self):
        for links in (['unknown'],['a','a'],[],[{}]):
            plan,drafts=sample();drafts[0]['fact_ids']=links
            with self.assertRaises(SourceError):final_groups(plan,drafts)

    def test_foreign_duplicate_missing_indices_block(self):
        for index in (1,99,True):
            plan,drafts=sample();drafts[0]['index']=index
            with self.assertRaises(SourceError):final_groups(plan,drafts)

    def test_old_manifest_falls_back_without_rebinding(self):
        plan,_=sample()
        self.assertEqual(item_fact_ids({'ordinal':0},plan),['a','b'])
        self.assertEqual(item_fact_ids({'ordinal':0,'fact_ids':['b']},plan),['b'])

    def review(self,code,severity,legacy=False):
        report={'blocking':[],'warnings':[]}
        apply_review(report,{'approved':False,'issues':[{'code':code,'severity':severity,
                     'detail':'concrete issue','fact_ids':['a']}]},{'fact_ids':['a']},allow_advisory=not legacy)
        return report

    def test_order_advice_does_not_rewrite_good_prose(self):
        r=self.review('narrative_order','advisory')
        self.assertEqual(r['blocking'],[])
        self.assertFalse(r['machine_review_passed'])
        self.assertFalse(r['semantic_verified'])
        self.assertTrue(r['advisory_issues'])

    def test_factual_defects_cannot_hide_as_advisory(self):
        for code in ('wrong_time','wrong_speaker','invented_fact','wrong_order','unknown'):
            self.assertTrue(self.review(code,'advisory')['blocking'])

    def test_legacy_review_not_silently_reinterpreted(self):
        self.assertTrue(self.review('narrative_order','advisory',legacy=True)['blocking'])

    def test_order_with_false_causality_still_blocks(self):
        self.assertTrue(self.review('narrative_order','blocking')['blocking'])


class EditorialTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=linked.LinkedTests.asyncSetUp
    async def setup_draft(self):
        job=await self.service.start('batch','scope',self.route)
        await self.service.wait(job)
        rows=self.store.drafts(job)
        with self.store.connect() as db:
            workflow=json.loads(db.execute('SELECT contract FROM workflow_runs WHERE job_id=?',(job,)).fetchone()[0])
        request={'request_id':'editorial-test','confirm_local_revision':True,'confirm_fact_review':True,
                 'drafts':[{**json.loads(r['draft']),'expected_revision':r['revision'],
                            'review_reason':'Checked the original source, dates and speaker ownership.'} for r in rows]}
        return job,workflow,request

    async def test_zero_call_revision_preserves_history_and_budget(self):
        job,workflow,request=await self.setup_draft()
        calls=len(self.model.calls);used=self.store.job(job)['used'];before=self.source.read_bytes()
        result=revise(self.service,job,workflow,request)
        self.assertEqual(result['model_calls'],0)
        self.assertEqual(len(self.model.calls),calls)
        self.assertEqual(self.store.job(job)['used'],used)
        self.assertEqual(self.source.read_bytes(),before)
        row=self.store.drafts(job)[0]
        self.assertEqual(row['revision'],1)
        report=json.loads(row['report'])
        self.assertTrue(report['operator_reviewed'])
        self.assertFalse(report['machine_review_passed'])
        self.assertEqual(revise(self.service,job,workflow,request),result)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM diary_drafts WHERE job_id=?',(job,)).fetchone()[0],2)

    async def test_local_revalidation_keeps_editorial_decision(self):
        job,workflow,request=await self.setup_draft()
        revise(self.service,job,workflow,request)
        from astrbot_plugin_memos_memory.generation_v2.revalidation import revalidate
        revalidate(self.service,job,workflow)
        self.assertTrue(json.loads(self.store.drafts(job)[0]['report'])['operator_reviewed'])

    async def test_bad_correction_and_stale_revision_block(self):
        job,workflow,request=await self.setup_draft()
        for change in ({'fact_ids':['foreign']},{'body':'He waited.'},{'expected_revision':99}):
            bad=copy.deepcopy(request);bad['drafts'][0].update(change)
            with self.assertRaises((SourceError,ConflictError)):revise(self.service,job,workflow,bad)

    async def test_confirmation_and_audit_identity_required(self):
        job,workflow,request=await self.setup_draft()
        for key in ('confirm_fact_review','confirm_local_revision','request_id'):
            bad=copy.deepcopy(request);bad.pop(key)
            with self.assertRaises(ValueError):revise(self.service,job,workflow,bad)
        revise(self.service,job,workflow,request)
        request['drafts'][0]['body']+=' I waited.'
        with self.assertRaises(ConflictError):revise(self.service,job,workflow,request)

    async def test_published_revision_immutable(self):
        job,workflow,request=await self.setup_draft()
        self.pub.prepare(job,'scope')
        with self.assertRaises(ConflictError):revise(self.service,job,workflow,request)

    async def test_source_reference_must_match(self):
        job,workflow,_=await self.setup_draft()
        plan=self.store.plan(job);batch=load_batch(self.source,'batch','scope')
        verify_plan_evidence(plan,batch)
        for field,value in [('quote','invented'),('role','system'),('event_ts',0),('turn_id',999)]:
            bad=copy.deepcopy(plan);bad['facts'][0]['citations'][0][field]=value
            with self.assertRaises(SourceError):verify_plan_evidence(bad,batch)
        bad=copy.deepcopy(plan);bad['facts'][0]['claim']='changed claim'
        with self.assertRaises(SourceError):verify_plan_evidence(bad,batch)

    async def test_manifest_binding_not_initial_plan_drives_projection(self):
        job,workflow,request=await self.setup_draft()
        revise(self.service,job,workflow,request)
        manifest=self.pub.prepare(job,'scope')
        self.assertTrue(manifest['items'][0]['prose_fact_ids'])
        await self.pub.publish(job)
        await self.pub.deliver_index(job,self.service.index)
        self.assertTrue(await self.service.index.verify(job+':episode_index',manifest))
        with self.store.connect() as db:
            db.execute("UPDATE v2_evidence_index SET memo_names='[]' WHERE job_id=?",(job,))
        self.assertFalse(await self.service.index.verify(job+':episode_index',manifest))
