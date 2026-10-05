"""Explicit zero-call editorial review with immutable audit and source checks."""
import hashlib
import json
import time

from .bindings import final_groups, verify_plan_evidence
from .literary import WritingPolicy, fact_coverage, quality
from .revalidation import verified_parts
from .store import ConflictError, encoded


def revise(service, job, workflow, request):
    if request.get('confirm_local_revision') is not True or request.get('confirm_fact_review') is not True:
        raise ValueError('explicit local correction and factual review confirmation required')
    token = request.get('request_id')
    if not isinstance(token, str) or not 1 <= len(token) <= 160:
        raise ValueError('bounded request identity required')
    entries = request.get('drafts')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
        raise ValueError('complete bounded draft batch required')
    fingerprint = hashlib.sha256(encoded([job, entries]).encode()).hexdigest()
    with service.store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS editorial_revisions (
            request_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
            source_revision TEXT NOT NULL,receipt TEXT NOT NULL,created REAL NOT NULL)''')
        old = db.execute('SELECT * FROM editorial_revisions WHERE request_id=?', (token,)).fetchone()
        if old:
            if old['job_id'] != job or old['fingerprint'] != fingerprint:
                raise ConflictError('editorial request identity changed')
            return json.loads(old['receipt'])
        if db.execute('SELECT 1 FROM publish_batches WHERE job_id=?', (job,)).fetchone():
            raise ConflictError('cannot revise an immutable publication')
    batch, shards, _, verified = verified_parts(service, job, workflow)
    if len(verified) != len(shards):
        raise ConflictError('all extraction parts must be verified before editorial review')
    plan = service.store.plan(job)
    verify_plan_evidence(plan, batch)
    old_rows = service.store.drafts(job)
    rows = {r['ordinal']: r for r in old_rows}
    drafts = []
    for entry in entries:
        index = entry.get('index')
        if type(index) is not int or index not in rows or type(entry.get('expected_revision')) is not int:
            raise ValueError('draft identity and expected revision required')
        if rows[index]['revision'] != entry['expected_revision']:
            raise ConflictError('draft was revised concurrently')
        reason = entry.get('review_reason')
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 4000:
            raise ValueError('concrete factual review rationale required for each diary')
        drafts.append({k: entry.get(k) for k in ('index', 'title', 'body', 'fact_ids')})
        if 'prose_links' in entry:drafts[-1]['prose_links']=entry['prose_links']
    groups = final_groups(plan, drafts)
    policy = WritingPolicy(**workflow['policy'])
    reports = {}
    facts = {f['id']: f for f in plan['facts']}
    for draft in drafts:
        i = draft['index']
        tids = {c['turn_id'] for fid in groups[i]['fact_ids'] for c in facts[fid]['citations']}
        source = '\n'.join(t.content for t in batch.turns if t.id in tids)
        report = quality(draft, source, policy)
        fact_coverage(draft, groups[i], report)
        if report['blocking']:
            raise ConflictError('local correction failed: ' + ','.join(report['blocking']))
        entry = next(e for e in entries if e['index'] == i)
        report.update(binding=groups[i], editorial_review={
            'kind': 'explicit_local_review', 'reason': entry['review_reason'],
            'request_id': token, 'source_revision': batch.revision,
            'previous_report': json.loads(rows[i]['report'])},
            machine_review_passed=False, semantic_verified=False, operator_reviewed=True)
        reports[i] = report
    from .sources import load_batch
    if load_batch(service.archive, batch.batch_id, workflow['scope']).revision != batch.revision:
        raise ConflictError('source changed during editorial review')
    receipt = {'job_id': job, 'status': 'draft_ready', 'model_calls': 0, 'memos_writes': 0,
               'source_revision': batch.revision,
               'revisions': {str(i): rows[i]['revision'] + 1 for i in reports}}
    with service.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM publish_batches WHERE job_id=?', (job,)).fetchone():
            raise ConflictError('publication started during editorial review')
        for old in old_rows:
            current = db.execute('SELECT MAX(revision) FROM diary_drafts WHERE job_id=? AND ordinal=?',
                                 (job, old['ordinal'])).fetchone()[0]
            if current != old['revision']:
                raise ConflictError('draft revision changed during editorial review')
        for draft in drafts:
            i = draft['index']
            db.execute('INSERT INTO diary_drafts VALUES(?,?,?,?,?,?,?)',
                       (job, i, rows[i]['revision']+1, encoded(draft), encoded(reports[i]), 'qualified', time.time()))
        db.execute('INSERT INTO editorial_revisions VALUES(?,?,?,?,?,?)',
                   (token, job, fingerprint, batch.revision, encoded(receipt), time.time()))
        db.execute("UPDATE workflow_runs SET stage='draft_ready',error_kind='',updated=? WHERE job_id=?",
                   (time.time(), job))
    return receipt
