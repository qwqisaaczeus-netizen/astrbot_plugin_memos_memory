"""Restore per-diary review receipts from an existing verified task artifact."""
import json

from .evidence import parse_object
from .literary import WritingPolicy,quality,fact_coverage,apply_review
from .store import ConflictError
from .bindings import final_groups


def restore(service,job,workflow,batch):
    with service.store.connect() as db:
        task=db.execute("SELECT input_id,output_id FROM tasks WHERE id=? AND status='succeeded'",
                        (job+':review:batch:0',)).fetchone()
    if not task:return []
    plan=service.store.plan(job)
    if plan['source_revision']!=batch.revision:raise ConflictError('review source changed')
    request=parse_object(service.store.artifact(task['input_id'])['prompt'])
    response=parse_object(service.store.artifact(task['output_id'])['text'])
    inputs={d['index']:d for d in request['drafts']}
    reviews=response.get('reviews',[])
    if (len(reviews)!=len(inputs) or any(type(r.get('index')) is not int for r in reviews)
            or {r['index'] for r in reviews}!=set(inputs)):
        raise ConflictError('cached review identity mismatch')
    stored={r['ordinal']:r for r in service.store.drafts(job)}
    with service.store.connect() as db:
        contract=db.execute('SELECT contract FROM writing_contracts WHERE job_id=?',(job,)).fetchone()
    version=json.loads(contract[0]).get('writer') if contract else None
    groups=final_groups(plan,[json.loads(row['draft']) for row in stored.values()]) if version in ('literary-v3','literary-v4') else plan['narratives']
    facts={f['id']:f for f in plan['facts']};policy=WritingPolicy(**workflow['policy'])
    prepared=[]
    for review in reviews:
        i=review['index'];row=stored.get(i)
        if row and json.loads(row['report']).get('operator_reviewed'):continue
        if not row or json.loads(row['draft'])!=inputs[i]:continue
        if not 0<=i<len(plan['narratives']):raise ConflictError('foreign review')
        group=groups[i]
        supplied=next((g for g in request['narratives'] if g['index']==i),None)
        if not supplied or {f['id'] for f in supplied['facts']}!=set(group['fact_ids']):
            raise ConflictError('review evidence contract changed')
        tids={c['turn_id'] for fid in group['fact_ids'] for c in facts[fid]['citations']}
        source='\n'.join(t.content for t in batch.turns if t.id in tids)
        report=quality(inputs[i],source,policy);fact_coverage(inputs[i],group,report)
        apply_review(report,review,group,allow_advisory=version in ('literary-v3','literary-v4'))
        report['review_artifact_id']=task['output_id']
        prepared.append((row,report))
    for row,report in prepared:
        service.store.save_draft(job,row['ordinal'],row['revision'],json.loads(row['draft']),report,
                                 'awaiting_revision' if report['blocking'] else 'qualified')
    return [row['ordinal'] for row,_ in prepared]
