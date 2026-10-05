"""Versioned upgrade proposals. Never mutates active legacy memories."""
import hashlib
import json
from pathlib import Path

from .preflight import inspect_database, readonly
from .sources import load_batch
from .store import ConflictError, encoded


def stage_upgrades(source: Path, store):
    # Intended for a consistent offline snapshot, not an in-flight live DB.
    report=inspect_database(source)
    if report['status']!='inspected':
        raise ValueError('source schema is not fully inspectable')
    db=readonly(source)
    try:
        db.execute('BEGIN')
        proposals=[]
        for item in report['episodes']:
            row=dict(db.execute('SELECT * FROM episodes WHERE episode_id=?',(item['episode_id'],)).fetchone())
            preserved={k:row.get(k) for k in ('episode_id','memo_name','source_batch_id','event_ts','occurred_at',
                       'time_basis','original_memo_version','diary_content_hash','source_updated_ts','active',
                       'scene_start_turn','scene_end_turn','memory_type','evidence_quality')}
            ranges=[]
            revision=None
            if item['classification']=='source_complete':
                scope=db.execute('SELECT session_id FROM source_batches WHERE batch_id=?',(item['source_batch_id'],)).fetchone()[0]
                batch=load_batch(source,item['source_batch_id'],scope)
                revision=batch.revision
                ranges=[{'turn_id':t.id,'start':0,'end':len(t.content)} for t in batch.turns
                        if row['scene_start_turn']<=t.index<=row['scene_end_turn']]
            proposal={'old':preserved,'source_grade':item['classification'],'action':item['action'],
                      'source_revision':revision,'verified_ranges':ranges,'new_evidence_ids':[],
                      'active_changed':False,'status':'preview_only',
                      'derived_metadata':{'memory_type':row.get('memory_type'), 'source_level':item['classification']}}
            fingerprint=hashlib.sha256(encoded([preserved,revision,ranges]).encode()).hexdigest()
            proposals.append((item['episode_id'],item['classification'],fingerprint,encoded(proposal)))
        with store.connect() as target:
            target.execute('BEGIN IMMEDIATE')
            for eid,grade,fingerprint,proposal in proposals:
                old=target.execute('SELECT source_fingerprint FROM upgrade_previews WHERE episode_id=?',(eid,)).fetchone()
                if old and old[0]!=fingerprint:
                    raise ConflictError('legacy source changed; review upgrade proposal first')
                target.execute('INSERT OR IGNORE INTO upgrade_previews VALUES(?,?,?,?)',(eid,grade,fingerprint,proposal))
        return report['summary']
    finally:
        db.close()


def bind_upgrade_plan(store,episode_id,plan):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT proposal FROM upgrade_previews WHERE episode_id=?',(episode_id,)).fetchone()
        if row is None:
            raise KeyError(episode_id)
        proposal=json.loads(row[0])
        saved=db.execute('SELECT result FROM narrative_previews WHERE job_id=?',(plan.get('job_id'),)).fetchone()
        if saved is None or encoded(json.loads(saved[0]))!=encoded(plan):
            raise ConflictError('upgrade requires the persisted verified narrative preview')
        if proposal['source_grade']!='source_complete' or proposal['source_revision']!=plan.get('source_revision'):
            raise ConflictError('upgrade plan not grounded in this legacy source')
        allowed={r['turn_id']:r for r in proposal['verified_ranges']}
        links=[]
        for fact in plan['facts']:
            if all(c['turn_id'] in allowed and allowed[c['turn_id']]['start']<=c['start']<c['end']<=allowed[c['turn_id']]['end']
                   for c in fact['citations']):
                links.append(fact['evidence_id'])
        proposal['new_evidence_ids']=sorted(set(links))
        proposal['candidate_job_id']=plan['job_id']
        proposal['status']='evidence_link_preview'
        db.execute('UPDATE upgrade_previews SET proposal=? WHERE episode_id=?',(encoded(proposal),episode_id))
        return proposal
