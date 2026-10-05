"""Explicit local revalidation of retained outputs; no provider calls or source edits."""
import json
import time

from .evidence import validate_extraction
from .quote_grounding import resolve_quotes
from .sources import load_batch,split_batch,shard_payload
from .store import ConflictError,encoded


def verified_parts(service,job,workflow):
    batch=load_batch(service.archive,workflow['batch_id'],workflow['scope'])
    with service.store.connect() as db:
        receipt=db.execute('SELECT revision,contract FROM source_receipts WHERE job_id=?',(job,)).fetchone()
        rows=db.execute("SELECT part,result FROM extraction_parts WHERE job_id=? AND status='verified'",(job,)).fetchall()
    if not receipt or receipt['revision']!=batch.revision:raise ConflictError('source revision changed')
    contract=json.loads(receipt['contract'])
    shards=split_batch(batch,contract['char_limit'])
    indices=set()
    for row in rows:
        if not 0<=row['part']<len(shards):raise ConflictError('invalid cached shard')
        validate_extraction(row['result'],batch,shards[row['part']],contract['max_diaries'])
        indices.add(row['part'])
    return batch,shards,contract,indices


def revalidate(service,job,workflow):
    batch,shards,contract,verified=verified_parts(service,job,workflow)
    if contract.get('extraction_version')!='quote-v2':raise ConflictError('quote-v2 source contract required')
    with service.store.connect() as db:
        rows=db.execute("SELECT id,spec,input_id,output_id FROM tasks WHERE json_extract(spec,'$.job_id')=? AND status='output_rejected'",(job,)).fetchall()
    accepted=[];rejected=[]
    for row in rows:
        spec=json.loads(row['spec'])
        if spec['category']!='episode_extract' or not row['id'].startswith(job+':extract:'):continue
        if not row['id'].rsplit(':',1)[1].isdigit():continue
        part=int(row['id'].rsplit(':',1)[1])
        if part in verified:continue
        if not 0<=part<len(shards):raise ConflictError('foreign shard')
        request=service.store.artifact(row['input_id'])
        payload=json.loads(request['prompt']);expected=shard_payload(batch,shards[part])
        if any(payload.get(key)!=expected[key] for key in expected):raise ConflictError('input source contract changed')
        if payload.get('extraction_schema')!='quote-v2':raise ConflictError('wrong extraction schema')
        output=service.store.artifact(row['output_id'])
        try:result=resolve_quotes(output['text'],batch,shards[part],contract['max_diaries'])
        except (ValueError,TypeError,KeyError,AttributeError) as exc:
            rejected.append({'part':part,'error':str(exc)[:200]});continue
        accepted.append((part,row['output_id'],result))
    if load_batch(service.archive,workflow['batch_id'],workflow['scope']).revision!=batch.revision:
        raise ConflictError('source changed during revalidation')
    with service.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS local_revalidations (
            job_id TEXT NOT NULL,part INTEGER NOT NULL,artifact_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,repairs TEXT NOT NULL,created REAL NOT NULL,
            PRIMARY KEY(job_id,part,artifact_id))''')
        for part,artifact_id,result in accepted:
            db.execute("UPDATE extraction_parts SET status='verified',result=? WHERE job_id=? AND part=?",
                       (encoded(result),job,part))
            db.execute('INSERT OR IGNORE INTO local_revalidations VALUES(?,?,?,?,?,?)',
                       (job,part,artifact_id,batch.revision,encoded(result['compatibility_repairs']),time.time()))
    from .review_cache import restore
    reviewed=restore(service,job,workflow,batch)
    return {'job_id':job,'status':'locally_revalidated','accepted_parts':[x[0] for x in accepted],
            'reviews_reconciled':reviewed,
            'already_verified':sorted(verified),'rejected':rejected,'model_calls':0,'memos_writes':0}
