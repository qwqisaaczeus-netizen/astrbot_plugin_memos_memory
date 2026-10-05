"""Reversible evidence enrichment. Never rewrites a legacy diary or source."""
import json

from .preflight import readonly
from .projection import EvidenceIndex
from .sources import load_batch
from .store import ConflictError, encoded
from .upgrades import bind_upgrade_plan


def ensure(store):
    with store.connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS legacy_evidence_links(
              episode_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, scope TEXT NOT NULL,
              active INTEGER NOT NULL, proposal TEXT NOT NULL, facts TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS legacy_evidence_fts USING fts5(
              episode_id UNINDEXED, terms);
        ''')


def unchanged(archive,proposal,scope):
    old=proposal['old']
    db=readonly(archive)
    try:
        row=db.execute('SELECT * FROM episodes WHERE episode_id=?',(old['episode_id'],)).fetchone()
        if not row or row['active']!=1: return False
        row=dict(row)
        if any(row.get(k)!=v for k,v in old.items()): return False
    finally: db.close()
    try:
        batch=load_batch(archive,old['source_batch_id'],scope)
        return batch.revision==proposal['source_revision']
    except (ValueError,KeyError): return False


def apply(store,archive,episode_id,job):
    ensure(store)
    plan=store.plan(job)
    proposal=bind_upgrade_plan(store,episode_id,plan)
    with store.connect() as db:
        row=db.execute('SELECT contract FROM workflow_runs WHERE job_id=?',(job,)).fetchone()
    if not row: raise ConflictError('verified workflow required')
    scope=json.loads(row[0])['scope']
    if not unchanged(archive,proposal,scope): raise ConflictError('legacy content or source changed; upgrade withheld')
    facts=[f for f in plan['facts'] if f['evidence_id'] in proposal['new_evidence_ids']]
    if not facts or not proposal['old'].get('memo_name'): raise ConflictError('no eligible grounded links')
    proposal['status']='evidence_link_active'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR REPLACE INTO legacy_evidence_links VALUES(?,?,?,?,?,?)',
            (episode_id,job,scope,1,encoded(proposal),encoded(facts)))
        db.execute('DELETE FROM legacy_evidence_fts WHERE episode_id=?',(episode_id,))
        db.execute('INSERT INTO legacy_evidence_fts VALUES(?,?)',
            (episode_id,' '.join(EvidenceIndex.terms(' '.join(f['claim'] for f in facts)))))
        db.execute('UPDATE upgrade_previews SET proposal=? WHERE episode_id=?',(encoded(proposal),episode_id))
    return {'episode_id':episode_id,'status':proposal['status'],'links':len(facts),'memos_writes':0,'model_calls':0}


def rollback(store,episode_id):
    ensure(store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE legacy_evidence_links SET active=0 WHERE episode_id=?',(episode_id,))
        row=db.execute('SELECT proposal FROM upgrade_previews WHERE episode_id=?',(episode_id,)).fetchone()
        if row:
            p=json.loads(row[0]); p['status']='evidence_link_rolled_back'
            db.execute('UPDATE upgrade_previews SET proposal=? WHERE episode_id=?',(encoded(p),episode_id))
    return {'episode_id':episode_id,'status':'evidence_link_rolled_back','memos_writes':0,'model_calls':0}


def search(path,archive,scope,query,limit=20):
    tokens=EvidenceIndex.terms(query)[:64]
    if not tokens or not archive: return []
    db=readonly(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_evidence_fts'").fetchone(): return []
        match=' OR '.join('"'+t+'"' for t in tokens)
        rows=db.execute('''SELECT l.* FROM legacy_evidence_fts f JOIN legacy_evidence_links l
          ON l.episode_id=f.episode_id WHERE legacy_evidence_fts MATCH ? AND l.active=1 AND l.scope=?
          ORDER BY bm25(legacy_evidence_fts) LIMIT ?''',(match,scope,limit)).fetchall()
    finally: db.close()
    hits={}
    for row in rows:
        p=json.loads(row['proposal'])
        if not unchanged(archive,p,scope): continue
        old=p['old']; facts=json.loads(row['facts']); name=old['memo_name']
        selected=[f for f in facts if set(tokens)&set(EvidenceIndex.terms(f['claim']))]
        if not selected: continue
        hit=hits.setdefault(name,{'memo_name':name,'chunk_text':selected[0]['claim'],
          'score':0.7,'relevance':0.7,'lexical_rescue':True,'_v2_evidence':[],
          '_v2_job':row['job_id'],'occurred_at':old.get('occurred_at',''),
          'time_basis':old.get('time_basis','unknown'),'memory_type':old.get('memory_type','plot_fact')})
        seen={f['evidence_id'] for f in hit['_v2_evidence']}
        hit['_v2_evidence'].extend(f for f in selected if f['evidence_id'] not in seen)
    return list(hits.values())
