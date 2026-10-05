import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_generation_v2_test3 import archive
from test_generation_v2_test3 import response
from test_generation_v2_test4 import Model, Remote
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.coordinator import Coordinator
from astrbot_plugin_memos_memory.generation_v2.literary import DiaryStore
from astrbot_plugin_memos_memory.generation_v2.scheduler import Route, Scheduler
from astrbot_plugin_memos_memory.generation_v2.publishing import Publisher
from astrbot_plugin_memos_memory.generation_v2 import integration as i
from astrbot_plugin_memos_memory.generation_v2.adapters import classify_error


class LinkedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.source=root/'source.db'
        archive(self.source)
        self.store=DiaryStore(root/'generation_runtime.db')
        self.service=Coordinator(self.store,Scheduler(self.store),self.source)
        self.addAsyncCleanup(self.service.close)
        self.model=Model()
        self.route=Route('model','astr','fixture','fixture',AstrAdapter(self.model))
        self.remote=Remote()
        self.pub=Publisher(self.store,self.remote,self.source)
        self.plugin=SimpleNamespace(runtime_state_dir=root,_generation_v2_service=self.service)

    async def published(self):
        job=await self.service.start('batch','scope',self.route)
        await self.service.wait(job)
        self.pub.prepare(job,'scope')
        await self.pub.publish(job)
        return job

    async def test_old_extraction_handoff_never_auto_restarts_after_upgrade(self):
        job=await self.service.start('batch','scope',self.route,_launch=False)
        i.ensure_handoffs(self.store)
        with self.store.connect() as db:
            row=db.execute('SELECT contract FROM source_receipts WHERE job_id=?',(job,)).fetchone()
            contract=json.loads(row[0]); contract.pop('extraction_version',None)
            db.execute('UPDATE source_receipts SET contract=? WHERE job_id=?',(json.dumps(contract),job))
            db.execute('INSERT INTO auto_handoffs(batch_id,scope,seq,source_kind,job_id,created) VALUES(?,?,?,?,?,0)',
                       ('batch','scope',10,'eod',job))
        self.plugin.generation_v2_memos_capabilities_confirmed=True
        self.plugin._log_event=lambda *a:None
        self.plugin._vec=SimpleNamespace(buffer_drop=AsyncMock())
        self.assertEqual(await i.produce(self.plugin,'scope',[],10,'eod',3),0)
        self.assertEqual(self.model.calls,[])
        self.plugin._vec.buffer_drop.assert_not_awaited()
        self.assertEqual(i.pending(self.plugin,'scope')['status'],'manual_review')

    async def test_staged_evidence_never_searchable(self):
        job=await self.published()
        self.assertEqual(i.search_evidence(self.plugin,'scope','promise'),[])
        with self.store.connect() as db:
            manifest=json.loads(db.execute('SELECT manifest FROM publish_batches WHERE job_id=?',(job,)).fetchone()[0])
        await self.service.index.apply('staged',manifest)
        self.assertEqual(i.search_evidence(self.plugin,'scope','promise'),[])
        hit={'memo_name':manifest['items'][0]['name']}
        self.assertEqual(i.visible_hits(self.plugin,[hit],'scope'),[])

    async def test_active_fact_search_and_scope(self):
        job=await self.published()
        await self.pub.deliver_index(job,self.service.index)
        facts=self.store.plan(job)['facts']
        hits=i.search_evidence(self.plugin,'scope',facts[0]['claim'])
        self.assertTrue(hits)
        self.assertTrue(hits[0]['_v2_evidence'])
        self.assertEqual(i.search_evidence(self.plugin,'foreign',facts[0]['claim']),[])
        self.assertEqual(i.visible_hits(self.plugin,hits,'foreign'),[])
        self.assertEqual(i.visible_hits(self.plugin,hits,'scope'),hits)
        i.attach_evidence(hits[0])
        self.assertTrue(hits[0]['_fusion_event_core'])
        self.assertTrue(hits[0]['_fusion_source_evidence'])
        await self.pub.deliver_index(job,self.service.index)
        self.assertEqual(len(self.service.index.records('scope')),len(facts))

    async def test_rollback_removes_candidates(self):
        job=await self.published()
        await self.pub.deliver_index(job,self.service.index)
        query=self.store.plan(job)['facts'][0]['claim']
        hits=i.search_evidence(self.plugin,'scope',query)
        self.pub.rollback_local(job)
        self.assertEqual(i.search_evidence(self.plugin,'scope',query),[])
        self.assertEqual(i.visible_hits(self.plugin,hits,'scope'),[])

    def test_inference_is_labelled(self):
        hit={'_v2_evidence':[{'claim':'他可能愿意','basis':'inference',
            'citations':[{'turn_id':1,'quote':'以后再说','role':'user'}]}]}
        i.attach_evidence(hit,False)
        self.assertIn('非已确认事实',hit['_fusion_event_core'][0])
        self.assertEqual(hit['_fusion_source_evidence'],[])

    def test_incomplete_http_payload_is_transport_failure(self):
        from aiohttp import ClientPayloadError
        failure=classify_error(ClientPayloadError('response body interrupted'))
        self.assertEqual(failure.kind,'transport')
        self.assertTrue(failure.possibly_billed)

    async def test_actual_compression_entry_all_three_sources(self):
        from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
        plugin=SimpleNamespace(character_name='角色',runtime_state_dir=None,
            generation_v2_enable=True,_episodes=object(),_vec=object(),_memos=object(),diary_count_max_cap=3)
        messages=[{'role':'user','content':'a'},{'role':'assistant','content':'b'}]*20
        with patch.object(i,'produce',new_callable=AsyncMock,return_value=2) as produce:
            for kind in ('auto','eod','manual'):
                result=await MemosMemoryPlugin._compress_and_store(plugin,'scope',messages,source_kind=kind,buffer_up_to_seq=40)
                self.assertEqual(result,2)
                self.assertEqual(produce.call_args.args[4:6],(kind,3))

    async def test_toggle_off_keeps_pending_batch_on_new_owner(self):
        from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
        plugin=SimpleNamespace(character_name='角色',runtime_state_dir=None,
            generation_v2_enable=False,_episodes=object(),_vec=object(),_memos=object(),diary_count_max_cap=3)
        with patch.object(i,'pending',return_value={'batch_id':'old'}), \
             patch.object(i,'produce',new_callable=AsyncMock,return_value=1) as produce:
            self.assertEqual(await MemosMemoryPlugin._compress_and_store(plugin,'scope',
                [{'role':'assistant','content':'x'}]*20),1)
            produce.assert_awaited_once()

    async def test_actual_fusion_uses_grounded_claims(self):
        from astrbot_plugin_memos_memory.main import MemosMemoryPlugin
        hit={'_v2_evidence':[{'claim':'约定明天来','basis':'explicit',
            'citations':[{'turn_id':1,'quote':'明天我来','role':'user'}]}]}
        MemosMemoryPlugin._prepare_fused_memory_hit(SimpleNamespace(), '明天',hit)
        self.assertIn('约定明天来',hit['_fusion_event_core'][0])
        self.assertIn('明天我来',hit['_fusion_source_evidence'][0])
        hit['retrieval_key']='old diary '*1000
        self.assertIn('约定明天来',MemosMemoryPlugin._hit_evidence_text(hit)[:700])

    async def test_delivery_recovery_does_not_regenerate_or_drop_new_messages(self):
        job=await self.published()
        i.ensure_handoffs(self.store)
        with self.store.connect() as db:
            db.execute('INSERT INTO auto_handoffs(batch_id,scope,seq,source_kind,job_id,created) VALUES(?,?,?,?,?,0)',
                       ('batch','scope',10,'eod',job))
        episodes=SimpleNamespace(db_path=self.source,mark_batch=lambda *a:None)
        vec=SimpleNamespace(buffer_drop=AsyncMock(),buffer_take=AsyncMock(return_value=[{'seq':11}]))
        self.plugin._episodes=episodes
        self.plugin._vec=vec
        self.plugin._buffer={}
        self.plugin._memos=self.remote
        self.plugin.generation_v2_memos_capabilities_confirmed=True
        self.plugin._log_event=lambda *a:None
        before=len(self.model.calls)
        with patch.object(i,'MemosPublisherAdapter',side_effect=lambda remote,**kw:remote), \
             patch.object(i,'ConsumerIndex',side_effect=lambda plugin,index:index), \
             patch.object(i,'StateConsumer',return_value=SimpleNamespace(apply=AsyncMock())):
            count=await i.produce(self.plugin,'scope',[{'seq':11}],11,'auto',3)
        self.assertEqual(count,1)
        self.assertEqual(before,len(self.model.calls))
        vec.buffer_drop.assert_awaited_once_with('scope',10)
        self.assertEqual(self.plugin._buffer['scope'],[{'seq':11}])
        self.assertIsNone(i.pending(self.plugin,'scope'))

    async def test_state_failure_keeps_ownership_and_buffer(self):
        job=await self.published()
        i.ensure_handoffs(self.store)
        with self.store.connect() as db:
            db.execute('INSERT INTO auto_handoffs(batch_id,scope,seq,source_kind,job_id,created) VALUES(?,?,?,?,?,0)',
                       ('batch','scope',10,'eod',job))
        self.plugin._episodes=SimpleNamespace(db_path=self.source)
        self.plugin._vec=SimpleNamespace(buffer_drop=AsyncMock())
        self.plugin._memos=self.remote
        self.plugin.generation_v2_memos_capabilities_confirmed=True
        self.plugin._log_event=lambda *a:None
        with patch.object(i,'MemosPublisherAdapter',side_effect=lambda remote,**kw:remote), \
             patch.object(i,'ConsumerIndex',side_effect=lambda plugin,index:index), \
             patch.object(i,'StateConsumer',return_value=SimpleNamespace(apply=AsyncMock(side_effect=RuntimeError()))):
            self.assertEqual(await i.produce(self.plugin,'scope',[],10,'eod',3),0)
        self.plugin._vec.buffer_drop.assert_not_awaited()
        handoff=i.pending(self.plugin,'scope')
        self.assertEqual(handoff['status'],'awaiting_recovery')
        self.assertEqual(handoff['job_id'],job)

    async def test_real_episode_store_retains_fact_omitted_from_prose(self):
        from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
        episodes=EpisodicStore(str(Path(self.temp.name)/'real_archive.db'),2,'fixture')
        await episodes.init()
        self.addCleanup(episodes.close)
        batch=episodes.archive_batch('scope',[
            {'role':'user','content':'我答应明天回来。','event_ts':1790600000},
            {'role':'assistant','content':'我会记得把蓝色雨伞放在门边。','event_ts':1790600010}], 'eod')
        service=Coordinator(self.store,self.service.scheduler,episodes.db_path)
        class OmittedModel(Model):
            async def text_chat(self,*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
                payload=json.loads(prompt)
                if 'facts' in payload:
                    return SimpleNamespace(completion_text=json.dumps({'narratives':[{'theme':'One promise',
                        'rationale':'promise selected; umbrella retained as evidence','fact_ids':[payload['facts'][0]['id']]}]}))
                if 'owned' not in payload:
                    return await super().text_chat(prompt=prompt,contexts=contexts,system_prompt=system_prompt)
                data=response(payload)
                data['facts'][1]['claim']='蓝色雨伞放在门边'
                return SimpleNamespace(completion_text=json.dumps(data))
        route=Route('omitted','astr','fixture','fixture',AstrAdapter(OmittedModel()))
        job=await service.start(batch,'scope',route)
        self.assertEqual((await service.wait(job))['stage'],'draft_ready')
        pub=Publisher(self.store,self.remote,episodes.db_path)
        pub.prepare(job,'scope')
        await pub.publish(job)
        indexed={}
        async def index_memo(memo):
            indexed[memo['name']]=memo
            return True
        plugin=SimpleNamespace(_episodes=episodes,_vec=SimpleNamespace(get_memo_meta=indexed.get),
            _embed=AsyncMock(return_value=[1.,0.]),_index_memo_record=index_memo,
            _content_hash=lambda text:hashlib.sha256(text.encode()).hexdigest())
        sink=i.ConsumerIndex(plugin,service.index)
        await pub.deliver_index(job,sink)
        hits=service.index.search('scope','蓝色雨伞')
        self.assertEqual(len(hits),1)
        name=hits[0]['memo_name']
        stored=episodes.get_episode(name)
        self.assertEqual(stored['source_batch_id'],batch)
        self.assertIn('蓝色雨伞',stored['card_text'])
        i.attach_evidence(hits[0])
        self.assertIn('蓝色雨伞',hits[0]['_fusion_event_core'][0])
        self.assertTrue(episodes.evidence_for_memo(name,'蓝色雨伞',limit=3))
        before=plugin._embed.await_count
        with self.store.connect() as db:
            manifest=json.loads(db.execute('SELECT manifest FROM publish_batches WHERE job_id=?',(job,)).fetchone()[0])
        await sink.apply(job+':episode_index',manifest)
        self.assertEqual(plugin._embed.await_count,before)
        from astrbot_plugin_memos_memory.main import _parse_memo_content
        self.assertEqual(stored['diary_content_hash'],plugin._content_hash(_parse_memo_content(manifest['items'][0]['content'])[1]))
        with sqlite3.connect(episodes.db_path) as db:
            db.execute("UPDATE episodes SET evidence_quality='mixed_user_edited',diary_render_version='' WHERE memo_name=?",(name,))
        db.close()
        self.assertFalse(await sink.verify(job+':episode_index',manifest))
        await pub.deliver_index(job,sink)
        self.assertTrue(await sink.verify(job+':episode_index',manifest))
        self.assertEqual(episodes.get_episode(name)['evidence_quality'],'source_grounded')


if __name__=='__main__': unittest.main()
