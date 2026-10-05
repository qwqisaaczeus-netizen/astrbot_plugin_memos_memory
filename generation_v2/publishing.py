"""Durable create/reconcile/commit protocol. No blind retries or remote deletes."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid
from zoneinfo import ZoneInfo

from .sources import SourceError, load_batch
from .store import ConflictError, encoded
from .bindings import final_groups, verify_plan_evidence


class UnknownWrite(RuntimeError):
    pass


def utc_time(value):
    if value is None or value<=0:
        raise SourceError('event date unknown; calendar projection requires explicit resolution')
    return datetime.fromtimestamp(value,timezone.utc).isoformat().replace('+00:00','Z')


class MemosPublisherAdapter:
    """Memos v0.29.1 contract; explicit capability opt-in, verified by readback.

    Caller must confirm custom memoId and createTime support for the target.
    Unsupported/unknown services are never probed by creating a throwaway memo.
    """
    def __init__(self,client,*,capabilities_verified=False):
        self.client=client
        self.capabilities_verified=capabilities_verified

    async def get(self,name):
        session=await self.client._ensure_session()
        async with session.get(self.client._url('/'+name)) as response:
            if response.status==404: return None
            if response.status!=200:
                raise UnknownWrite('Memos readback unavailable: '+str(response.status))
            return await response.json()

    async def create(self,name,content,event_time):
        if not self.capabilities_verified: raise ConflictError('Memos capabilities not confirmed')
        session=await self.client._ensure_session()
        async with session.post(self.client._url('/memos'),params={'memoId':name.split('/')[1]},
                json={'content':content,'visibility':'PRIVATE','createTime':event_time}) as response:
            if response.status not in (200,201):
                # Even an error response must be reconciled before another create.
                raise UnknownWrite('Memos create response requires reconciliation: '+str(response.status))
            return await response.json()


class Publisher:
    def __init__(self,store,remote,archive):
        self.store,self.remote,self.archive=store,remote,archive

    def prepare(self,job,scope,*,replaces=()):
        plan=self.store.plan(job)
        batch=load_batch(self.archive,plan['batch_id'],scope)
        if plan['source_revision']!=batch.revision: raise SourceError('source changed before publication')
        drafts=self.store.drafts(job)
        if not drafts or len(drafts)!=len(plan['narratives']) or any(r['status']!='qualified' for r in drafts):
            raise ConflictError('entire batch must qualify before publication')
        verify_plan_evidence(plan,batch)
        with self.store.connect() as db:
            policy=json.loads(db.execute('SELECT contract FROM writing_contracts WHERE job_id=?',(job,)).fetchone()[0])
            if policy['plan_hash']!=hashlib.sha256(encoded(plan).encode()).hexdigest():
                raise ConflictError('narrative changed after drafting')
            zone=ZoneInfo(policy['timezone'])
            use_binding=(policy['writer'] in ('literary-v3','literary-v4')
                         or any(json.loads(row['report']).get('operator_reviewed') for row in drafts))
            bound=final_groups(plan,[json.loads(row['draft']) for row in drafts]) if use_binding else plan['narratives']
            items=[]
            for row in drafts:
                draft=json.loads(row['draft']); group=bound[row['ordinal']]
                start,end=group['event_start'],group['event_end']
                event_time=utc_time(start)
                day=datetime.fromtimestamp(start,zone).strftime('%Y-%m-%d')
                last=datetime.fromtimestamp(end,zone).strftime('%Y-%m-%d') if end else day
                date=day if day==last else day+' ~ '+last
                name='memos/'+hashlib.sha256((job+':'+str(row['ordinal'])).encode()).hexdigest()[:22]
                content=f'# {date} · {draft["title"]}\n\n{draft["body"]}'
                item={'ordinal':row['ordinal'],'name':name,'content':content,'event_time':event_time,
                      'event_end':end,'time_basis':group['time_basis'],'revision':row['revision']}
                if use_binding:
                    item.update({k:group[k] for k in ('fact_ids','prose_fact_ids','evidence_only_fact_ids','rebound_fact_ids')})
                    if 'reference_fact_ids' in group:item['reference_fact_ids']=group['reference_fact_ids']
                items.append(item)
            manifest={'job_id':job,'scope':scope,'batch_id':batch.batch_id,'source_revision':batch.revision,
                      'items':items,'facts':plan['facts'],'replaces':sorted(set(replaces))}
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT manifest FROM publish_batches WHERE job_id=?',(job,)).fetchone()
            if previous and previous[0]!=encoded(manifest): raise ConflictError('immutable publication manifest changed')
            if job in manifest['replaces']:
                raise ConflictError('generation cannot replace itself')
            if previous:
                return manifest
            for old in manifest['replaces']:
                row=db.execute('SELECT scope,active FROM published_generations WHERE job_id=?',(old,)).fetchone()
                if not row or row['scope']!=scope or not row['active']:
                    raise ConflictError('replacement must refer to active same-scope V2 generation')
            db.execute('INSERT OR IGNORE INTO publish_batches VALUES(?,?,?,NULL,NULL,?)',
                       (job,encoded(manifest),'prepared',time.time()))
            for item in items:
                db.execute('INSERT OR IGNORE INTO publish_items VALUES(?,?,?,?,?,?,NULL)',
                           (job,item['ordinal'],item['name'],item['content'],item['event_time'],'prepared'))
        return manifest

    def _claim(self,job,owner):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM publish_batches WHERE job_id=?',(job,)).fetchone()
            if not row: raise KeyError(job)
            if row['owner']: raise ConflictError('publication is already owned; restart reconciliation required')
            db.execute('UPDATE publish_batches SET owner=?,started=?,updated=? WHERE job_id=?',
                       (owner,time.time(),time.time(),job))
            return json.loads(row['manifest'])

    def recover_after_restart(self,job):
        """Explicit startup-only recovery; never call while an old worker is alive."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE publish_items SET status='outcome_unknown' WHERE job_id=? AND status='sending'",(job,))
            db.execute('UPDATE publish_batches SET owner=NULL WHERE job_id=?',(job,))

    def _status(self,job,index,status,receipt=None):
        with self.store.connect() as db:
            db.execute('UPDATE publish_items SET status=?,receipt=? WHERE job_id=? AND ordinal=?',
                       (status,encoded(receipt) if receipt is not None else None,job,index))

    @staticmethod
    def matches(item,memo):
        if not memo or memo.get('name')!=item['name'] or memo.get('content')!=item['content']: return False
        try:
            actual=datetime.fromisoformat(memo['createTime'].replace('Z','+00:00'))
            expected=datetime.fromisoformat(item['event_time'].replace('Z','+00:00'))
            if actual.tzinfo is None or expected.tzinfo is None: return False
            # Memos persists Unix seconds; retain subsecond source precision locally.
            return (actual.astimezone(timezone.utc).replace(microsecond=0)
                    ==expected.astimezone(timezone.utc).replace(microsecond=0))
        except (KeyError,ValueError,TypeError): return False

    async def publish(self,job):
        owner=uuid.uuid4().hex
        manifest=await asyncio.to_thread(self._claim,job,owner)
        try:
            batch=await asyncio.to_thread(load_batch,self.archive,manifest['batch_id'],manifest['scope'])
            if batch.revision!=manifest['source_revision']: raise SourceError('publication source changed')
            if not self.remote.capabilities_verified: raise ConflictError('remote capability not confirmed')
            with self.store.connect() as db:
                rows=[dict(r) for r in db.execute('SELECT * FROM publish_items WHERE job_id=? ORDER BY ordinal',(job,))]
            for item in rows:
                # Always check known deterministic identity, including previously successful items.
                memo=await self.remote.get(item['name'])
                if memo is not None:
                    if not self.matches(item,memo):
                        await asyncio.to_thread(self._status,job,item['ordinal'],'conflict')
                        raise ConflictError('remote content/date changed; never overwrite user edits')
                    await asyncio.to_thread(self._status,job,item['ordinal'],'remote_verified',memo)
                    continue
                if item['status']!='prepared':
                    await asyncio.to_thread(self._status,job,item['ordinal'],'outcome_unknown')
                    raise UnknownWrite('missing after attempted write; explicit reconciliation needed, not recreated')
                await asyncio.to_thread(self._status,job,item['ordinal'],'sending')
                try:
                    result=await self.remote.create(item['name'],item['content'],item['event_time'])
                    if not self.matches(item,result): raise UnknownWrite('create response identity/content/date mismatch')
                    actual=await self.remote.get(item['name'])
                    if not self.matches(item,actual): raise UnknownWrite('readback mismatch')
                except BaseException:
                    await asyncio.to_thread(self._status,job,item['ordinal'],'outcome_unknown')
                    raise
                await asyncio.to_thread(self._status,job,item['ordinal'],'remote_verified',actual)
            current=await asyncio.to_thread(load_batch,self.archive,manifest['batch_id'],manifest['scope'])
            if current.revision!=manifest['source_revision']: raise SourceError('source changed during publication')
            await asyncio.to_thread(self._commit,job,manifest)
            return {'job_id':job,'status':'index_pending','items':len(rows)}
        finally:
            with self.store.connect() as db:
                db.execute('UPDATE publish_batches SET owner=NULL,updated=? WHERE job_id=? AND owner=?',
                           (time.time(),job,owner))

    def _commit(self,job,manifest):
        # Canonical evidence projection and downstream outbox share a single local transaction.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM publish_items WHERE job_id=? AND status!='remote_verified'",(job,)).fetchone():
                raise ConflictError('publication incomplete')
            db.execute('INSERT OR IGNORE INTO published_generations VALUES(?,?,0,?)',
                       (job,manifest['scope'],encoded(manifest)))
            for kind in ('episode_index','semantic_state'):
                db.execute('INSERT OR IGNORE INTO production_outbox VALUES(?,?,?,?,?)',
                           (job+':'+kind,job,kind,encoded(manifest),'pending'))
            db.execute("UPDATE publish_batches SET status='index_pending',updated=? WHERE job_id=? AND status!='published'",(time.time(),job))

    async def deliver_index(self,job,sink):
        """Sink must atomically project all facts and memo links using event_id as idempotency key."""
        with self.store.connect() as db:
            row=db.execute("SELECT * FROM production_outbox WHERE event_id=?",(job+':episode_index',)).fetchone()
        if not row: raise ConflictError('remote batch not committed')
        manifest=json.loads(row['payload'])
        if row['status']=='delivered' and await sink.verify(row['event_id'],manifest): return
        for item in manifest['items']:
            if not self.matches(item,await self.remote.get(item['name'])):
                raise ConflictError('remote changed before index activation')
        await sink.apply(row['event_id'],manifest)
        if not await sink.verify(row['event_id'],manifest):
            raise ConflictError('index did not verify complete evidence projection')
        if row['status']=='delivered': return
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT status FROM production_outbox WHERE event_id=?',(row['event_id'],)).fetchone()[0]=='delivered':
                return
            # A second replacement may have finished while remote calls were in flight.
            for old in manifest['replaces']:
                predecessor=db.execute('SELECT scope,active FROM published_generations WHERE job_id=?',(old,)).fetchone()
                if not predecessor or predecessor['scope']!=manifest['scope'] or not predecessor['active']:
                    raise ConflictError('replacement predecessor changed before activation')
            db.execute("UPDATE production_outbox SET status='delivered' WHERE event_id=?",(row['event_id'],))
            db.execute('UPDATE published_generations SET active=1 WHERE job_id=?',(job,))
            for old in manifest['replaces']:
                db.execute('UPDATE published_generations SET active=0 WHERE job_id=?',(old,))
            db.execute("UPDATE publish_batches SET status='published',updated=? WHERE job_id=?",(time.time(),job))

    def rollback_local(self,job):
        """Revert only local visibility; retain both remote drafts and all originals."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM published_generations WHERE job_id=?',(job,)).fetchone()
            if not row or not row['active']: raise ConflictError('generation not active; stale rollback refused')
            manifest=json.loads(row['manifest'])
            for other in db.execute('SELECT job_id,manifest FROM published_generations WHERE active=1 AND scope=? AND job_id!=?',(row['scope'],job)):
                if set(json.loads(other['manifest']).get('replaces',[])) & set(manifest['replaces']):
                    raise ConflictError('rollback would resurrect a predecessor replaced by another generation')
            db.execute('UPDATE published_generations SET active=0 WHERE job_id=?',(job,))
            for old in manifest['replaces']:
                db.execute('UPDATE published_generations SET active=1 WHERE job_id=? AND scope=?',(old,row['scope']))
            db.execute("UPDATE publish_batches SET status='rolled_back',updated=? WHERE job_id=?",(time.time(),job))
            db.execute('INSERT OR IGNORE INTO production_outbox VALUES(?,?,?,?,?)',
                       (job+':state_reconcile',job,'state_reconcile',encoded({'rolled_back':job,'restored':manifest['replaces']}),'pending'))

    async def deliver_state(self,job,sink):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM production_outbox WHERE event_id=?',(job+':semantic_state',)).fetchone()
            generation=db.execute('SELECT active FROM published_generations WHERE job_id=?',(job,)).fetchone()
        if not row or not generation or not generation[0]: raise ConflictError('index not active')
        if row['status']=='delivered': return
        await sink.apply(row['event_id'],json.loads(row['payload']))
        with self.store.connect() as db:
            db.execute("UPDATE production_outbox SET status='delivered' WHERE event_id=?",(row['event_id'],))
