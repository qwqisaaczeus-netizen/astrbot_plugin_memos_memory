"""Explicit, idempotent revisions; old budgets and outputs are never reset."""
import asyncio
import hashlib
import json
import time

from .integration import ensure_handoffs, route_for, service_for, stage_routes_for
from .literary import WritingPolicy
from .sources import load_batch
from .store import ConflictError, encoded


def ensure_controls(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS workflow_revisions (
            request_id TEXT PRIMARY KEY, parent_job TEXT NOT NULL, child_job TEXT NOT NULL,
            action TEXT NOT NULL, fingerprint TEXT NOT NULL, created REAL NOT NULL)''')


def saved_workflow(service, job):
    with service.store.connect() as db:
        row=db.execute('SELECT contract FROM workflow_runs WHERE job_id=?',(job,)).fetchone()
    if not row: raise ValueError('unknown workflow')
    return json.loads(row[0])


def frozen_routes(plugin, workflow):
    ids={workflow['primary'],workflow.get('backup')}
    for pair in workflow.get('stage_routes',{}).values(): ids.update(pair)
    ids.discard(None)
    registry={}
    candidates=[None]+[p.get('value',p.get('id')) for p in plugin._chat_provider_options()]
    candidates += [getattr(plugin,field,'') for field in ('compress_provider_id',
        'episode_extraction_provider_id','narrative_plan_provider_id','diary_render_provider_id','diary_review_provider_id')]
    pool=getattr(plugin,'_external_models',None)
    if pool:
        candidates += ['external:'+str(m['id']) for m in pool.payload().get('models',[])]
    for provider_id in candidates:
        for task in ('memory_generation','episode_extract','narrative_plan','diary_write','diary_review'):
            try:
                a,b=route_for(plugin,workflow['scope'],provider_id,task)
                for r in (a,b):
                    if r: registry[r.digest]=r
            except Exception:
                continue
    if not ids<=registry.keys():
        raise ConflictError('frozen model unavailable; create an explicit revision with current routes')
    primary=registry[workflow['primary']]
    backup=registry.get(workflow.get('backup'))
    stages={k:(registry[a],registry.get(b)) for k,(a,b) in workflow.get('stage_routes',{}).items()}
    return primary,backup,stages or None


async def control(plugin, request):
    service=await service_for(plugin)
    if service.closed or getattr(plugin,'_terminating',False): raise ConflictError('plugin stopping')
    job=request.get('job_id'); action=request.get('action')
    if not isinstance(job,str) or not job: raise ValueError('job required')
    if action not in ('pause','cancel','resume','retry','rewrite','replan','reextract','revalidate','revise_local'):
        raise ValueError('unknown action')
    workflow=await asyncio.to_thread(saved_workflow,service,job)
    lock=plugin._compress_locks.setdefault(workflow['scope'],asyncio.Lock())
    if lock.locked(): raise ConflictError('session compression active; wait before controlling this job')
    async with lock:
        await asyncio.to_thread(ensure_handoffs,service.store)
        await asyncio.to_thread(ensure_controls,service.store)
        if action=='revise_local':
            active=service.tasks.get(job)
            if active and not active.done():raise ConflictError('drafting still active')
            delivery=service.delivery_tasks.get(job)
            if delivery and not delivery.done():raise ConflictError('publication still active')
            from .local_revision import revise
            return await asyncio.to_thread(revise,service,job,workflow,request)
        with service.store.connect() as db:
            manifest=db.execute('SELECT 1 FROM publish_batches WHERE job_id=?',(job,)).fetchone()
        if manifest:
            raise ConflictError('publication already prepared; use delivery recovery, never redraft an uncertain remote write')
        if action in ('pause','cancel'):
            task=service.tasks.get(job)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            await service.scheduler.cancel_job(job)
            service._stage(job,'paused' if action=='pause' else 'cancelled')
            with service.store.connect() as db:
                db.execute('UPDATE auto_handoffs SET status=?,next_retry=0 WHERE job_id=?',('paused',job))
            return {'job_id':job,'status':service.status(job)['stage'],'source_retained':True,'model_calls':0}
        if action=='revalidate':
            if request.get('confirm_local_revalidation') is not True:raise ValueError('explicit local revalidation required')
            active=service.tasks.get(job)
            if active and not active.done():raise ConflictError('job still running')
            from .revalidation import revalidate
            return await asyncio.to_thread(revalidate,service,job,workflow)
        if request.get('confirm_model_calls') is not True:
            raise ValueError('explicit paid-call confirmation required')
        with service.store.connect() as db:
            receipt_contract=json.loads(db.execute('SELECT contract FROM source_receipts WHERE job_id=?',(job,)).fetchone()[0])
        if receipt_contract.get('extraction_version')!='quote-v2' and action!='reextract':
            raise ConflictError('RC4 extraction contract changed; explicitly reextract from retained source with current routes')
        active=service.tasks.get(job)
        if active and not active.done(): return {'job_id':job,'status':'already_running'}
        if action=='resume':
            a,b,stages=frozen_routes(plugin,workflow)
            with service.store.connect() as db:
                receipt=db.execute('SELECT deadline FROM source_receipts WHERE job_id=?',(job,)).fetchone()
                budget=db.execute('SELECT cap,used FROM jobs WHERE id=?',(job,)).fetchone()
                tasks=db.execute("SELECT id,spec,status FROM tasks WHERE json_extract(spec,'$.job_id')=?",(job,)).fetchall()
            from .revalidation import verified_parts
            _,_,_,verified=await asyncio.to_thread(verified_parts,service,job,workflow)
            renew=receipt['deadline']<=time.time()
            if renew and request.get('renew_deadline') is not True:
                raise ConflictError('deadline expired; explicit renewal or revision required')
            if renew and any(json.loads(r['spec'])['category']!='episode_extract' and r['status']!='succeeded' for r in tasks):
                raise ConflictError('unresolved writing/review task requires an explicit revision')
            if budget and budget['used']>=budget['cap']:
                raise ConflictError('budget exhausted; explicit revision required')
            for row in tasks:
                if row['status']=='succeeded': continue
                spec=json.loads(row['spec'])
                if spec['category']=='episode_extract' and row['id'] in {job+f':extract:{i}' for i in verified}:
                    continue
                if row['status']=='output_rejected' and row['id'].rsplit(':',1)[-1].isdigit():
                    from .citation_repair import request as repair_request
                    batch,shards,_,_=await asyncio.to_thread(verified_parts,service,job,workflow)
                    part=int(row['id'].rsplit(':',1)[1])
                    with service.store.connect() as db:
                        output=db.execute('SELECT output_id FROM tasks WHERE id=?',(row['id'],)).fetchone()
                    if output and output[0] and 0<=part<len(shards):
                        text=service.store.artifact(output[0])['text']
                        if repair_request(text,batch,shards[part]):continue
                x,y=(stages or {}).get(spec['category'],(a,b))
                history=service.store.attempts_for(row['id'])
                if row['status']=='cancelled' or (history and service.scheduler._next(x,y,history) is None):
                    raise ConflictError('previous attempt cannot be safely resumed; explicit revision required')
            if renew:
                with service.store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('CREATE TABLE IF NOT EXISTS deadline_renewals(job_id TEXT,old_deadline REAL,new_deadline REAL,created REAL)')
                    new_deadline=time.time()+float(receipt_contract['job_timeout'])
                    db.execute('INSERT INTO deadline_renewals VALUES(?,?,?,?)',(job,receipt['deadline'],new_deadline,time.time()))
                    db.execute('UPDATE source_receipts SET deadline=? WHERE job_id=?',(new_deadline,job))
            result=await service.start(workflow['batch_id'],workflow['scope'],a,b,
                policy=WritingPolicy(**workflow['policy']),stage_routes=stages,**workflow['options'])
            return {'job_id':result,'status':'resuming','budget_reset':False,'memos_writes':0}
        token=request.get('request_id')
        cap=request.get('job_cap',12)
        if not isinstance(token,str) or not 8<=len(token)<=80 or type(cap) is not int or not 5<=cap<=30:
            raise ValueError('request id and explicit budget (5..30) required')
        upgrade=request.get('upgrade_production_contract',False)
        if type(upgrade) is not bool or (upgrade and action!='reextract'):
            raise ValueError('contract upgrade requires explicit source re-extraction')
        identity=[job,action,cap,request.get('use_current_routes',False)]
        if upgrade:identity.append('claim-audit-v1')
        fingerprint=hashlib.sha256(encoded(identity).encode()).hexdigest()
        with service.store.connect() as db:
            previous=db.execute('SELECT * FROM workflow_revisions WHERE request_id=?',(token,)).fetchone()
            successor=db.execute('SELECT child_job FROM workflow_revisions WHERE parent_job=?',(job,)).fetchone()
        if previous:
            if previous['fingerprint']!=fingerprint: raise ConflictError('request id reused with changed options')
            return {'job_id':previous['child_job'],'status':'revision_exists','model_calls':0}
        if successor: raise ConflictError('this job already has a revision; operate on its successor')
        source=await asyncio.to_thread(load_batch,service.archive,workflow['batch_id'],workflow['scope'])
        with service.store.connect() as db:
            receipt=db.execute('SELECT revision FROM source_receipts WHERE job_id=?',(job,)).fetchone()
        if source.revision!=receipt['revision']: raise ConflictError('source changed; cannot reuse old evidence')
        if request.get('use_current_routes') is True:
            a,b=route_for(plugin,workflow['scope']); stages=stage_routes_for(plugin,workflow['scope'])
        else:
            a,b,stages=frozen_routes(plugin,workflow)
        options={**workflow['options'],'revision':token,'job_cap':cap}
        if upgrade:
            options.update(semantic_audit=True,call_profile=getattr(plugin,'generation_v2_call_profile','balanced-v1'))
        if receipt_contract.get('extraction_version')!='quote-v2':
            options['char_limit']=4000
        child=await service.start(workflow['batch_id'],workflow['scope'],a,b,
            policy=WritingPolicy(**workflow['policy']),stage_routes=stages,_launch=False,**options)
        with service.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if action!='reextract':
                for row in db.execute("SELECT part,result FROM extraction_parts WHERE job_id=? AND status='verified'",(job,)).fetchall():
                    db.execute("UPDATE extraction_parts SET result=?,status='verified' WHERE job_id=? AND part=?",(row['result'],child,row['part']))
            oldplan=db.execute('SELECT result FROM narrative_previews WHERE job_id=?',(job,)).fetchone()
            if action in ('retry','rewrite') and oldplan:
                plan=json.loads(oldplan[0]); plan['job_id']=child
                db.execute('INSERT OR REPLACE INTO narrative_previews VALUES(?,?)',(child,encoded(plan)))
                if action=='retry':
                    db.execute('''INSERT OR IGNORE INTO diary_drafts
                        SELECT ?,ordinal,revision,draft,report,status,created FROM diary_drafts
                        WHERE job_id=? AND status IN ('qualified','review_pending') AND revision=
                        (SELECT MAX(x.revision) FROM diary_drafts x WHERE x.job_id=diary_drafts.job_id
                         AND x.ordinal=diary_drafts.ordinal)''',(child,job))
            db.execute('INSERT INTO workflow_revisions VALUES(?,?,?,?,?,?)',(token,job,child,action,fingerprint,time.time()))
            db.execute("UPDATE workflow_runs SET stage='superseded',updated=? WHERE job_id=?",(time.time(),job))
            db.execute("UPDATE auto_handoffs SET job_id=?,status='manual_review',next_retry=0 WHERE job_id=?",(child,job))
        await service.start(workflow['batch_id'],workflow['scope'],a,b,
            policy=WritingPolicy(**workflow['policy']),stage_routes=stages,**options)
        return {'job_id':child,'parent_job':job,'status':'revision_queued','new_budget':cap,'memos_writes':0}
