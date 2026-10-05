"""Atomic V2 projection, staged until a batch's publication is acknowledged."""
import json
import re

from .store import ConflictError, encoded
from .bindings import item_fact_ids


def safe_claim(fact):
    if fact.get('claim_audit',{}).get('status')!='uncertain':return fact['claim']
    quotes=' / '.join(f"{c['role']}: {c['quote']}" for c in fact['citations'])
    return '[解释未确认，仅保留原文证据] '+quotes


def projection_basis(fact):
    return 'inference' if fact.get('claim_audit',{}).get('status')=='uncertain' else fact['basis']


class EvidenceIndex:
    def __init__(self,store):
        self.store=store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS v2_index_events(event_id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS v2_evidence_index(job_id TEXT, evidence_id TEXT, claim TEXT NOT NULL,
                    scope TEXT NOT NULL, batch_id TEXT NOT NULL, citations TEXT NOT NULL, memo_names TEXT NOT NULL,
                    PRIMARY KEY(job_id,evidence_id));
                CREATE VIRTUAL TABLE IF NOT EXISTS v2_evidence_fts USING fts5(
                    job_id UNINDEXED,evidence_id UNINDEXED,terms);
                CREATE TABLE IF NOT EXISTS v2_projection_versions(name TEXT PRIMARY KEY,version INTEGER NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            version=db.execute("SELECT version FROM v2_projection_versions WHERE name='quote_fts'").fetchone()
            rebuild=not version or version[0]<2
            # Upgrade projections created before the lexical route existed.
            for row in db.execute('''SELECT e.* FROM v2_evidence_index e WHERE ? OR NOT EXISTS
                (SELECT 1 FROM v2_evidence_fts f WHERE f.job_id=e.job_id AND f.evidence_id=e.evidence_id)''',(rebuild,)):
                db.execute('DELETE FROM v2_evidence_fts WHERE job_id=? AND evidence_id=?',(row['job_id'],row['evidence_id']))
                db.execute('INSERT INTO v2_evidence_fts VALUES(?,?,?)',
                           (row['job_id'],row['evidence_id'],self.lexical_text(row['claim'],json.loads(row['citations']))))
            db.execute("INSERT OR REPLACE INTO v2_projection_versions VALUES('quote_fts',2)")

    @classmethod
    def lexical_text(cls,claim,citations):
        text=claim+' '+' '.join(c['quote'] for c in citations)
        terms=set(cls.terms(text))
        terms.update('u'+char for char in re.findall(r'[\u4e00-\u9fff]',text))
        return ' '.join(sorted(terms))

    @staticmethod
    def terms(text):
        tokens=re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+',str(text).lower())
        return sorted({part for token in tokens for part in
                       ([token] if token.isascii() else ['u'+token] if len(token)<2 else
                        [token[i:i+2] for i in range(len(token)-1)])})

    async def apply(self,event_id,manifest):
        import asyncio
        await asyncio.to_thread(self._apply,event_id,manifest)

    async def verify(self,event_id,manifest):
        with self.store.connect() as db:
            row=db.execute('SELECT manifest FROM v2_index_events WHERE event_id=?',(event_id,)).fetchone()
            rows={r['evidence_id']:r for r in db.execute('SELECT * FROM v2_evidence_index WHERE job_id=?',(manifest['job_id'],))}
            if not row or row[0]!=encoded(manifest) or len(rows)!=len({f['evidence_id'] for f in manifest['facts']}):return False
            plan=self.store.plan(manifest['job_id'])
            for fact in manifest['facts']:
                actual=rows.get(fact['evidence_id'])
                names=[item['name'] for item in manifest['items'] if fact['id'] in item_fact_ids(item,plan)]
                if (not actual or actual['claim']!=fact['claim'] or actual['scope']!=manifest['scope']
                        or actual['batch_id']!=manifest['batch_id'] or actual['citations']!=encoded(fact['citations'])
                        or actual['memo_names']!=encoded(names)):return False
            return True

    def _apply(self,event_id,manifest):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT manifest FROM v2_index_events WHERE event_id=?',(event_id,)).fetchone()
            payload=encoded(manifest)
            if old:
                if old[0]!=payload: raise ConflictError('index event identity reused with different payload')
                return
            plan=self.store.plan(manifest['job_id'])
            for fact in manifest['facts']:
                names=[item['name'] for item in manifest['items']
                       if fact['id'] in item_fact_ids(item,plan)]
                # Evidence omitted from prose still has a batch association and remains queryable.
                db.execute('INSERT OR IGNORE INTO v2_evidence_index VALUES(?,?,?,?,?,?,?)',
                    (manifest['job_id'],fact['evidence_id'],fact['claim'],manifest['scope'],manifest['batch_id'],
                     encoded(fact['citations']),encoded(names)))
                db.execute('INSERT INTO v2_evidence_fts VALUES(?,?,?)',
                           (manifest['job_id'],fact['evidence_id'],self.lexical_text(fact['claim'],fact['citations'])))
            db.execute('INSERT INTO v2_index_events VALUES(?,?)',(event_id,payload))

    def records(self,scope,limit=100):
        if type(limit) is not int or not 1<=limit<=500: raise ValueError('invalid limit')
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('''SELECT e.* FROM v2_evidence_index e
                JOIN published_generations g ON e.job_id=g.job_id
                WHERE g.active=1 AND e.scope=? ORDER BY e.job_id,e.evidence_id LIMIT ?''',(scope,limit))]

    def search(self,scope,query,limit=30):
        tokens=self.terms(query)[:64]
        if not scope or not tokens: return []
        match=' OR '.join('"'+token.replace('"','""')+'"' for token in tokens)
        with self.store.connect() as db:
            rows=db.execute('''SELECT e.*,g.manifest FROM v2_evidence_fts f
                JOIN v2_evidence_index e ON e.job_id=f.job_id AND e.evidence_id=f.evidence_id
                JOIN published_generations g ON g.job_id=e.job_id
                WHERE v2_evidence_fts MATCH ? AND g.active=1 AND e.scope=?
                ORDER BY bm25(v2_evidence_fts) LIMIT ?''',(match,scope,max(1,min(100,limit)))).fetchall()
        hits={}
        for row in rows:
            manifest=json.loads(row['manifest'])
            names=json.loads(row['memo_names']) or [manifest['items'][0]['name']]
            fact=next((f for f in manifest['facts'] if f['evidence_id']==row['evidence_id']),None)
            if fact is None:continue
            visible_claim=safe_claim(fact)
            overlap=len(set(tokens)&set(self.lexical_text(row['claim'],fact['citations']).split()))/len(tokens)
            score=min(.9,.3+.6*overlap)
            if len(str(query).strip())==1:score=min(score,.5)
            for name in names:
                item=next((x for x in manifest['items'] if x['name']==name),None)
                if not item: continue
                hit=hits.setdefault(name,{'memo_name':name,'chunk_text':visible_claim,
                    'score':score,'relevance':score,'lexical_rescue':True,
                    '_v2_evidence':[],'_v2_job':row['job_id'],
                    'ts_text':item['event_time'],'occurred_at':item['event_time'],
                    'time_basis':item['time_basis'],'memory_type':'plot_fact'})
                hit['score']=hit['relevance']=max(hit['score'],score)
                hit['_v2_evidence'].append({'evidence_id':row['evidence_id'],'claim':visible_claim,
                                            'claim_audit_status':fact.get('claim_audit',{}).get('status','not_audited'),
                                            'basis':projection_basis(fact),
                                            'citations':json.loads(row['citations'])})
        return sorted(hits.values(),key=lambda h:h['score'],reverse=True)[:limit]
