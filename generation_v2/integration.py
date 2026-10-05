"""Production handoff and compatibility projections for Astr's existing consumers."""
import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3
from pathlib import Path
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .adapters import AstrAdapter, DirectAdapter
from .coordinator import Coordinator
from .literary import DiaryStore, WritingPolicy
from .preflight import readonly
from .projection import EvidenceIndex, safe_claim, projection_basis
from .publishing import MemosPublisherAdapter, Publisher
from .scheduler import Route, Scheduler
from .sources import load_batch
from .store import ConflictError, encoded
from .workbench import ledger_path
from .bindings import item_fact_ids


async def service_for(plugin):
    service=getattr(plugin,'_generation_v2_service',None)
    if service is not None: return service
    lock=getattr(plugin,'_generation_v2_init_lock',None)
    if lock is None:
        lock=asyncio.Lock(); plugin._generation_v2_init_lock=lock
    async with lock:
        service=getattr(plugin,'_generation_v2_service',None)
        if service is None:
            store=await asyncio.to_thread(DiaryStore,ledger_path(plugin))
            archive=getattr(getattr(plugin,'_episodes',None),'db_path',None) or Path(plugin.runtime_state_dir)/'episodic_memory.db'
            service=Coordinator(store,Scheduler(store),archive)
            plugin._generation_v2_service=service
        return service


def route_for(plugin,scope,provider_id=None,task='memory_generation'):
    provider=plugin._resolve_chat_provider(provider_id if provider_id is not None else getattr(plugin,'compress_provider_id',''),umo=scope)
    if provider is None: raise ValueError('compression model unavailable')
    from ..external_models import ExternalModelPoolProvider,ExternalSingleProvider
    if isinstance(provider,ExternalModelPoolProvider):
        registry=plugin._external_models
        models=registry._ordered_models(provider.preferred_id)
        if not models: raise ValueError('external compression model unavailable')
        provider=ExternalSingleProvider(registry,str(models[0]['id']))
    identity=str(getattr(provider,'provider_id','') or getattr(provider.meta(),'id',''))
    direct=isinstance(provider,ExternalSingleProvider)
    def location(model):
        if isinstance(model,ExternalSingleProvider):
            url=str((model.registry.model(model.model_id) or {}).get('base_url',''))
        else:
            url=str(getattr(model,'provider_config',{}).get('api_base',''))
        parsed=urlsplit(url)
        host=parsed.hostname or 'unknown-provider-endpoint'
        return host+(':'+str(parsed.port) if parsed.port else ''),host
    endpoint,domain=location(provider)
    registry=getattr(plugin,'_external_models',None)
    policies = getattr(registry, 'task_call_policies', {})
    primary=Route(identity,'direct' if direct else 'astr',endpoint,domain,
                  DirectAdapter(provider, policies=policies) if direct else AstrAdapter(provider, policies=policies))
    backup=None
    registry=getattr(plugin,'_external_models',None)
    if not direct and registry is not None:
        followup=registry.followup_provider(task)
        if followup is not None:
            endpoint,domain=location(followup)
            backup=Route(followup.provider_id,'direct',endpoint,domain,DirectAdapter(followup, policies=policies))
    return primary,backup


def stage_routes_for(plugin,scope):
    fields={'episode_extract':'episode_extraction_provider_id','narrative_plan':'narrative_plan_provider_id',
            'diary_write':'diary_render_provider_id','diary_review':'diary_review_provider_id'}
    mappings=getattr(getattr(plugin,'_external_models',None),'astr_followup_models',{})
    result={}
    for task,field in fields.items():
        configured=getattr(plugin,field,'')
        if configured or task in mappings:
            result[task]=route_for(plugin,scope,configured or None,task)
    return result or None


def ensure_handoffs(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS auto_handoffs (
            batch_id TEXT PRIMARY KEY,scope TEXT NOT NULL,seq INTEGER,source_kind TEXT NOT NULL,
            job_id TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'owned',
            next_retry REAL NOT NULL DEFAULT 0,error_kind TEXT NOT NULL DEFAULT '',created REAL NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS auto_handoff_pending ON auto_handoffs(scope,status,created)')


def pending(plugin,scope):
    path=ledger_path(plugin)
    if path is None or not path.is_file(): return None
    db=readonly(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='auto_handoffs'").fetchone(): return None
        row=db.execute("SELECT * FROM auto_handoffs WHERE scope=? AND status!='done' ORDER BY created LIMIT 1",(scope,)).fetchone()
        return dict(row) if row else None
    finally: db.close()


class ConsumerIndex:
    """Project all grounded facts, including those deliberately left out of prose."""
    def __init__(self,plugin,canonical): self.plugin,self.canonical=plugin,canonical

    async def apply(self,event_id,manifest):
        migration=getattr(self.plugin,'_episode_migration_task',None)
        if migration is not None and not migration.done():
            await asyncio.shield(migration)
        await self.canonical.apply(event_id,manifest)
        plugin=self.plugin
        from ..main import _parse_memo_content
        plan=await asyncio.to_thread(self.canonical.store.plan,manifest['job_id'])
        batch=await asyncio.to_thread(load_batch,plugin._episodes.db_path,manifest['batch_id'],manifest['scope'])
        turns={t.id:t for t in batch.turns}
        selected={fid for item in manifest['items'] for fid in item_fact_ids(item,plan)}
        for item in manifest['items']:
            name=item['name']
            if not plugin._vec.get_memo_meta(name):
                if not await plugin._index_memo_record({'name':name,'content':item['content'],
                        'createTime':item['event_time'],'updateTime':item['event_time']}):
                    raise ConflictError('diary passage projection not ready')
            ids=set(item_fact_ids(item,plan))
            facts=[f for f in plan['facts'] if f['id'] in ids or (item['ordinal']==0 and f['id'] not in selected)]
            evidence=[]
            for f in facts:
                for c in f['citations']:
                    t=turns[c['turn_id']]
                    evidence.append({'kind':f['kind'],'actor':t.role,'detail':f"[{projection_basis(f)}] {safe_claim(f)}",
                        'quote':c['quote'],'turn_indexes':[t.index],'confidence':1 if projection_basis(f)=='explicit' else .7,
                        'grounded':True,'tier':'must' if f['kind'] in ('promise','conflict') else 'supporting'})
            core='\n'.join(f"[{projection_basis(f)}] {safe_claim(f)}" for f in facts)
            body_hash=plugin._content_hash(_parse_memo_content(item['content'])[1])
            old=await asyncio.to_thread(plugin._episodes.get_episode,name)
            if (old and old.get('diary_render_version')=='6.1-evidence-v2' and old.get('card_text')==core
                    and old.get('evidence_quality')=='source_grounded'
                    and old.get('diary_content_hash')==body_hash):
                continue
            embedding=await plugin._embed(core or item['content'])
            indices=[index for ev in evidence for index in ev['turn_indexes']]
            episode={'scene_anchor':plan['narratives'][item['ordinal']]['theme'],
                'retrieval_key':core,'evidence':evidence,'memory_type':'plot_fact',
                'occurred_at':item['event_time'],'event_ts':datetime.fromisoformat(item['event_time'].replace('Z','+00:00')).timestamp(),
                'time_basis':item['time_basis'],'scene_start_turn':min(indices) if indices else -1,
                'scene_end_turn':max(indices) if indices else -1}
            await asyncio.to_thread(plugin._episodes.upsert_episode,memo_name=name,episode=episode,
                card_text=core,embedding=embedding,source_batch_id=manifest['batch_id'],source_kind='generation_v2',
                legacy=False,evidence_quality='source_grounded',diary_content_hash=body_hash,
                diary_render_version='6.1-evidence-v2')

    async def verify(self,event_id,manifest):
        if not await self.canonical.verify(event_id,manifest): return False
        from ..main import _parse_memo_content
        plan=await asyncio.to_thread(self.canonical.store.plan,manifest['job_id'])
        selected={fid for item in manifest['items'] for fid in item_fact_ids(item,plan)}
        for item in manifest['items']:
            episode=await asyncio.to_thread(self.plugin._episodes.get_episode,item['name'])
            if not episode or episode.get('source_batch_id')!=manifest['batch_id'] or not self.plugin._vec.get_memo_meta(item['name']):
                return False
            ids=set(item_fact_ids(item,plan))
            core='\n'.join(f"[{projection_basis(f)}] {safe_claim(f)}" for f in plan['facts']
                           if f['id'] in ids or (item['ordinal']==0 and f['id'] not in selected))
            if (episode.get('evidence_quality')!='source_grounded'
                    or episode.get('diary_render_version')!='6.1-evidence-v2'
                    or episode.get('card_text')!=core
                    or episode.get('diary_content_hash')!=self.plugin._content_hash(_parse_memo_content(item['content'])[1])):
                return False
        return True


class StateConsumer:
    def __init__(self,plugin): self.plugin=plugin
    async def apply(self,event_id,manifest):
        plugin=self.plugin
        if not getattr(plugin,'semantic_state_enable',False): return
        episodes=await asyncio.to_thread(plugin._episodes.episodes_for_batch,manifest['batch_id'])
        if not episodes: raise ConflictError('published evidence view missing')
        # Existing queue is idempotent by batch; its adaptive update policy stays intact.
        plugin._schedule_semantic_state_update(episodes,source_batch_id=manifest['batch_id'],reason='generation_v2')


def new_production_options(plugin,store,batch_id,scope):
    """Never retrofit new stages onto an already frozen production receipt."""
    import hashlib
    scope_hash=hashlib.sha256(scope.encode()).hexdigest()
    with store.connect() as db:
        row=db.execute('SELECT contract FROM source_receipts WHERE batch_id=? AND scope_hash=? ORDER BY created LIMIT 1',
                       (batch_id,scope_hash)).fetchone()
    if row:
        contract=json.loads(row[0])
        return {key:value for key,value in (
            ('semantic_audit',bool(contract.get('semantic_audit'))),
            ('call_profile',contract.get('call_profile','legacy')))
            if value not in (False,'legacy')}
    return {'semantic_audit':bool(getattr(plugin,'generation_v2_claim_audit',True)),
            'call_profile':getattr(plugin,'generation_v2_call_profile','balanced-v1')}


async def produce(plugin,scope,msgs,seq,source_kind,diary_cap):
    service=await service_for(plugin)
    store=service.store
    await asyncio.to_thread(ensure_handoffs,store)
    handoff=await asyncio.to_thread(pending,plugin,scope)
    if handoff is None:
        batch=await asyncio.to_thread(plugin._episodes.archive_batch,scope,msgs,source_kind)
        info=await asyncio.to_thread(plugin._episodes.batch_info,batch)
        if info and info.get('status')=='committed':
            if seq is not None:
                await plugin._vec.buffer_drop(scope,seq)
                plugin._buffer[scope]=await plugin._vec.buffer_take(scope,last_seq=seq)
            return 0
        def claim():
            with store.connect() as db:
                db.execute('INSERT OR IGNORE INTO auto_handoffs(batch_id,scope,seq,source_kind,created) VALUES(?,?,?,?,?)',
                           (batch,scope,seq,source_kind,time.time()))
        await asyncio.to_thread(claim)
        handoff=await asyncio.to_thread(pending,plugin,scope)
    if handoff is None or handoff['next_retry']>time.time(): return 0
    if handoff['status'] in ('paused','manual_review','awaiting_recovery'):
        return 0
    batch=handoff['batch_id']
    def update(**fields):
        allowed={'job_id','status','next_retry','error_kind'}
        if not set(fields)<=allowed: raise ValueError('invalid handoff fields')
        with store.connect() as db:
            db.execute('UPDATE auto_handoffs SET '+','.join(k+'=?' for k in fields)+' WHERE batch_id=?',(*fields.values(),batch))
    job=handoff['job_id']
    try:
        if not getattr(plugin,'generation_v2_memos_capabilities_confirmed',False):
            raise ConflictError('confirm custom Memos IDs and event-date support before automatic publication')
        with store.connect() as db:
            publishing=job and db.execute('SELECT 1 FROM publish_batches WHERE job_id=?',(job,)).fetchone()
        # Once a publication manifest exists, recovery is delivery only, even after restart.
        if not publishing:
            if job:
                from .recovery import saved_workflow, frozen_routes
                with store.connect() as db:
                    receipt=db.execute('SELECT contract FROM source_receipts WHERE job_id=?',(job,)).fetchone()
                if not receipt or json.loads(receipt[0]).get('extraction_version')!='quote-v2':
                    await asyncio.to_thread(update,status='manual_review',next_retry=0,
                                            error_kind='extraction_upgrade_requires_reextract')
                    plugin._log_event('compress','RC4 requires explicit source re-extraction',
                                      {'job_id':job,'buffer_preserved':True,'model_calls':0})
                    return 0
                workflow=await asyncio.to_thread(saved_workflow,service,job)
                primary,backup,stages=frozen_routes(plugin,workflow)
                job=await service.start(batch,scope,primary,backup,stage_routes=stages,
                    policy=WritingPolicy(**workflow['policy']),**workflow['options'])
            else:
                primary,backup=route_for(plugin,scope)
                job=await service.start(batch,scope,primary,backup,stage_routes=stage_routes_for(plugin,scope),policy=WritingPolicy(
                    character=plugin.character_name,timezone=plugin.rp_time_timezone),
                    max_diaries=max(1,min(12,diary_cap)),job_cap=int(getattr(plugin,'generation_v2_job_cap',12)),
                    **new_production_options(plugin,store,batch,scope))
            await asyncio.to_thread(update,job_id=job)
            status=await service.wait(job)
            if status['stage']!='draft_ready': raise ConflictError('production draft not qualified; source retained')
        publisher=Publisher(store,MemosPublisherAdapter(plugin._memos,capabilities_verified=True),plugin._episodes.db_path)
        await asyncio.to_thread(publisher.prepare,job,scope)
        await asyncio.to_thread(service._stage,job,'publishing')
        await publisher.publish(job)
        await asyncio.to_thread(service._stage,job,'indexing')
        await publisher.deliver_index(job,ConsumerIndex(plugin,service.index))
        # Queue current-state work before acknowledging the buffer, not after a new diary generation.
        await asyncio.to_thread(service._stage,job,'state_pending')
        await publisher.deliver_state(job,StateConsumer(plugin))
        await asyncio.to_thread(plugin._episodes.mark_batch,batch,'committed')
        if handoff['seq'] is not None:
            await plugin._vec.buffer_drop(scope,handoff['seq'])
            plugin._buffer[scope]=await plugin._vec.buffer_take(scope,last_seq=handoff['seq'])
        await asyncio.to_thread(update,status='done',next_retry=0,error_kind='')
        await asyncio.to_thread(service._stage,job,'published')
        count=len(store.drafts(job))
        plugin._compress_count=getattr(plugin,'_compress_count',0)+1
        plugin._last_compress_ts=time.time()
        plugin._log_event('compress',f'6.1 production committed: {count} diaries',{'batch':batch,'job_id':job})
        return count
    except asyncio.CancelledError: raise
    except Exception as exc:
        await asyncio.to_thread(update,status='awaiting_recovery',next_retry=time.time()+900,error_kind=type(exc).__name__)
        if job:
            await asyncio.to_thread(service._stage,job,'awaiting_recovery',type(exc).__name__)
        plugin._log_event('compress','6.1 production retained for recovery',{'batch':batch,'error_kind':type(exc).__name__,'buffer_preserved':True})
        return 0


async def resume_delivery(plugin,request):
    """Explicit delivery recovery never starts a new drafting workflow."""
    if getattr(plugin,'_terminating',False) or request.get('confirm_delivery') is not True:
        raise ValueError('explicit delivery confirmation required while running')
    job=request.get('job_id')
    if not isinstance(job,str) or not job: raise ValueError('job required')
    if not getattr(plugin,'generation_v2_memos_capabilities_confirmed',False):
        raise ConflictError('Memos capabilities must be confirmed')
    service=await service_for(plugin)
    if service.closed: raise ConflictError('coordinator closed')
    await asyncio.to_thread(ensure_handoffs,service.store)
    def lookup():
        with service.store.connect() as db:
            row=db.execute('''SELECT h.* FROM auto_handoffs h JOIN publish_batches p ON p.job_id=h.job_id
                WHERE h.job_id=?''',(job,)).fetchone()
            return dict(row) if row else None
    handoff=await asyncio.to_thread(lookup)
    if service.closed or getattr(plugin,'_terminating',False): raise ConflictError('plugin stopping')
    if not handoff: raise ConflictError('delivery manifest and automatic handoff required')
    if handoff['status']=='done': return {'job_id':job,'status':'already_done'}
    active=service.delivery_tasks.get(job)
    if active and not active.done(): return {'job_id':job,'status':'already_running'}
    if service.tasks.get(job) and not service.tasks[job].done():
        raise ConflictError('drafting still active')

    async def deliver():
        lock=plugin._compress_locks.setdefault(handoff['scope'],asyncio.Lock())
        async with lock:
            first=await asyncio.to_thread(pending,plugin,handoff['scope'])
            if first is None: return
            if first['batch_id']!=handoff['batch_id']:
                raise ConflictError('an earlier batch owns this session')
            def reset_cooldown():
                with service.store.connect() as db:
                    db.execute("UPDATE auto_handoffs SET next_retry=0,status='owned' WHERE batch_id=?",(handoff['batch_id'],))
            await asyncio.to_thread(reset_cooldown)
            await produce(plugin,handoff['scope'],[],handoff['seq'],handoff['source_kind'],3)
    task=asyncio.create_task(deliver())
    service.delivery_tasks[job]=task
    def release(done):
        if service.delivery_tasks.get(job) is done: service.delivery_tasks.pop(job,None)
        if not done.cancelled() and done.exception() is not None:
            service._stage(job,'awaiting_recovery',type(done.exception()).__name__)
    task.add_done_callback(release)
    return {'job_id':job,'status':'delivery_queued','generation_calls':0,'embedding_may_call':True}


def _storage_failure(plugin,operation,exc):
    log=getattr(plugin,'_log_event',None)
    if callable(log):
        log('recall','6.1 evidence storage unavailable; unverified candidates withheld',
            {'operation':operation,'error_kind':type(exc).__name__})


def visible_hits(plugin,hits,scope):
    try:
        return _visible_hits(plugin,hits,scope)
    except (sqlite3.Error,OSError) as exc:
        _storage_failure(plugin,'visibility',exc)
        return []


def _visible_hits(plugin,hits,scope):
    path=ledger_path(plugin)
    if path is None or not path.is_file() or not hits: return hits
    db=readonly(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='publish_items'").fetchone(): return hits
        names=list({h.get('memo_name') for h in hits if h.get('memo_name')})
        if not names: return hits
        rows=db.execute('''SELECT i.name,g.active,g.scope FROM publish_items i LEFT JOIN
            published_generations g ON g.job_id=i.job_id WHERE i.name IN ('''+','.join('?' for _ in names)+')',names)
        owned={r['name']:bool(r['active'] and r['scope']==scope) for r in rows}
        return [h for h in hits if owned.get(h.get('memo_name'),True)]
    finally: db.close()


def search_evidence(plugin,scope,query):
    try:
        return _search_evidence(plugin,scope,query)
    except (sqlite3.Error,OSError) as exc:
        _storage_failure(plugin,'search',exc)
        return []


def _search_evidence(plugin,scope,query):
    path=ledger_path(plugin)
    if not scope or path is None or not path.is_file(): return []
    db=readonly(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='v2_evidence_fts'").fetchone(): return []
    finally: db.close()
    # Read-only facade: do not initialize/migrate tables on the recall hot path.
    class Reader:
        @contextmanager
        def connect(self):
            connection=readonly(path)
            try: yield connection
            finally: connection.close()
    index=object.__new__(EvidenceIndex)
    index.store=Reader()
    hits=index.search(scope,query)
    from .legacy_links import search as legacy_search
    archive=getattr(getattr(plugin,'_episodes',None),'db_path',None)
    by_name={h['memo_name']:h for h in hits}
    for hit in legacy_search(path,archive,scope,query):
        if hit['memo_name'] in by_name:
            current=by_name[hit['memo_name']]
            seen={f['evidence_id'] for f in current['_v2_evidence']}
            current['_v2_evidence'].extend(f for f in hit['_v2_evidence'] if f['evidence_id'] not in seen)
        else:
            by_name[hit['memo_name']]=hit
    return sorted(by_name.values(),key=lambda h:h['score'],reverse=True)[:30]


def attach_evidence(hit,include_quotes=True,limit=3):
    limit=max(1,min(8,int(limit)))
    facts=hit.get('_v2_evidence',[])[:limit]
    def recorded(fact):
        values=[]
        for c in fact['citations']:
            if not c.get('event_ts'): continue
            try: zone=ZoneInfo(c.get('timezone') or 'Asia/Shanghai')
            except (ValueError,KeyError): zone=ZoneInfo('UTC')
            values.append(datetime.fromtimestamp(c['event_ts'],zone).isoformat(timespec='minutes'))
        if not values: return ' [原文时间未知]'
        times=sorted(set(values))
        bounds=times if len(times)<2 else [times[0],times[-1]]
        return ' [原文记录时间: '+' ~ '.join(bounds)+']'
    hit['_fusion_event_core']=[('推测，非已确认事实: ' if f.get('basis')=='inference' else '事件证据: ')+f['claim']+recorded(f) for f in facts]
    hit['_fusion_source_evidence']=[]
    if include_quotes:
        for fact in facts:
            for c in fact['citations'][:1]:
                quote=c['quote']
                excerpt=quote[:420]+('… [节选]' if len(quote)>420 else '')
                hit['_fusion_source_evidence'].append(f"{c.get('role','')} [原文#{c['turn_id']}]: {excerpt}")
    hit['_fusion_evidence_quality']='source_grounded'


def sole_published_scope(plugin):
    """Admin lab may infer only an unambiguous single scope, never combine chats."""
    try:
        return _sole_published_scope(plugin)
    except (sqlite3.Error,OSError) as exc:
        _storage_failure(plugin,'scope',exc)
        return ''


def _sole_published_scope(plugin):
    path=ledger_path(plugin)
    if path is None or not path.is_file(): return ''
    db=readonly(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='published_generations'").fetchone(): return ''
        rows=db.execute('SELECT DISTINCT scope FROM published_generations WHERE active=1 LIMIT 2').fetchall()
        return rows[0][0] if len(rows)==1 else ''
    finally: db.close()
