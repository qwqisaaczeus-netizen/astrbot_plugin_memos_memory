import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_rc4_compatibility as helpers
from astrbot_plugin_memos_memory.generation_v2.literary import compact_inputs,fact_coverage
from astrbot_plugin_memos_memory.generation_v2.store import encoded
from astrbot_plugin_memos_memory.generation_v2.recovery import control


class WriterCapacityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=helpers.CompatibilityTests.asyncSetUp

    async def test_selective_prose_keeps_evidence_only_ledger(self):
        report={'blocking':[]}
        fact_coverage({'fact_ids':['a']},{'fact_ids':['a','b']},report)
        self.assertEqual(report['blocking'],[])
        self.assertEqual(report['evidence_only_fact_ids'],['b'])
        for links in ([],['a','a'],['foreign'],[{}]):
            report={'blocking':[]}
            fact_coverage({'fact_ids':links},{'fact_ids':['a','b']},report)
            self.assertEqual(report['blocking'],['fact_assignment_mismatch'])

    async def test_compaction_preserves_all_claims_and_existing_citations(self):
        original=[{'facts':[{'id':'f','claim':'claim','basis':'explicit','kind':'promise',
            'quotes':['duplicated'],'source_refs':[{'turn_id':1}],
            'citations':[{'role':'user','quote':'original','recorded_ts':1}]}]}]
        before=copy.deepcopy(original)
        packed,changed=compact_inputs(original,limit=1)
        self.assertTrue(changed);self.assertEqual(original,before)
        self.assertEqual(packed[0]['facts'][0]['citations'],original[0]['facts'][0]['citations'])
        self.assertEqual(packed[0]['facts'][0]['claim'],'claim')
        self.assertNotIn('quotes',packed[0]['facts'][0])

    async def run_batch(self,invalid=False):
        plan=await self.service.pipeline.extract('batch','scope',self.route,job_cap=10)
        group=plan['narratives'][0]
        plan['narratives']=[{**group,'fact_ids':[fact['id']]} for fact in plan['facts']]
        self.store.save_plan(plan['job_id'],plan)
        original=self.model.text_chat
        async def model(*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
            obj=json.loads(prompt)
            if 'drafts' in obj:
                self.model.calls.append(obj)
                rows=[{'index':d['index'],'approved':True,'issues':[]} for d in obj['drafts']]
                if invalid:rows[0]['index']=99
                return SimpleNamespace(completion_text=encoded({'reviews':rows}))
            return await original(prompt=prompt,contexts=contexts,system_prompt=system_prompt,
                                  request_timeout=request_timeout,request_max_retries=request_max_retries)
        self.model.text_chat=model
        with patch('astrbot_plugin_memos_memory.generation_v2.literary.compact_inputs',
                   side_effect=lambda value:compact_inputs(value,limit=1)):
            job=await self.service.start('batch','scope',self.route,job_cap=10)
            await self.service.wait(job)
        return job

    async def test_batch_review_keeps_independent_results_with_one_request(self):
        job=await self.run_batch()
        self.assertEqual(self.service.status(job)['stage'],'draft_ready')
        drafts=self.store.drafts(job)
        self.assertEqual(len(drafts),2)
        self.assertTrue(all(d['status']=='qualified' for d in drafts))
        self.assertEqual(self.store.job(job)['used'],4)
        self.assertEqual(len([c for c in self.model.calls if 'drafts' in c]),1)

    async def test_incomplete_batch_review_never_qualifies_drafts(self):
        job=await self.run_batch(invalid=True)
        self.assertEqual(self.service.status(job)['stage'],'awaiting_recovery')
        self.assertTrue(all(d['status']=='review_pending' for d in self.store.drafts(job)))
        self.assertEqual(self.store.job(job)['used'],4)

    async def test_cached_batch_review_reports_restore_without_calls(self):
        job=await self.run_batch()
        calls=len(self.model.calls)
        with self.store.connect() as db:
            db.execute("UPDATE diary_drafts SET status='review_pending' WHERE job_id=?",(job,))
        result=await control(self.plugin,{'job_id':job,'action':'revalidate','confirm_local_revalidation':True})
        self.assertEqual(result['reviews_reconciled'],[0,1])
        self.assertEqual(result['model_calls'],0)
        self.assertTrue(all(d['status']=='qualified' for d in self.store.drafts(job)))
        self.assertEqual(len(self.model.calls),calls)
