import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from astrbot_plugin_memos_memory.generation_v2.sources import load_batch, split_batch, shard_payload, SourceError
from astrbot_plugin_memos_memory.generation_v2.evidence import parse_object,validate_extraction,validate_plan
from astrbot_plugin_memos_memory.generation_v2.production import ProductionStore,EvidencePipeline
from astrbot_plugin_memos_memory.generation_v2.scheduler import Scheduler,Route
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.upgrades import stage_upgrades,bind_upgrade_plan
from astrbot_plugin_memos_memory.generation_v2.store import ConflictError


def archive(path,texts=None):
    texts=texts or ['I promise to return tomorrow.','I will wait for you.']
    db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE source_batches(batch_id TEXT,session_id TEXT,message_count INTEGER);
        CREATE TABLE source_turns(id INTEGER,batch_id TEXT,turn_index INTEGER,role TEXT,content TEXT,
            event_ts REAL,event_timezone TEXT,content_hash TEXT);
        CREATE TABLE episodes(episode_id TEXT,memo_name TEXT,source_batch_id TEXT,active INTEGER,
            scene_start_turn INTEGER,scene_end_turn INTEGER,memory_type TEXT);''')
    db.execute('INSERT INTO source_batches VALUES(?,?,?)',('batch','scope',len(texts)))
    for i,text in enumerate(texts):
        db.execute('INSERT INTO source_turns VALUES(?,?,?,?,?,?,?,?)',
                   (i+1,'batch',i,'user' if i%2==0 else 'assistant',text,
                    1790600000+i*3600,'Asia/Shanghai',hashlib.sha256(text.encode()).hexdigest()))
    db.execute("INSERT INTO episodes VALUES('old','memos/old','batch',1,0,1,'relationship')")
    db.commit()
    db.close()


def response(payload):
    facts,coverage=[],[]
    for i,s in enumerate(payload['owned']):
        if s['text']:
            fid=f'f{i}'
            facts.append({'id':fid,'kind':'promise','claim':'An explicit statement.', 'basis':'explicit',
                'citations':[{'turn_id':s['turn_id'],'start':s['start'],'end':s['start']+min(12,len(s['text'])),
                              'quote':s['text'][:12]}]})
            coverage.append({'span_id':s['span_id'],'status':'extracted','fact_ids':[fid]})
        else:
            coverage.append({'span_id':s['span_id'],'status':'omitted','fact_ids':[],'reason':'empty message'})
    if payload.get('extraction_schema')=='quote-v2':
        for fact, s in zip(facts,[s for s in payload['owned'] if s['text']]):
            fact['citations']=[{'span_id':s['span_id'],'quote':s['text']}]
        return {'facts':facts,'omissions':[{'span_id':c['span_id'],'reason':c['reason']} for c in coverage if c['status']=='omitted']}
    return {'source_revision':payload['source_revision'],'facts':facts,'coverage':coverage,
            'narratives':[{'theme':'One experience','rationale':'same continuous scene','fact_ids':[f['id'] for f in facts]}] if facts else []}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'source.db'
        archive(self.path)

    def load(self):
        return load_batch(self.path,'batch','scope')

    def mutate(self,sql):
        db=sqlite3.connect(self.path)
        db.execute(sql)
        db.commit()
        db.close()

    def test_readonly_source(self):
        before=self.path.read_bytes()
        batch=self.load()
        self.assertEqual(len(batch.turns),2)
        self.assertEqual(before,self.path.read_bytes())

    def test_scope_boundary(self):
        with self.assertRaises(SourceError):
            load_batch(self.path,'batch','another')

    def test_missing_turn_blocks(self):
        self.mutate('DELETE FROM source_turns WHERE id=2')
        with self.assertRaises(SourceError): self.load()

    def test_content_hash_blocks(self):
        self.mutate("UPDATE source_turns SET content='wrong' WHERE id=1")
        with self.assertRaises(SourceError): self.load()

    def test_time_change_changes_revision(self):
        original=self.load().revision
        self.mutate('UPDATE source_turns SET event_ts=event_ts+1 WHERE id=1')
        self.assertNotEqual(original,self.load().revision)

    def test_speaker_change_changes_revision(self):
        original=self.load().revision
        self.mutate("UPDATE source_turns SET role='assistant' WHERE id=1")
        self.assertNotEqual(original,self.load().revision)

    def test_pair_kept_when_fits(self):
        self.assertEqual(len(split_batch(self.load(),128)),1)

    def test_long_turn_no_tail_loss(self):
        path=Path(self.tmp.name)/'long.db'
        archive(path,['x'*731])
        batch=load_batch(path,'batch','scope')
        shards=split_batch(batch,128)
        spans=[s for shard in shards for s in shard.owned]
        self.assertEqual(''.join(batch.turns[0].content[s.start:s.end] for s in spans),'x'*731)
        self.assertTrue(all(spans[i].end==spans[i+1].start for i in range(len(spans)-1)))
        with self.assertRaises(SourceError): split_batch(batch,128,max_shards=2)

    def test_single_exchange_not_discarded(self):
        batch=self.load()
        shard=split_batch(batch)[0]
        result=validate_extraction(json.dumps(response(shard_payload(batch,shard))),batch,shard)
        self.assertEqual(len(result['facts']),2)

    def test_exact_quotes_and_source_times(self):
        batch=self.load()
        shard=split_batch(batch)[0]
        obj=response(shard_payload(batch,shard))
        obj['facts'][0]['event_ts']=999
        valid=validate_extraction(json.dumps(obj),batch,shard)
        self.assertEqual(valid['facts'][0]['citations'][0]['event_ts'],batch.turns[0].event_ts)
        obj['facts'][0]['citations'][0]['quote']='invented'
        with self.assertRaises(SourceError): validate_extraction(json.dumps(obj),batch,shard)

    def test_coverage_required(self):
        batch=self.load(); shard=split_batch(batch)[0]
        obj=response(shard_payload(batch,shard)); obj['coverage'].pop()
        with self.assertRaises(SourceError): validate_extraction(json.dumps(obj),batch,shard)

    def test_context_cannot_supply_owned_evidence(self):
        path=Path(self.tmp.name)/'long.db'; archive(path,['a'*180,'b'*180])
        batch=load_batch(path,'batch','scope'); shard=split_batch(batch,128)[0]
        obj=response(shard_payload(batch,shard)); ctx=shard.context[0]
        obj['facts'][0]['citations'][0]={'turn_id':ctx.turn_id,'start':ctx.start,'end':ctx.start+1,'quote':'a'}
        with self.assertRaises(SourceError): validate_extraction(json.dumps(obj),batch,shard)

    def test_inference_not_fact(self):
        batch=self.load(); shard=split_batch(batch)[0]
        obj=response(shard_payload(batch,shard)); obj['facts'][0]['basis']='inference'
        result=validate_extraction(json.dumps(obj),batch,shard)
        self.assertEqual(result['facts'][0]['certainty'],'hypothesis')
        self.assertFalse(result['facts'][0]['semantic_verified'])

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(SourceError): parse_object('{"facts":[],"facts":[]}')

    def test_orphan_fact_rejected(self):
        batch=self.load(); shard=split_batch(batch)[0]
        obj=response(shard_payload(batch,shard))
        obj['facts'].append({**obj['facts'][0],'id':'orphan'})
        with self.assertRaises(SourceError): validate_extraction(json.dumps(obj),batch,shard)

    def test_nontext_rationale_rejected(self):
        with self.assertRaises(SourceError):
            validate_plan([{'theme':'x','rationale':True,'fact_ids':['a']}],[{'id':'a'}])

    def test_fenced_json_supported(self):
        self.assertEqual(parse_object('```json\n{"a":1}\n```'),{'a':1})

    def test_plan_ceiling_and_unknown_ids(self):
        with self.assertRaises(SourceError): validate_plan([{'theme':'x','rationale':'y','fact_ids':['missing']}],[],3)
        with self.assertRaises(SourceError): validate_plan([{}]*4,[],3)


class FakeProvider:
    def __init__(self):
        self.calls=[]
        self.reject_part=None
    async def text_chat(self,*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
        payload=json.loads(prompt)
        self.calls.append(payload)
        if self.reject_part is not None and len(self.calls)==self.reject_part:
            return SimpleNamespace(completion_text='invalid')
        if 'owned' in payload:
            result=response(payload)
        else:
            result={'narratives':[{'theme':'Combined experience','rationale':'continuous event despite splitting',
                                  'fact_ids':[f['id'] for f in payload['facts']]}]}
        return SimpleNamespace(completion_text=json.dumps(result))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'source.db'; archive(self.path)
        self.store=ProductionStore(Path(self.tmp.name)/'runtime.db')
        self.scheduler=Scheduler(self.store); self.addAsyncCleanup(self.scheduler.close)
        self.pipeline=EvidencePipeline(self.store,self.scheduler,self.path)
        self.provider=FakeProvider()
        self.route=Route('test-model','astr','fixture','fixture',AstrAdapter(self.provider))

    async def test_single_batch_one_extraction_plan_call_and_reuse(self):
        first=await self.pipeline.extract('batch','scope',self.route)
        second=await self.pipeline.extract('batch','scope',self.route)
        self.assertEqual(first,second)
        self.assertEqual(len(self.provider.calls),2)
        self.assertEqual(first['status'],'planned_not_published')
        self.assertEqual(first['writing_person'],'first_person')
        self.assertEqual(self.store.progress(first['job_id']),[{'part':0,'status':'verified'}])

    async def test_source_dates_not_generation_date(self):
        plan=await self.pipeline.extract('batch','scope',self.route)
        self.assertEqual(plan['narratives'][0]['event_start'],1790600000)
        self.assertEqual(plan['narratives'][0]['event_end'],1790603600)

    async def test_budget_preflight_before_call(self):
        with self.assertRaises(SourceError):
            await self.pipeline.extract('batch','scope',self.route,job_cap=1)
        self.assertFalse(self.provider.calls)

    async def test_owner_cancel_drains_model(self):
        started=asyncio.Event()
        stopped=asyncio.Event()
        class Slow:
            async def text_chat(self,*,prompt,contexts,system_prompt,request_timeout=None,request_max_retries=None):
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    stopped.set()
        route=Route('slow','astr','fixture','fixture',AstrAdapter(Slow()))
        task=asyncio.create_task(self.pipeline.extract('batch','scope',route))
        await asyncio.wait_for(started.wait(),5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(stopped.is_set())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'cancelled')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM narrative_previews').fetchone()[0],0)

    async def test_invalid_result_never_creates_plan(self):
        self.provider.reject_part=1
        with self.assertRaises(RuntimeError):
            await self.pipeline.extract('batch','scope',self.route)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM narrative_previews').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status FROM extraction_parts').fetchone()[0],'awaiting_recovery')

    async def test_shards_do_not_become_diaries(self):
        path=Path(self.tmp.name)/'long.db'; archive(path,['a'*150,'b'*150])
        self.pipeline.archive_path=path
        plan=await self.pipeline.extract('batch','scope',self.route,char_limit=128,job_cap=8)
        self.assertEqual(len(plan['narratives']),1)
        self.assertEqual(len(self.provider.calls),5) # four extraction shards + one compact plan
        self.assertNotIn('owned',self.provider.calls[-1])

    async def test_failed_shard_preserves_earlier_checkpoint(self):
        path=Path(self.tmp.name)/'long.db'; archive(path,['a'*150,'b'*150]); self.pipeline.archive_path=path
        self.provider.reject_part=2
        with self.assertRaises(RuntimeError):
            await self.pipeline.extract('batch','scope',self.route,char_limit=128,job_cap=8)
        with self.store.connect() as db:
            states=[r[0] for r in db.execute('SELECT status FROM extraction_parts ORDER BY part')]
        self.assertEqual(states,['verified','awaiting_recovery','pending','pending'])

    async def test_upgrade_preserves_old_and_is_idempotent(self):
        before=self.path.read_bytes()
        summary=stage_upgrades(self.path,self.store)
        self.assertEqual(summary,stage_upgrades(self.path,self.store))
        plan=await self.pipeline.extract('batch','scope',self.route)
        proposal=bind_upgrade_plan(self.store,'old',plan)
        self.assertTrue(proposal['new_evidence_ids'])
        self.assertFalse(proposal['active_changed'])
        self.assertEqual(proposal['old']['memo_name'],'memos/old')
        self.assertEqual(before,self.path.read_bytes())

    async def test_diary_only_never_fabricates_original(self):
        db=sqlite3.connect(self.path)
        db.execute("UPDATE episodes SET source_batch_id='' "); db.commit(); db.close()
        self.assertEqual(stage_upgrades(self.path,self.store),{'diary_only':1})
        with self.assertRaises(ConflictError): bind_upgrade_plan(self.store,'old',{'source_revision':None})

    async def test_modified_plan_cannot_bind(self):
        stage_upgrades(self.path,self.store)
        plan=await self.pipeline.extract('batch','scope',self.route)
        plan['facts'][0]['claim']='tampered'
        with self.assertRaises(ConflictError): bind_upgrade_plan(self.store,'old',plan)

    async def test_changed_original_refuses_restage(self):
        stage_upgrades(self.path,self.store)
        db=sqlite3.connect(self.path)
        db.execute('UPDATE source_turns SET event_ts=event_ts+1 WHERE id=1'); db.commit(); db.close()
        with self.assertRaises(ConflictError): stage_upgrades(self.path,self.store)

    async def test_unknown_scene_range_not_whole_batch(self):
        db=sqlite3.connect(self.path)
        db.execute('UPDATE episodes SET scene_start_turn=-1,scene_end_turn=-1'); db.commit(); db.close()
        self.assertEqual(stage_upgrades(self.path,self.store),{'source_partial':1})
        with self.store.connect() as db:
            proposal=json.loads(db.execute('SELECT proposal FROM upgrade_previews').fetchone()[0])
        self.assertEqual(proposal['verified_ranges'],[])


if __name__=='__main__': unittest.main()
