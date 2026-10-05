"""Read-only workbench and explicitly requested offline migration preview."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from .literary import DiaryStore
from .preflight import readonly
from .upgrades import stage_upgrades


def ledger_path(plugin):
    directory=getattr(plugin,'runtime_state_dir',None)
    if directory is None: return None
    return Path(directory)/'generation_runtime.db'


def overview(plugin,offset=0):
    if type(offset) is not int or not 0<=offset<=1000000: raise ValueError('invalid offset')
    path=ledger_path(plugin)
    empty={'enabled':False,'jobs':[],'upgrades':[],'grades':{},'available':False,
           'draft_entry':True,'automatic_publication':False,'sources':[], 'provider_options':[]}
    empty.update(enabled=bool(getattr(plugin,'generation_v2_enable',False)),
                 automatic_publication=bool(getattr(plugin,'generation_v2_enable',False) and
                                            getattr(plugin,'generation_v2_memos_capabilities_confirmed',False)),
                 handoffs=[], mind_calls=[])
    from .operations import readiness
    empty['readiness']=readiness(plugin)
    archive=getattr(getattr(plugin,'_episodes',None),'db_path',None)
    if archive and Path(archive).is_file():
        source=readonly(Path(archive))
        try:
            empty['sources']=[dict(r) for r in source.execute(
                'SELECT batch_id,message_count FROM source_batches ORDER BY rowid DESC LIMIT 50')]
        finally: source.close()
    resolver=getattr(plugin,'_chat_provider_options',None)
    if callable(resolver): empty['provider_options']=resolver()
    if path is None or not path.is_file(): return empty
    db=readonly(path)
    try:
        if db.execute('PRAGMA application_id').fetchone()[0]!=1296905777:
            raise ValueError('not a generation runtime ledger')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        empty['mind_calls']=[{'task':json.loads(r['spec'])['category'],'status':r['status'],
            'attempts':r['attempts'],'updated':r['updated']} for r in db.execute(
            "SELECT spec,status,attempts,updated FROM tasks WHERE id LIKE 'mind:%' ORDER BY updated DESC LIMIT 50")]
        if 'auto_handoffs' in tables:
            empty['handoffs']=[dict(r) for r in db.execute('''SELECT batch_id,source_kind,job_id,status,
                next_retry,error_kind,created FROM auto_handoffs ORDER BY created DESC LIMIT 50''')]
        jobs=[]
        if 'source_receipts' in tables:
            for row in db.execute('SELECT job_id,batch_id,status,created FROM source_receipts ORDER BY created DESC LIMIT 50'):
                item=dict(row)
                if 'workflow_runs' in tables:
                    run=db.execute('SELECT stage,error_kind,updated FROM workflow_runs WHERE job_id=?',(row['job_id'],)).fetchone()
                    if run:
                        item['workflow']=dict(run)
                        item['status']=run['stage']
                        # A stopped process cannot be presented as still running.
                        service=getattr(plugin,'_generation_v2_service',None)
                        task=getattr(service,'tasks',{}).get(row['job_id'])
                        if run['stage'] in ('queued','extracting','writing_reviewing') and (task is None or task.done()):
                            item['status']='interrupted_pending_resume'
                        delivery=getattr(service,'delivery_tasks',{}).get(row['job_id'])
                        if run['stage'] in ('publishing','indexing','state_pending') and (delivery is None or delivery.done()):
                            # Ordinary production is owned by Astr's compression task, not this registry.
                            scope_row=db.execute('SELECT scope FROM auto_handoffs WHERE job_id=?',(row['job_id'],)).fetchone() if 'auto_handoffs' in tables else None
                            lock=getattr(plugin,'_compress_locks',{}).get(scope_row[0]) if scope_row else None
                            if lock is None or not lock.locked(): item['status']='delivery_pending_resume'
                item['parts']=[dict(r) for r in db.execute('SELECT part,status FROM extraction_parts WHERE job_id=? ORDER BY part',(row['job_id'],))]
                item['drafts']=[]
                if 'diary_drafts' in tables:
                    item['drafts']=[{'ordinal':r['ordinal'],'revision':r['revision'],'status':r['status'],
                                     'draft':json.loads(r['draft']),'report':json.loads(r['report'])}
                        for r in db.execute('''SELECT d.* FROM diary_drafts d WHERE job_id=? AND revision=
                          (SELECT MAX(revision) FROM diary_drafts x WHERE x.job_id=d.job_id AND x.ordinal=d.ordinal)
                          ORDER BY ordinal''',(row['job_id'],))]
                item['publication']=[]
                if 'publish_items' in tables:
                    item['publication']=[dict(r) for r in db.execute('SELECT ordinal,name,event_time,status FROM publish_items WHERE job_id=?',(row['job_id'],))]
                item['attempts']=[]
                for attempt in db.execute('''SELECT a.ordinal,a.started,a.ended,a.outcome,a.metadata,t.spec
                    FROM attempts a JOIN tasks t ON t.id=a.task_id
                    WHERE json_extract(t.spec,'$.job_id')=? ORDER BY a.started''',(row['job_id'],)):
                    meta=json.loads(attempt['metadata']); spec=json.loads(attempt['spec'])
                    usage={k:v for k,v in meta.get('usage',{}).items()
                           if k in ('prompt_tokens','completion_tokens','total_tokens') and type(v) is int and v>=0}
                    item['attempts'].append({'task':spec['category'],'ordinal':attempt['ordinal'],
                        'outcome':attempt['outcome'],'error_kind':meta.get('kind',''),
                        'elapsed_ms':round(max(0,(attempt['ended'] or time.time())-attempt['started'])*1000),
                        'usage':usage})
                    diagnostics = meta.get('diagnostics', meta.get('capabilities', {}).get('diagnostics', {}))
                    item['attempts'][-1]['diagnostics'] = {k: v for k, v in diagnostics.items()
                        if k in ('finish_reason', 'reasoning_chars', 'content_chars', 'first_event_ms',
                                 'first_content_ms', 'max_tokens', 'stream', 'completion_tokens',
                                 'reasoning_tokens', 'timeout_seconds', 'timeout_phase', 'idle_timeout_seconds') and isinstance(v, (str, int, float, bool, type(None)))}
                    request_policy=meta.get('capabilities',{}).get('request_policy',{})
                    item['attempts'][-1]['request_policy']={k:v for k,v in request_policy.items()
                        if k in ('thinking','reasoning_effort','max_tokens','stream','idle_timeout')}
                receipt=db.execute('SELECT deadline,contract FROM source_receipts WHERE job_id=?',(row['job_id'],)).fetchone()
                item['budget']={'used':len(item['attempts']),'cap':json.loads(receipt['contract'])['job_cap'],
                                'deadline':receipt['deadline'],'expired':receipt['deadline']<=time.time()}
                production_contract=json.loads(receipt['contract'])
                item['call_profile']=production_contract.get('call_profile','legacy')
                item['claim_audit']={'enabled':bool(production_contract.get('semantic_audit')),'status':'not_requested'}
                if 'narrative_previews' in tables:
                    preview=db.execute('SELECT result FROM narrative_previews WHERE job_id=?',(row['job_id'],)).fetchone()
                    if preview:
                        audit=json.loads(preview[0]).get('claim_audit')
                        if audit:item['claim_audit'].update(status='machine_reviewed' if audit.get('performed',True) else 'not_needed_empty',**audit)
                    elif production_contract.get('semantic_audit'):
                        item['claim_audit']['status']='pending_or_interrupted'
                item['token_summary']={k:sum(a['usage'].get(k,0) for a in item['attempts'])
                                       for k in ('prompt_tokens','completion_tokens','total_tokens')}
                item['token_summary']['usage_missing_attempts']=sum('total_tokens' not in a['usage'] for a in item['attempts'])
                item['token_summary']['reasoning_tokens']=sum(a['diagnostics'].get('reasoning_tokens',0) or 0
                                                               for a in item['attempts'])
                if 'writing_contracts' in tables:
                    contract=db.execute('SELECT contract FROM writing_contracts WHERE job_id=?',(row['job_id'],)).fetchone()
                    item['writer_version']=json.loads(contract[0]).get('writer') if contract else None
                jobs.append(item)
        upgrades=[]; grades={}
        if 'upgrade_previews' in tables:
            grades={r[0]:r[1] for r in db.execute('SELECT source_grade,COUNT(*) FROM upgrade_previews GROUP BY source_grade')}
            for r in db.execute('SELECT episode_id,source_grade,proposal FROM upgrade_previews ORDER BY episode_id LIMIT 100 OFFSET ?',(offset,)):
                p=json.loads(r['proposal'])
                upgrades.append({'episode_id':r['episode_id'],'grade':r['source_grade'],'memo_name':p['old'].get('memo_name'),
                                 'status':p['status'],'links':len(p['new_evidence_ids']),'ranges':len(p['verified_ranges']),
                                 'batch_id':p['old'].get('source_batch_id'),
                                 'candidate_job_id':p.get('candidate_job_id')})
        return {**empty,'available':True,'jobs':jobs,'upgrades':upgrades,'grades':grades,'offset':offset,'total':sum(grades.values())}
    finally: db.close()


def migration_preview(plugin):
    path=ledger_path(plugin)
    archive=getattr(getattr(plugin,'_episodes',None),'db_path',None)
    if path is None or not archive: raise ValueError('stable storage or original archive unavailable')
    # Online backup gives all related tables one consistent snapshot without altering the live DB.
    with tempfile.TemporaryDirectory(prefix='memos-v2-preview-') as directory:
        snapshot=Path(directory)/'source.db'
        src=readonly(Path(archive)); dst=sqlite3.connect(snapshot)
        try: src.backup(dst)
        finally: dst.close(); src.close()
        summary=stage_upgrades(snapshot,DiaryStore(path))
    return {'grades':summary,'status':'preview_only','model_calls':0,'memos_writes':0}
