import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_rc4_compatibility as helpers
from astrbot_plugin_memos_memory.generation_v2.citation_repair import request,apply
from astrbot_plugin_memos_memory.generation_v2.recovery import control
from astrbot_plugin_memos_memory.generation_v2.store import encoded
from astrbot_plugin_memos_memory.generation_v2.sources import SourceError


class CitationRepairTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=helpers.CompatibilityTests.asyncSetUp
    rejected=helpers.CompatibilityTests.rejected

    def result(self,kind='event'):
        obj=helpers.CompatibilityTests.result(self,kind)
        obj['facts'][0]['citations'][0]['quote']='I pledge to return tomorrow.'
        return obj

    async def test_partial_repair_cannot_change_claims_or_accept_fabrication(self):
        text=encoded(self.result());payload=request(text,self.batch,self.shard)
        key=payload['repairs'][0]
        fix={'fact_id':key['fact_id'],'citation_index':key['citation_index'],'quote':key['owned_source']}
        result=apply(text,encoded({'repairs':[fix]}),payload,self.batch,self.shard)
        self.assertEqual(result['facts'][0]['claim'],self.result()['facts'][0]['claim'])
        self.assertTrue(result['citation_repairs'])
        with self.assertRaises(SourceError):apply(text,encoded({'repairs':[{**fix,'claim':'different'}]}),payload,self.batch,self.shard)
        with self.assertRaises(SourceError):apply(text,encoded({'repairs':[{**fix,'quote':'invented'}]}),payload,self.batch,self.shard)

    async def test_invalid_json_never_starts_citation_repair(self):
        for text in ('not JSON','[]','{"facts": NaN}','{"facts":[],"facts":[]}'):
            self.assertIsNone(request(text,self.batch,self.shard))

    async def test_resume_repairs_once_same_budget_preserves_old_artifact(self):
        job=await self.rejected()
        original=self.model.text_chat
        async def repaired_model(*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
            payload=json.loads(prompt)
            if 'repairs' in payload:
                self.model.calls.append(payload)
                return SimpleNamespace(completion_text=encoded({'repairs':[
                    {'fact_id':r['fact_id'],'citation_index':r['citation_index'],'quote':r['owned_source']}
                    for r in payload['repairs']]}))
            return await original(prompt=prompt,contexts=contexts,system_prompt=system_prompt,
                                  request_timeout=request_timeout,request_max_retries=request_max_retries)
        self.model.text_chat=repaired_model
        with patch('astrbot_plugin_memos_memory.generation_v2.recovery.frozen_routes',return_value=(self.route,None,None)):
            await control(self.plugin,{'job_id':job,'action':'resume','confirm_model_calls':True})
        state=await self.service.wait(job)
        self.assertEqual(state['stage'],'draft_ready')
        self.assertEqual(self.store.job(job)['used'],5)
        self.assertEqual(len([c for c in self.model.calls if 'repairs' in c]),1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE id=?',(job+':extract:0',)).fetchone()[0],'output_rejected')
        self.assertEqual(self.original,self.source.read_bytes())
