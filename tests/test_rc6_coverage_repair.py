import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import test_generation_v2_test3 as fixture
from astrbot_plugin_memos_memory.generation_v2.coverage_repair import request, apply
from astrbot_plugin_memos_memory.generation_v2.sources import load_batch, split_batch, SourceError
from astrbot_plugin_memos_memory.generation_v2.production import ProductionStore, EvidencePipeline
from astrbot_plugin_memos_memory.generation_v2.scheduler import Scheduler, Route
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'source.db';fixture.archive(self.path,['我喝了水','我想更了解你'])
        self.batch=load_batch(self.path,'batch','scope');self.shard=split_batch(self.batch)[0]
        self.original={'facts':[{'id':'f1','kind':'event','claim':'喝水','basis':'explicit',
                        'citations':[{'span_id':'1:0:4','quote':'我喝了水'}]}],'omissions':[]}
        self.supplement={'facts':[{'id':'extra','kind':'relationship','claim':'想更了解对方','basis':'explicit',
                         'citations':[{'span_id':'2:0:6','quote':'我想更了解你'}]}],'omissions':[]}

    def payload(self):return request(json.dumps(self.original,ensure_ascii=False),self.batch,self.shard)
    def merge(self,payload=None):return apply(json.dumps(self.original,ensure_ascii=False),json.dumps(self.supplement,ensure_ascii=False),
                                           payload or self.payload(),self.batch,self.shard)

    def test_only_missing_owned_text_is_sent_and_all_evidence_survives(self):
        payload=self.payload();self.assertEqual([e['span_id'] for e in payload['missing_owned']],['2:0:6'])
        result=self.merge();self.assertEqual(len(result['facts']),2)
        self.assertEqual(result['facts'][0]['claim'],'喝水')
        self.assertEqual(result['coverage_repairs']['added_fact_ids'],['extra'])
        self.assertEqual({e['span_id'] for e in result['coverage']},{'1:0:4','2:0:6'})

    def test_cannot_overwrite_old_fact(self):
        self.supplement['facts'][0]['id']='f1'
        with self.assertRaises(SourceError):self.merge()

    def test_supplement_cannot_cite_existing_other_span(self):
        self.supplement['facts'][0]['citations']=[{'span_id':'1:0:4','quote':'我喝了水'}]
        with self.assertRaises(SourceError):self.merge()

    def test_payload_tampering_is_rejected(self):
        payload=self.payload();payload['missing_owned'][0]['text']='不同原文'
        with self.assertRaises(SourceError):self.merge(payload)

    def test_existing_bad_quote_must_be_repaired_first(self):
        self.original['facts'][0]['citations'][0]['quote']='我喝了茶'
        self.assertIsNone(self.payload())


class CoveragePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.archive=root/'source.db';fixture.archive(self.archive,['我喝了水','我想更了解你'])
        self.before=hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.store=ProductionStore(root/'ledger.db');self.scheduler=Scheduler(self.store)
        self.addAsyncCleanup(self.scheduler.close);self.pipeline=EvidencePipeline(self.store,self.scheduler,self.archive)

    def route(self,bad_quote=False):
        class Model:
            def __init__(self):self.calls=[]
            async def text_chat(self,prompt,contexts=None,system_prompt='',request_timeout=None,request_max_retries=None,**kwargs):
                payload=json.loads(prompt);self.calls.append(payload)
                if 'owned' in payload:
                    obj={'facts':[{'id':1,'kind':'event','claim':'喝水','basis':'explicit',
                                   'citations':[{'span_id':'1:0:4','quote':'我喝了茶' if bad_quote else '我喝了水'}]}],'omissions':[]}
                elif 'repairs' in payload:
                    obj={'repairs':[{'fact_id':'1','citation_index':0,'quote':'我喝了水'}]}
                elif 'missing_owned' in payload:
                    obj={'facts':[{'id':'extra','kind':'relationship','claim':'想更了解对方','basis':'explicit',
                                   'citations':[{'span_id':'2:0:6','quote':'我想更了解你'}]}],'omissions':[]}
                else:
                    obj={'narratives':[{'theme':'相处','rationale':'同一次交流','fact_ids':[f['id'] for f in payload['facts']]}]}
                return SimpleNamespace(completion_text=json.dumps(obj,ensure_ascii=False))
        model=Model();return model,Route('fixture','astr','fixture','fixture',AstrAdapter(model))

    async def test_real_pipeline_uses_one_bounded_supplement(self):
        model,route=self.route()
        plan=await self.pipeline.extract('batch','scope',route,job_cap=4)
        self.assertEqual(len(model.calls),3)
        self.assertEqual(len(plan['facts']),2)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(),self.before)

    async def test_quote_then_coverage_repair_are_bounded(self):
        model,route=self.route(True)
        plan=await self.pipeline.extract('batch','scope',route,job_cap=5)
        self.assertEqual(len(model.calls),4)
        self.assertEqual(len(plan['facts']),2)

    async def test_budget_still_refuses_next_provider_call(self):
        model,route=self.route(True)
        with self.assertRaises(RuntimeError):await self.pipeline.extract('batch','scope',route,job_cap=3)
        self.assertEqual(len(model.calls),3)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(),self.before)


if __name__=='__main__':unittest.main()
