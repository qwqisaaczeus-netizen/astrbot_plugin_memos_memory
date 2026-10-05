"""Offline real plugin SQLite benchmark; bounded graph work is reported honestly."""
import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.thread_builder import ThreadBuilder
from astrbot_plugin_memos_memory.thread_candidates import ThreadCandidates
from astrbot_plugin_memos_memory.thread_projector import ThreadProjector
from astrbot_plugin_memos_memory.thread_retrieval import ThreadRetrievalLab
from astrbot_plugin_memos_memory.claim_ledger import ClaimLedger

ROOT = Path(os.environ.get(
    'BENCH_ROOT',
    str(Path(__file__).parent / 'evidence' / 'completion-scale'),
))


def seed(conn, count, start=0):
    """Bulk-load the real episode schema with deliberately skewed topics."""
    now = time.time()
    rows = []
    scopes = []
    for i in range(start, start + count):
        episode_id = f'e{i:08d}'
        scope = 'hot' if i % 10 < 8 else f'scope{i % 19}'
        rare = f'rare{i // 1000}' if i % 1000 < 2 else ''
        rows.append((
            episode_id,
            f'memos/{i}',
            '2026-01-01',
            1700000000 + (i % 365) * 86400,
            'plot_fact',
            'everyday topic ' + rare,
            'common conversation ' + rare,
            json.dumps(['frequent', rare] if rare else ['frequent']),
            'diary_derived',
            '[]',
            'common conversation ' + rare,
            now,
            now,
        ))
        scopes.append((episode_id, scope, 'benchmark', 1.0, now, now))
    conn.executemany(
        '''INSERT INTO episodes(
            episode_id,memo_name,occurred_at,event_ts,memory_type,
            scene_anchor,retrieval_key,entities_json,evidence_quality,
            unresolved_json,card_text,created_ts,updated_ts)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        rows,
    )
    conn.executemany(
        '''INSERT INTO thread_episode_scopes(
            episode_id,scope_id,assignment_source,confidence,created_ts,updated_ts)
           VALUES(?,?,?,?,?,?)''',
        scopes,
    )
    conn.commit()

def quant(values):
    values = sorted(values)
    return {f'p{p}': round(values[min(len(values)-1, int((len(values)-1)*p/100))], 4) if values else None for p in (50,95,99)} | {'samples': len(values)}

def timed(fn, samples):
    t = time.perf_counter(); r = fn(); samples.append((time.perf_counter()-t)*1000); return r

def rss():
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [(k, ctypes.c_size_t) for k in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
            c = Counters(); c.cb = ctypes.sizeof(c)
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
            return c.WorkingSetSize
        return None

def scale(n):
    db = ROOT / f'workload-{n}.db'
    if db.exists(): raise RuntimeError(f'refusing overwrite {db}')
    store = EpisodicStore(str(db), None, 'test10-benchmark')
    asyncio.run(store.init()); conn=store._connect(); ts=store._threads
    result={'scale':n, 'sqlite':sqlite3.sqlite_version, 'python':platform.python_version(), 'timings_ms':{}, 'workload':'80% hot scope; 20 other scopes; frequent common terms/entities; rare pair per 1000; 365 event dates; source updates after build'}
    started=time.perf_counter(); seed(conn,n); result['seed_seconds']=time.perf_counter()-started
    # Real candidate materialization over every row, no list-only timing.
    c=ThreadCandidates(store._connect); samples=[]; cursor=''; count=0
    started=time.perf_counter()
    while True:
        r=timed(lambda:c.backfill_batch(after_episode_id=cursor,limit=200),samples)
        cursor=r['after_episode_id']; count+=r['processed']
        if r['completed']:break
    result['candidate_backfill_rows']=count;result['candidate_backfill_seconds']=time.perf_counter()-started
    result['timings_ms']['candidate_backfill_batch_200']=quant(samples)
    # Queue through public plugin API, then actual builder with a per-scale budget.
    started=time.perf_counter()
    for row in conn.execute('SELECT episode_id,scope_id FROM thread_episode_scopes').fetchall(): ts.enqueue(row['scope_id'],row['episode_id'])
    result['enqueue_seconds']=time.perf_counter()-started
    builder=ThreadBuilder(ts,store._connect,store._lock); samples=[]; candidates=[];processed=0; errors=0
    started=time.perf_counter()
    while processed<n and time.perf_counter()-started<float(os.environ.get('GRAPH_BUDGET_SECONDS', '45')):
        r=timed(lambda:builder.process_batch(10),samples)
        processed+=r.get('processed',0);errors+=r.get('errors',0);candidates.extend(r.get('candidate_counts',[]))
        if not r.get('processed'):break
    result['graph_backfill_seconds']=time.perf_counter()-started;result['graph_backfill_processed']=processed;result['graph_backfill_errors']=errors
    result['graph_backfill_complete']=processed==n;result['timings_ms']['graph_backfill_batch_10']=quant(samples);result['candidate_counts']=quant(candidates)
    # Date edits requeue completed records and exercise dirty index invalidation.
    samples=[]
    for i in range(30):
        eid=f'e{i:08d}'
        conn.execute('UPDATE episodes SET event_ts=event_ts+172800,updated_ts=? WHERE episode_id=?',(time.time(),eid));conn.commit()
        scope=conn.execute('SELECT scope_id FROM thread_episode_scopes WHERE episode_id=?',(eid,)).fetchone()[0]
        ts.enqueue(scope,eid)
        timed(lambda:builder.process_batch(1),samples)
    result['timings_ms']['incremental_queue_build_1']=quant(samples)
    # Populate accepted projections using real store mutation paths, independent
    # of local evidence deliberately refusing common-topic identity assertions.
    for i in range(0,min(n,2000)-1,20):
        ts.upsert_edge(dict(scope_id='hot',source_episode_id=f'e{i:08d}',target_episode_id=f'e{i+1:08d}',edge_type='continues',status='accepted',confidence=.96))
    samples=[];projection=timed(lambda:ThreadProjector(ts).project(scope_id='hot'),samples)
    result['timings_ms']['full_scope_projection']=quant(samples);result['projection']=projection
    samples=[];claims=timed(lambda:ClaimLedger(ts).rebuild_scope('hot'),samples)
    result['timings_ms']['full_scope_claim_rebuild']=quant(samples);result['claim_rebuild']=claims
    lab=ThreadRetrievalLab(ts); queries=[]; lists=[]; candidate_time=[]; metrics=[]
    for i in range(60):
        eid=f'e{(i*1000)%n:08d}';query=f'rare{((i*1000)%n)//1000}'
        timed(lambda:lab.run(scope_id='hot',query=query,plan={'target_entities':[query]},base_result={'hits':[]},record=True),queries)
        timed(lambda:ts.list_threads(scope_id='hot',limit=50),lists)
        timed(lambda:c.candidates_for(eid,'hot'),candidate_time);metrics.append(dict(c.last_metrics))
    result['timings_ms'].update(query=quant(queries),list_threads=quant(lists),candidate_lookup=quant(candidate_time))
    result['candidate_metrics']=metrics
    result['query_plans']={name:[list(r) for r in conn.execute('EXPLAIN QUERY PLAN '+sql,args)] for name,sql,args in (
        ('seed','SELECT DISTINCT e.episode_id,e.event_ts FROM thread_episode_terms t INDEXED BY idx_thread_episode_terms_term CROSS JOIN episodes e ON e.episode_id=t.episode_id CROSS JOIN thread_episode_scopes s ON s.episode_id=e.episode_id WHERE s.scope_id=? AND t.term IN (?) AND e.active=1 ORDER BY e.event_ts DESC LIMIT 30',('hot','rare0')),
        ('route','SELECT t.thread_id,MAX(m.membership_confidence),(SELECT COUNT(*) FROM memory_thread_members m2 WHERE m2.thread_id=t.thread_id) FROM memory_thread_members m JOIN memory_threads t ON t.thread_id=m.thread_id WHERE m.episode_id IN (?) AND t.scope_id=? AND t.status=\'active\' GROUP BY t.thread_id LIMIT 20',('e00000000','hot')),
        ('posting','SELECT episode_id FROM thread_candidate_postings WHERE scope_id=? AND kind=? AND term=? ORDER BY episode_id LIMIT 81',('hot','term','common')),
        ('date','SELECT episode_id,event_ts FROM thread_candidate_episodes WHERE scope_id=? AND event_ts<=? AND event_ts>=? ORDER BY event_ts DESC,episode_id DESC LIMIT 51',('hot',1800000000,1700000000)),
        ('queue','SELECT id FROM thread_build_queue WHERE status=\'pending\' AND next_attempt_ts<=? ORDER BY next_attempt_ts,id LIMIT 10',(time.time(),)),
        ('list','SELECT t.*,(SELECT COUNT(*) FROM memory_thread_members m WHERE m.thread_id=t.thread_id) FROM memory_threads t WHERE scope_id=? ORDER BY last_event_ts DESC,updated_ts DESC LIMIT 50',('hot',)),
    )}
    result['queue']=ts.queue_counts();result['db_bytes']=db.stat().st_size;wal=Path(str(db)+'-wal');result['wal_bytes']=wal.stat().st_size if wal.exists() else 0;result['rss_bytes']=rss()
    result['table_counts']={t:conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('episodes','thread_candidate_postings','memory_episode_edges','memory_threads','memory_claims','thread_materialized_views')}
    result['integrity_check']=conn.execute('PRAGMA integrity_check').fetchone()[0]
    (ROOT/f'benchmark-{n}.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    store.close()
    print(json.dumps({k:result[k] for k in ('scale','candidate_backfill_seconds','graph_backfill_processed','graph_backfill_seconds','timings_ms','db_bytes','wal_bytes','rss_bytes')}),flush=True)
    return result

if __name__=='__main__':
    ROOT.mkdir(parents=True, exist_ok=True)
    scales=[int(x) for x in sys.argv[1:]] or [1000,10000,50000,100000]
    for n in scales:scale(n)
