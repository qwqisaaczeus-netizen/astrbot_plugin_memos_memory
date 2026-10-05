import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_generation_v2_test3 as source_fixture
import test_generation_v2_test4 as workflow_fixture
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter, DirectAdapter, production_route
from astrbot_plugin_memos_memory.generation_v2.bindings import final_groups, verify_plan_evidence, retain_verified_prose_links
from astrbot_plugin_memos_memory.generation_v2.claim_audit import apply, request
from astrbot_plugin_memos_memory.generation_v2.coordinator import Coordinator
from astrbot_plugin_memos_memory.generation_v2.integration import new_production_options
from astrbot_plugin_memos_memory.generation_v2.literary import DiaryStore, natural_paragraphs, fact_coverage, compact_inputs
from astrbot_plugin_memos_memory.generation_v2.projection import EvidenceIndex, safe_claim, projection_basis
from astrbot_plugin_memos_memory.generation_v2.publishing import Publisher
from astrbot_plugin_memos_memory.generation_v2.runner import SingleAttemptRunner
from astrbot_plugin_memos_memory.generation_v2.scheduler import Route, Scheduler
from astrbot_plugin_memos_memory.generation_v2.sources import SourceError, load_batch
from astrbot_plugin_memos_memory.generation_v2.store import TaskSpec, encoded, ConflictError
from astrbot_plugin_memos_memory.call_policy import defaults


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.batch=SimpleNamespace(revision='revision',turns=[])
        quote='我吃了你煮的粥，等会儿想去院子坐坐。'
        self.facts=[{'id':'a','kind':'event','basis':'explicit','claim':'她煮了粥并去了院子。',
                     'citations':[{'quote':quote,'role':'assistant','turn_id':1,'event_ts':100}],
                     'evidence_id':'old','semantic_verified':False}]
        self.row={'id':'a','status':'corrected','claim':'她吃了对方煮的粥，打算去院子坐坐。',
                  'basis':'explicit','actor':'assistant','modality':'intended','reason':'原文是吃粥与打算，非煮粥与完成。'}

    def test_actor_modality_correction_preserves_original(self):
        result=apply(encoded({'facts':[self.row]}),self.facts,self.batch)[0]
        self.assertIn('打算',result['claim'])
        self.assertEqual(result['claim_audit']['original_claim'],self.facts[0]['claim'])
        self.assertEqual(result['citations'],self.facts[0]['citations'])
        self.assertNotEqual(result['evidence_id'],'old')
        self.assertFalse(result['semantic_verified'])
        self.assertEqual(self.facts[0]['evidence_id'],'old')

    def test_uncertain_keeps_quote_not_bad_claim(self):
        self.row['status']='uncertain'
        result=apply(encoded({'facts':[self.row]}),self.facts,self.batch)[0]
        self.assertEqual(result['certainty'],'unconfirmed')
        self.assertIn('我吃了你煮的粥',safe_claim(result))
        self.assertNotIn('她煮了粥',safe_claim(result))
        self.assertEqual(projection_basis(result),'inference')

    def test_cannot_add_or_omit_ids(self):
        for rows in ([],[self.row,self.row],[{**self.row,'id':'foreign'}]):
            with self.assertRaises(SourceError):apply(encoded({'facts':rows}),self.facts,self.batch)

    def test_accepted_cannot_silently_change_claim(self):
        with self.assertRaises(SourceError):apply(encoded({'facts':[{**self.row,'status':'accepted'}]}),self.facts,self.batch)

    def test_invalid_modality(self):
        with self.assertRaises(SourceError):apply(encoded({'facts':[{**self.row,'modality':'maybe'}]}),self.facts,self.batch)

    def test_basis_in_modality_preserves_claim_and_unknown_completion(self):
        row={'id':'a','status':'accepted','actor':'unknown','modality':'inference'}
        result=apply(encoded({'facts':[row]}),self.facts,self.batch)[0]
        self.assertEqual(result['claim'],self.facts[0]['claim'])
        self.assertEqual(result['basis'],self.facts[0]['basis'])
        self.assertEqual(result['claim_audit']['modality'],'unknown')
        self.assertEqual(result['claim_audit']['compatibility_repairs'][0]['received'],'inference')
        self.assertFalse(result['semantic_verified'])
        self.assertEqual(row['modality'],'inference')

    def test_compatibility_cannot_accept_changed_claim_or_missing_actor(self):
        for row in ({**self.row,'status':'accepted','modality':'inference'},
                    {'id':'a','status':'accepted','modality':'inference'}):
            with self.assertRaises(SourceError):apply(encoded({'facts':[row]}),self.facts,self.batch)

    def test_capacity_does_not_truncate(self):
        with self.assertRaises(SourceError):request(self.facts*513,self.batch)
        self.batch.turns=[SimpleNamespace(id=1,role='assistant',content='x'*120001)]
        with self.assertRaises(SourceError):request(self.facts,self.batch)

    def test_prose_association_is_not_coverage(self):
        plan={'claim_audit':{'version':'claim-audit-v1'},'facts':self.facts,
              'narratives':[{'theme':'a','rationale':'a','fact_ids':['a']}]}
        draft={'index':0,'body':'我还惦记着那碗粥。','fact_ids':['a']}
        group=final_groups(plan,[draft])[0]
        self.assertEqual(group['prose_fact_ids'],[])
        self.assertEqual(group['reference_fact_ids'],['a'])
        report={'blocking':[],'warnings':[]}
        fact_coverage(draft,group,report)
        self.assertFalse(report['semantic_coverage_verified'])
        self.assertIn('association_only_no_prose_links',report['warnings'])

    def test_body_link_exact_and_no_foreign_fact(self):
        plan={'claim_audit':{},'facts':self.facts,'narratives':[{'fact_ids':['a']}]}
        plan['claim_audit']={'version':'v1'}
        draft={'index':0,'body':'我吃了那碗粥。','fact_ids':['a'],
               'prose_links':[{'fact_id':'a','quote':'我吃了那碗粥'}]}
        self.assertEqual(final_groups(plan,[draft])[0]['prose_fact_ids'],['a'])
        draft['prose_links'][0]['quote']='我自己煮了粥'
        with self.assertRaises(SourceError):final_groups(plan,[draft])

    def test_unmatched_body_link_is_reference_not_prose_and_body_unchanged(self):
        draft={'index':0,'body':'我吃了那碗粥。','fact_ids':['a'],
               'prose_links':[{'fact_id':'a','quote':'我自己煮了粥'}]}
        corrected=retain_verified_prose_links(draft,{'a'})
        plan={'claim_audit':{'version':'v1'},'facts':self.facts,'narratives':[{'fact_ids':['a']}]}
        group=final_groups(plan,[corrected])[0]
        self.assertEqual(group['prose_fact_ids'],[])
        self.assertEqual(group['reference_fact_ids'],['a'])
        self.assertEqual(corrected['body'],draft['body'])
        self.assertEqual(corrected['prose_link_rejections'][0]['disposition'],'reference_only')
        self.assertEqual(retain_verified_prose_links(corrected,{'a'}),corrected)
        self.assertEqual(len(draft['prose_links']),1)

    def test_unmatched_body_link_foreign_and_malformed_still_reject(self):
        for row in ({'fact_id':'foreign','quote':'我吃了粥'}, {'fact_id':'a','quote':''},
                    {'fact_id':'a','quote':None}):
            with self.assertRaises(SourceError):retain_verified_prose_links({'body':'我吃了粥','prose_links':[row]},{'a'})

    def test_uncertain_cannot_bind_to_prose(self):
        self.facts[0]['claim_audit']={'status':'uncertain'}
        with self.assertRaises(SourceError):final_groups({'facts':self.facts,'narratives':[{'fact_ids':['a']}]},
                                                          [{'index':0,'fact_ids':['a']}])

    def test_paragraphs_preserve_all_nonwhitespace_characters(self):
        body=''.join('我把第'+str(i)+'件小事记在心里，不急着替它下结论。' for i in range(40))
        result=natural_paragraphs(body)
        self.assertIn('\n\n',result)
        self.assertEqual(''.join(result.split()),''.join(body.split()))
        self.assertEqual(natural_paragraphs(result),result)
        self.assertEqual(natural_paragraphs('我在等。'),'我在等。')

    def test_writer_compaction_preserves_claims_citations_and_audit_semantics(self):
        inputs=[{'facts':[{'id':'a','claim':'她吃了对方煮的粥。','basis':'explicit','kind':'event',
            'citations':[{'quote':'你煮的粥','role':'assistant','turn_id':1}],
            'quotes':['你煮的粥'],'source_refs':[{'turn_id':1}],
            'claim_audit':{'status':'corrected','actor':'她','modality':'completed',
                'original_claim':'她煮了粥。','reason':'Eating is not cooking.','machine_review_only':True}}]}]
        original=copy.deepcopy(inputs)
        packed,changed=compact_inputs(inputs,limit=1)
        self.assertTrue(changed)
        self.assertEqual(inputs,original)
        fact=packed[0]['facts'][0]
        for key in ('id','claim','basis','kind','citations'):
            self.assertEqual(fact[key],original[0]['facts'][0][key])
        self.assertEqual(fact['claim_audit'],{'status':'corrected','actor':'她','modality':'completed'})
        self.assertNotIn('quotes',fact)
        self.assertNotIn('source_refs',fact)

    def test_profile_honors_explicit_controls_and_legacy_identity(self):
        adapter=AstrAdapter(object(),policies={'diary_write':{**defaults('diary_write'),'thinking':'enabled','max_tokens':65536}})
        route=Route('p','astr','fixture','fixture',adapter)
        balanced=production_route(route,'balanced-v1')
        self.assertIs(production_route(route,'legacy'),route)
        self.assertEqual(adapter.policies['episode_extract']['thinking'],'enabled')
        self.assertEqual(balanced.adapter.policies['episode_extract']['thinking'],'disabled')
        self.assertEqual(balanced.adapter.policies['narrative_plan']['max_tokens'],4096)
        self.assertEqual(balanced.adapter.policies['diary_write']['max_tokens'],65536)
        self.assertNotEqual(route.digest,balanced.digest)
        self.assertEqual(production_route(route,'balanced-v1').digest,balanced.digest)

    def test_single_chinese_character_has_lexical_index_but_multiword_stays_bigram(self):
        terms=EvidenceIndex.lexical_text('She ate porridge.',[{'quote':'我吃了粥'}]).split()
        self.assertIn(EvidenceIndex.terms('粥')[0],terms)
        self.assertEqual(EvidenceIndex.terms('椅子'),['椅子'])


class AuditedModel(workflow_fixture.Model):
    def __init__(self):
        super().__init__();self.uncertain=False;self.invalid_audit=False;self.multi=False
    async def text_chat(self,*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
        payload=json.loads(prompt)
        if 'context' in payload and 'facts' in payload:
            self.calls.append(payload)
            rows=[{'id':f['id'],'status':'uncertain' if self.uncertain else 'accepted','claim':f['claim'],
                   'basis':f['basis'],'actor':'unknown','modality':'reported','reason':'scripted offline review'}
                  for f in payload['facts']]
            if self.invalid_audit:rows=[]
            return SimpleNamespace(completion_text=encoded({'facts':rows}))
        if 'drafts' in payload:
            self.calls.append(payload)
            return SimpleNamespace(completion_text=encoded({'reviews':[{'index':d['index'],'approved':True,'issues':[]}
                                                                       for d in payload['drafts']]}))
        if self.multi and 'facts' in payload and 'owned' not in payload:
            self.calls.append(payload)
            return SimpleNamespace(completion_text=encoded({'narratives':[{'theme':'scene '+str(i),'rationale':'distinct experience',
                'fact_ids':[f['id']]} for i,f in enumerate(payload['facts'])]}))
        response=await super().text_chat(prompt=prompt,contexts=contexts,system_prompt=system_prompt,
                                       request_timeout=request_timeout,request_max_retries=request_max_retries)
        if 'prose_links' in system_prompt and 'narratives' in payload and 'draft' not in payload:
            obj=json.loads(response.completion_text)
            for draft in obj['drafts']:
                draft['prose_links']=[{'fact_id':draft['fact_ids'][0],'quote':'I carried our small promise home quietly.'}]
            response.completion_text=encoded(obj)
        return response


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.archive=root/'source.db';source_fixture.archive(self.archive)
        self.original_hash=hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.store=DiaryStore(root/'generation_runtime.db')
        self.service=Coordinator(self.store,Scheduler(self.store),self.archive)
        self.addAsyncCleanup(self.service.close)
        self.model=AuditedModel();self.route=Route('model','astr','fixture','fixture',AstrAdapter(self.model))

    async def start(self,**kwargs):
        job=await self.service.start('batch','scope',self.route,semantic_audit=True,job_cap=7,**kwargs)
        await self.service.wait(job);return job

    async def test_retained_audit_revalidation_avoids_another_model_request(self):
        with patch('astrbot_plugin_memos_memory.generation_v2.claim_audit.apply',
                   side_effect=SourceError('old parser rejected compatible response')):
            job=await self.start()
        self.assertEqual(self.service.status(job)['stage'],'awaiting_recovery')
        before=len([p for p in self.model.calls if 'context' in p and 'facts' in p])
        self.assertEqual(await self.start(),job)
        self.assertEqual(self.service.status(job)['stage'],'draft_ready')
        self.assertEqual(len([p for p in self.model.calls if 'context' in p and 'facts' in p]),before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks WHERE id=?',(job+':semantic-audit',)).fetchone()[0],
                             'output_rejected')

    async def test_retained_extraction_revalidation_avoids_another_model_request(self):
        with patch('astrbot_plugin_memos_memory.generation_v2.production.resolve_quotes',
                   side_effect=SourceError('old parser rejected compatible response')):
            job=await self.start()
        self.assertEqual(self.service.status(job)['stage'],'awaiting_recovery')
        before=len([p for p in self.model.calls if 'owned' in p])
        self.assertEqual(await self.start(),job)
        self.assertEqual(self.service.status(job)['stage'],'draft_ready')
        self.assertEqual(len([p for p in self.model.calls if 'owned' in p]),before)

    async def test_audited_writer_large_input_is_bounded_not_truncated(self):
        job=await self.start()
        plan=self.store.plan(job)
        plan['narratives'][0]['rationale']='r'*120001
        with self.store.connect() as db:
            db.execute('UPDATE narrative_previews SET result=? WHERE job_id=?',(encoded(plan),job))
            db.execute('DELETE FROM writing_contracts WHERE job_id=?',(job,))
        before=len(self.model.calls)
        with self.assertRaisesRegex(SourceError,'writing input exceeds capacity'):
            await self.service.writer.write(job,'scope',self.route)
        self.assertEqual(len(self.model.calls),before)
        self.assertEqual(self.store.plan(job)['narratives'][0]['rationale'],plan['narratives'][0]['rationale'])

    async def test_audit_write_publish_search_and_restart_no_extra_requests(self):
        job=await self.start()
        self.assertEqual(self.service.status(job)['stage'],'draft_ready')
        plan=self.store.plan(job);self.assertTrue(plan['claim_audit'])
        self.assertEqual({fid for entry in plan['coverage'] for fid in entry['fact_ids']},
                         {fact['id'] for fact in plan['facts']})
        verify_plan_evidence(plan,load_batch(self.archive,'batch','scope'))
        with self.store.connect() as db:
            writer=json.loads(db.execute('SELECT contract FROM writing_contracts WHERE job_id=?',(job,)).fetchone()[0])['writer']
        self.assertEqual(writer,'literary-v4')
        remote=workflow_fixture.Remote();pub=Publisher(self.store,remote,self.archive)
        pub.prepare(job,'scope');await pub.publish(job);await pub.deliver_index(job,self.service.index)
        hits=self.service.index.search('scope',plan['facts'][0]['citations'][0]['quote'])
        self.assertTrue(hits);self.assertFalse(self.service.index.search('foreign','promise'))
        count=len(self.model.calls)
        await self.start();await pub.publish(job);await pub.deliver_index(job,self.service.index)
        self.assertEqual(len(self.model.calls),count);self.assertEqual(remote.calls,1)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(),self.original_hash)

    async def test_audit_failure_retains_original_no_draft_no_publish(self):
        self.model.invalid_audit=True
        job=await self.start()
        self.assertEqual(self.service.status(job)['stage'],'awaiting_recovery')
        self.assertEqual(self.store.drafts(job),[])
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(),self.original_hash)
        self.assertEqual(len(self.model.calls),2)

    async def test_all_uncertain_retained_not_written(self):
        self.model.uncertain=True;job=await self.start()
        self.assertEqual(self.service.status(job)['stage'],'awaiting_recovery')
        plan=self.store.plan(job);self.assertTrue(plan['facts']);self.assertEqual(plan['narratives'],[])
        self.assertEqual(self.store.drafts(job),[])

    async def test_insufficient_budget_fails_before_first_request(self):
        with self.assertRaises(ConflictError):
            await self.service.start('batch','scope',self.route,semantic_audit=True,job_cap=4)
        self.assertEqual(self.model.calls,[])

    async def test_old_receipt_keeps_legacy_options(self):
        await self.service.start('batch','scope',self.route,_launch=False)
        plugin=SimpleNamespace(generation_v2_claim_audit=True,generation_v2_call_profile='balanced-v1')
        self.assertEqual(new_production_options(plugin,self.store,'batch','scope'),{})
        self.assertEqual(new_production_options(plugin,self.store,'other','scope')['call_profile'],'balanced-v1')

    async def test_actual_webui_draft_defaults_include_audit_and_do_not_publish(self):
        from astrbot_plugin_memos_memory.generation_v2.plugin_gateway import start_draft
        plugin=SimpleNamespace(runtime_state_dir=Path(self.tmp.name),_generation_v2_service=self.service,
            _episodes=SimpleNamespace(db_path=self.archive),character_name='Fixture',rp_time_timezone='UTC',
            context=SimpleNamespace(get_provider_by_id=lambda _:self.model))
        result=await start_draft(plugin,{'confirm_model_calls':True,'batch_id':'batch','provider_id':'fixture'})
        await self.service.wait(result['job_id'])
        self.assertEqual(self.service.status(result['job_id'])['stage'],'draft_ready')
        self.assertEqual(result['memos_writes'],0)
        self.assertTrue(self.store.plan(result['job_id'])['claim_audit'])
        with self.store.connect() as db:
            options=json.loads(db.execute('SELECT contract FROM workflow_runs WHERE job_id=?',
                                          (result['job_id'],)).fetchone()[0])['options']
        self.assertEqual(options['call_profile'],'balanced-v1')

    async def test_automatic_handoff_uses_same_audit_before_buffer_ack(self):
        from unittest.mock import AsyncMock,patch
        from astrbot_plugin_memos_memory.generation_v2 import integration
        marked=[];remote=workflow_fixture.Remote()
        plugin=SimpleNamespace(runtime_state_dir=Path(self.tmp.name),_generation_v2_service=self.service,
            generation_v2_memos_capabilities_confirmed=True,character_name='Fixture',rp_time_timezone='UTC',
            _episodes=SimpleNamespace(db_path=self.archive,archive_batch=lambda *args:'batch',
                batch_info=lambda _: {'status':'archived'},mark_batch=lambda *args:marked.append(args)),
            _memos=object(),_buffer={'scope':[]},_log_event=lambda *args:None,
            _vec=SimpleNamespace(buffer_drop=AsyncMock(),buffer_take=AsyncMock(return_value=[])))
        sink=SimpleNamespace(apply=AsyncMock(),verify=AsyncMock(return_value=True))
        with patch.object(integration,'route_for',return_value=(self.route,None)), \
             patch.object(integration,'stage_routes_for',return_value=None), \
             patch.object(integration,'MemosPublisherAdapter',return_value=remote), \
             patch.object(integration,'ConsumerIndex',return_value=self.service.index), \
             patch.object(integration,'StateConsumer',return_value=sink):
            count=await integration.produce(plugin,'scope',[],2,'eod',3)
        self.assertEqual(count,1)
        plugin._vec.buffer_drop.assert_awaited_once_with('scope',2)
        self.assertEqual(marked,[('batch','committed')])
        self.assertEqual(remote.calls,1)
        with self.store.connect() as db:
            job=db.execute('SELECT job_id FROM workflow_runs').fetchone()[0]
        self.assertTrue(self.store.plan(job)['claim_audit'])

    async def test_v4_batch_review_restores_after_interruption_without_calls(self):
        from astrbot_plugin_memos_memory.generation_v2.review_cache import restore
        from astrbot_plugin_memos_memory.generation_v2.recovery import saved_workflow
        self.model.multi=True;job=await self.start()
        self.assertEqual(self.service.status(job)['stage'],'draft_ready')
        self.assertEqual(len(self.model.calls),5)
        with self.store.connect() as db:
            db.execute("UPDATE diary_drafts SET report='{}',status='review_pending' WHERE job_id=?",(job,))
        restored=restore(self.service,job,saved_workflow(self.service,job),load_batch(self.archive,'batch','scope'))
        self.assertEqual(restored,[0,1]);self.assertEqual(len(self.model.calls),5)
        self.assertTrue(all(r['status']=='qualified' for r in self.store.drafts(job)))

    async def test_explicit_reextract_can_upgrade_old_job_without_editing_it(self):
        from unittest.mock import patch
        from astrbot_plugin_memos_memory.generation_v2 import recovery
        old=await self.service.start('batch','scope',self.route,_launch=False)
        with self.store.connect() as db:
            contract=db.execute('SELECT contract FROM source_receipts WHERE job_id=?',(old,)).fetchone()[0]
        plugin=SimpleNamespace(_generation_v2_service=self.service,_compress_locks={})
        with patch.object(recovery,'frozen_routes',return_value=(self.route,None,None)):
            child=await recovery.control(plugin,{'job_id':old,'action':'reextract','confirm_model_calls':True,
                'request_id':'rc6-upgrade-test','job_cap':8,'upgrade_production_contract':True})
        await self.service.wait(child['job_id'])
        self.assertEqual(self.service.status(child['job_id'])['stage'],'draft_ready')
        self.assertTrue(self.store.plan(child['job_id'])['claim_audit'])
        with self.store.connect() as db:
            self.assertEqual(contract,db.execute('SELECT contract FROM source_receipts WHERE job_id=?',(old,)).fetchone()[0])

    async def test_old_fts_upgrade_adds_quote_terms_without_changing_claims(self):
        job=await self.start();pub=Publisher(self.store,workflow_fixture.Remote(),self.archive)
        pub.prepare(job,'scope');await pub.publish(job);await pub.deliver_index(job,self.service.index)
        with self.store.connect() as db:
            old=[tuple(r) for r in db.execute('SELECT * FROM v2_evidence_index')]
            db.execute("DELETE FROM v2_projection_versions WHERE name='quote_fts'")
            db.execute('UPDATE v2_evidence_fts SET terms=?',('obsolete',))
        EvidenceIndex(self.store);EvidenceIndex(self.store)
        with self.store.connect() as db:
            self.assertEqual(old,[tuple(r) for r in db.execute('SELECT * FROM v2_evidence_index')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM v2_evidence_fts').fetchone()[0],len(old))
        self.assertTrue(self.service.index.search('scope',self.store.plan(job)['facts'][0]['citations'][0]['quote']))


class LoopbackFaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from astrbot_plugin_memos_memory.direct_llm import OpenAICompatibleTextClient
        self.mode='ok';self.requests=0
        async def handler(request):
            self.requests+=1
            if request.path.startswith('/astr/'):
                return web.json_response({'error':{'message':'local overload','type':'server_error'}},status=503)
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(request)
            try:
                if self.mode=='empty_transport':
                    request.transport.close();return response
                if self.mode=='heartbeat':
                    for _ in range(10):
                        await response.write(b': ping\n\n');await asyncio.sleep(.1)
                    return response
                for piece in ('hello ','world'):
                    await response.write(('data: '+encoded({'choices':[{'delta':{'content':piece}}]})+'\n\n').encode())
                    if self.mode=='long':await asyncio.sleep(.2)
                if self.mode=='interrupted':
                    request.transport.close();return response
                reason='length' if self.mode=='truncated' else 'stop'
                await response.write(('data: '+encoded({'choices':[{'delta':{},'finish_reason':reason}],
                    'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}})+'\n\ndata: [DONE]\n\n').encode())
                await response.write_eof()
            except (ConnectionError,RuntimeError):pass
            return response
        app=web.Application();app.router.add_post('/chat/completions',handler)
        app.router.add_post('/astr/chat/completions',handler)
        self.server=web.AppRunner(app);await self.server.setup();self.addAsyncCleanup(self.server.cleanup)
        site=web.TCPSite(self.server,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        self.base_url='http://127.0.0.1:'+str(port)
        self.assertNotEqual(port,8689)
        self.client=OpenAICompatibleTextClient('local-fixture')
        self.client.configure(base_url='http://127.0.0.1:'+str(port),model='deepseek-v4-pro',api_key='fake-local-key')
        async def close():
            if self.client._http:await self.client._http.close()
        self.addAsyncCleanup(close)

    async def invoke(self,timeout=.25):
        from astrbot_plugin_memos_memory.generation_v2.transport import invoke
        return await invoke(self.client,{'prompt':'local fixture'},timeout,{**defaults('diary_write'),'idle_timeout':.35})

    async def test_meaningful_stream_survives_initial_deadline(self):
        self.mode='long';result=await self.invoke(.25)
        self.assertEqual(result.completion_text,'hello world');self.assertEqual(self.requests,1)

    async def test_heartbeats_do_not_extend_deadline(self):
        self.mode='heartbeat'
        with self.assertRaises(TimeoutError):await self.invoke(.25)
        self.assertEqual(self.requests,1)

    async def test_interrupted_and_truncated_never_success_partial_retained(self):
        for mode in ('interrupted','truncated'):
            self.mode=mode
            with self.assertRaises(Exception) as result:await self.invoke(1)
            self.assertEqual(result.exception.partial_text,'hello world')
        self.assertEqual(self.requests,2)

    async def test_partial_is_private_artifact_not_publishable_success(self):
        self.mode='truncated'
        with tempfile.TemporaryDirectory() as root:
            store=DiaryStore(Path(root)/'runtime.db');spec=TaskSpec('partial','job','diary_write','scope','v1','v1','v1',time.time()+10)
            store.create(spec,{'prompt':'local fixture','contexts':[],'system_prompt':''})
            store.attach('partial',{},job_cap=3)
            adapter=DirectAdapter(self.client,policies={})
            with self.assertRaises(Exception):await SingleAttemptRunner(store).run('partial',adapter,route_identity='local',timeout=2)
            row=store.read('partial');self.assertEqual(row['status'],'awaiting_recovery')
            artifact=store.artifact(row['output_id']);self.assertFalse(artifact['publishable'])
            self.assertEqual(artifact['text'],'hello world');self.assertEqual(self.requests,1)

    async def test_followup_is_one_budgeted_dispatch_after_transport_failure(self):
        self.mode='empty_transport'
        class Primary:
            async def text_chat(self,*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
                raise ConnectionError('scripted no-output transport failure')
        with tempfile.TemporaryDirectory() as root:
            store=DiaryStore(Path(root)/'runtime.db');scheduler=Scheduler(store)
            try:
                primary=Route('astr','astr','primary','primary',AstrAdapter(Primary()))
                backup=Route('direct','direct','loopback','loopback',DirectAdapter(self.client,policies={}))
                spec=TaskSpec('followup','job','diary_write','scope','v1','v1','v1',time.time()+15)
                store.create(spec,{'prompt':'local fixture','contexts':[],'system_prompt':''})
                self.mode='ok'
                result=await scheduler.submit('followup',primary,backup,timeout=2,job_cap=3)
                self.assertEqual(result['text'],'hello world');self.assertEqual(self.requests,1)
                self.assertEqual(store.job('job')['used'],3)  # One primary retry, then one follow-up.
            finally:await scheduler.close()

    async def test_actual_astr_sdk_overload_to_direct_http_followup(self):
        from openai import AsyncOpenAI
        from unittest.mock import AsyncMock
        sdk=AsyncOpenAI(api_key='fake-local-key',base_url=self.base_url+'/astr/',max_retries=2)
        provider=SimpleNamespace(client=sdk,provider_config={},_prepare_chat_payload=AsyncMock(return_value=(
            {'model':'deepseek-v4-pro','messages':[{'role':'user','content':'local fixture'}]},[])))
        try:
            with tempfile.TemporaryDirectory() as root:
                store=DiaryStore(Path(root)/'runtime.db');scheduler=Scheduler(store)
                try:
                    primary=Route('astr-sdk','astr','loopback-primary','primary',AstrAdapter(provider,policies={}))
                    backup=Route('direct','direct','loopback-backup','backup',DirectAdapter(self.client,policies={}))
                    store.create(TaskSpec('sdk-followup','job','diary_write','scope','v1','v1','v1',time.time()+20),
                                 {'prompt':'local fixture','contexts':[],'system_prompt':''})
                    result=await scheduler.submit('sdk-followup',primary,backup,timeout=3,job_cap=3)
                    self.assertEqual(result['text'],'hello world');self.assertEqual(self.requests,2)
                    self.assertEqual(store.job('job')['used'],2)
                    self.assertEqual(sdk.max_retries,2)  # Shared client was not mutated; no hidden SDK retries.
                finally:await scheduler.close()
        finally:await sdk.close()
