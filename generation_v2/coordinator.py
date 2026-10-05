"""One restartable owner for extraction, writing and explicit publication.

Drafting never consumes the live buffer and never writes Memos. Publication is
a separate operation, so a disconnected reviewer cannot accidentally publish.
"""
import asyncio
from dataclasses import asdict
import json
import time
from zoneinfo import ZoneInfo

from .literary import DiaryWriter, WritingPolicy
from .production import EvidencePipeline
from .projection import EvidenceIndex
from .store import ConflictError, encoded
from .sources import load_batch, split_batch


class Coordinator:
    def __init__(self, store, scheduler, archive):
        self.store, self.scheduler, self.archive = store, scheduler, archive
        self.pipeline = EvidencePipeline(store, scheduler, archive)
        self.writer = DiaryWriter(store, scheduler, archive)
        self.index = EvidenceIndex(store)
        self.tasks = {}
        self.delivery_tasks = {}
        self.closed = False
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS workflow_runs (
                job_id TEXT PRIMARY KEY, contract TEXT NOT NULL, stage TEXT NOT NULL,
                error_kind TEXT NOT NULL, updated REAL NOT NULL)''')

    def _stage(self, job, stage, error=''):
        with self.store.connect() as db:
            db.execute('UPDATE workflow_runs SET stage=?,error_kind=?,updated=? WHERE job_id=?',
                       (stage, error, time.time(), job))

    async def start(self, batch_id, scope, route, backup=None, *, policy=None, stage_routes=None, _launch=True, **options):
        if self.closed:
            raise RuntimeError('production coordinator closed')
        policy = policy or WritingPolicy()
        ZoneInfo(policy.timezone)
        # Reserve review capacity before spending the first extraction call.
        source = await asyncio.to_thread(load_batch, self.archive, batch_id, scope)
        count = len(split_batch(source, options.get('char_limit', 4000)))
        required = count + 2 + int(options.get('semantic_audit',False))
        required += (1 if options.get('semantic_audit') else options.get('max_diaries',3)) if policy.review else 0
        if required > options.get('job_cap', 6):
            raise ConflictError('job budget cannot cover extraction, writing and maximum planned reviews')
        batch, shards, job, deadline, contract = await self.pipeline.prepare(batch_id, scope, **options)
        with self.store.connect() as db:
            existing_contract=db.execute('SELECT contract FROM workflow_runs WHERE job_id=?',(job,)).fetchone()
        if existing_contract and 'stage_routes' not in json.loads(existing_contract[0]):
            stage_routes=None  # Pre-RC2 workflows retain their original route contract.
        workflow={'scope': scope, 'batch_id': batch_id, 'options': options,
                         'policy': asdict(policy), 'primary': route.digest,
                         'backup': backup.digest if backup else None}
        if stage_routes:
            workflow['stage_routes']={key:[a.digest,b.digest if b else None] for key,(a,b) in stage_routes.items()}
        value = encoded(workflow)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT contract FROM workflow_runs WHERE job_id=?', (job,)).fetchone()
            if old and old[0] != value:
                raise ConflictError('workflow identity cannot change route, scope, policy or budget')
            db.execute('INSERT OR IGNORE INTO workflow_runs VALUES(?,?,?,?,?)',
                       (job, value, 'queued', '', time.time()))
        existing = self.tasks.get(job)
        if _launch and (existing is None or existing.done()):
            self.tasks[job] = asyncio.create_task(self._draft(job, scope, route, backup,
                                                           policy, (batch, shards, job, deadline, contract),stage_routes))
            self.tasks[job].add_done_callback(lambda task: self._release(job, task))
        return job

    def _release(self, job, task):
        if self.tasks.get(job) is task:
            self.tasks.pop(job, None)
        if not task.cancelled():
            task.exception()

    async def _draft(self, job, scope, route, backup, policy, prepared, stage_routes=None):
        try:
            from .adapters import production_route
            profile=prepared[4].get('call_profile','legacy')
            route,backup=production_route(route,profile),production_route(backup,profile)
            stage_routes={key:(production_route(a,profile),production_route(b,profile))
                          for key,(a,b) in (stage_routes or {}).items()}
            self._stage(job, 'extracting')
            with self.store.connect() as db:
                has_plan=db.execute('SELECT 1 FROM narrative_previews WHERE job_id=?',(job,)).fetchone()
            if not has_plan:
                await self.pipeline._extract(prepared, route, backup, stage_routes)
            self._stage(job, 'writing_reviewing')
            await self.writer.write(job, scope, route, backup, policy, stage_routes)
            self._stage(job, 'draft_ready')
        except asyncio.CancelledError:
            await self.scheduler.cancel_job(job)
            self._stage(job, 'paused', 'CancelledError')
            raise
        except Exception as exc:
            # Keep private prompts and provider exception strings out of public status.
            self._stage(job, 'awaiting_recovery', type(exc).__name__)
        return self.status(job)

    def status(self, job):
        with self.store.connect() as db:
            row = db.execute('SELECT job_id,stage,error_kind,updated FROM workflow_runs WHERE job_id=?', (job,)).fetchone()
        if not row:
            raise KeyError(job)
        return dict(row)

    async def wait(self, job):
        task = self.tasks.get(job)
        if task:
            await asyncio.shield(task)
        return self.status(job)

    async def publish(self, job, scope, publisher, *, replaces=(), state_sink=None):
        with self.store.connect() as db:
            row = db.execute('SELECT contract FROM workflow_runs WHERE job_id=?', (job,)).fetchone()
        if not row or json.loads(row[0])['scope'] != scope:
            raise ConflictError('unknown workflow or wrong scope')
        if self.tasks.get(job) and not self.tasks[job].done():
            raise ConflictError('drafting is still running')
        try:
            publisher.prepare(job, scope, replaces=replaces)
            self._stage(job, 'publishing')
            await publisher.publish(job)
            self._stage(job, 'indexing')
            await publisher.deliver_index(job, self.index)
            self._stage(job, 'state_pending')
            if state_sink is not None:
                await publisher.deliver_state(job, state_sink)
                self._stage(job, 'published')
        except Exception as exc:
            self._stage(job, 'awaiting_recovery', type(exc).__name__)
            raise
        return self.status(job)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values()) + list(self.delivery_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.scheduler.close()
