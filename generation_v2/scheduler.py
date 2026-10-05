"""Bounded workers; one retry owner, finite durable budgets, unchanged inputs."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
import hashlib
import json
import time
import uuid

from .adapters import classify_error
from .contracts import effective_timeout
from .runner import SingleAttemptRunner
from .store import ConflictError


@dataclass(frozen=True)
class Route:
    identity: str
    kind: str
    endpoint: str
    fault_domain: str
    adapter: object

    def __post_init__(self):
        if self.kind not in {'astr', 'direct'} or not all((self.identity, self.endpoint, self.fault_domain)):
            raise ValueError('complete route identity required')

    @property
    def digest(self):
        suffix = getattr(self.adapter, 'policy_digest', '')
        return hashlib.sha256((self.identity + (':'+suffix if suffix else '')).encode()).hexdigest()


class Scheduler:
    def __init__(self, store, *, workers=8, queue_size=128, endpoint_limit=2, job_limit=2):
        if not 1 <= workers <= 16 or not 1 <= queue_size <= 1024:
            raise ValueError('invalid worker/queue bound')
        if not 1 <= endpoint_limit <= 10 or not 1 <= job_limit <= 6:
            raise ValueError('invalid concurrency bound')
        self.store = store
        self.worker_count, self.queue_size = workers, queue_size
        self.endpoint_limit, self.job_limit = endpoint_limit, job_limit
        self.lanes = {'astr': asyncio.Semaphore(6), 'direct': asyncio.Semaphore(10)}
        self.endpoints, self.jobs = {}, {}
        self.condition = asyncio.Condition()
        self.queue, self.workers, self.active, self.futures = [], [], {}, {}
        self.active_jobs = {}
        self.closed = False
        self.owner = uuid.uuid4().hex

    async def submit(self, task_id, primary, backup=None, *, timeout=180, job_cap=6, validator=None):
        if self.closed:
            raise RuntimeError('scheduler closed')
        timeout = effective_timeout(timeout, 180)
        if backup and backup.digest == primary.digest:
            raise ValueError('backup must have a distinct route identity')
        row = await asyncio.to_thread(self.store.read, task_id)
        spec = json.loads(row['spec'])
        policy = {'primary':primary.digest, 'backup':backup.digest if backup else None,
                  'timeout':timeout, 'same_fault_domain': bool(backup and backup.fault_domain == primary.fault_domain)}
        await asyncio.to_thread(self.store.attach, task_id, policy, job_cap)
        async with self.condition:
            if self.closed:
                raise RuntimeError('scheduler closed')
            future = self.futures.get(task_id)
            if future is None or future.done():
                if len(self.queue) >= self.queue_size:
                    raise RuntimeError('scheduler queue full')
                future = asyncio.get_running_loop().create_future()
                self.futures[task_id] = future
                self.queue.append((time.monotonic(), spec['priority'], task_id, primary, backup, timeout, validator, spec))
                if not self.workers:
                    self.workers = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]
                self.condition.notify()
        # A disconnected WebUI waiter must not cancel a shared background job.
        return await asyncio.shield(future)

    async def _worker(self):
        while True:
            async with self.condition:
                await self.condition.wait_for(lambda: self.closed or self.queue)
                if self.closed:
                    return
                now = time.monotonic()
                i = min(range(len(self.queue)), key=lambda i: self.queue[i][1] - (now-self.queue[i][0])/30)
                item = self.queue.pop(i)
                tid = item[2]
                task = asyncio.create_task(self._execute(*item[2:]))
                self.active[tid] = task
                self.active_jobs[tid] = item[-1]['job_id']
            future = self.futures[tid]
            try:
                result = await task
            except asyncio.CancelledError:
                future.cancel()
            except Exception as exc:
                if not future.done():
                    # Do not share a traceback containing this long-lived worker
                    # with consumers that may clear exception frames.
                    error = ConflictError('task ownership or policy conflict') if isinstance(exc, ConflictError) else RuntimeError('scheduled task failed: ' + type(exc).__name__)
                    future.set_exception(error)
                    future.exception()  # Background errors remain observable without warning leaks.
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                self.active.pop(tid, None)
                self.active_jobs.pop(tid, None)
                self.futures.pop(tid, None)
                job_id = item[-1]['job_id']
                if job_id not in self.active_jobs.values() and not any(i[-1]['job_id']==job_id for i in self.queue):
                    self.jobs.pop(job_id, None)

    async def _execute(self, tid, primary, backup, timeout, validator, spec):
        acquire = asyncio.create_task(asyncio.to_thread(self.store.acquire, tid, self.owner))
        try:
            await asyncio.shield(acquire)
        except asyncio.CancelledError:
            await acquire
            await asyncio.to_thread(self.store.pause_pending, tid, 'cancelled')
            await asyncio.to_thread(self.store.release, tid, self.owner)
            raise
        try:
            while True:
                row = await asyncio.to_thread(self.store.read, tid)
                if row['status'] == 'succeeded':
                    return await asyncio.to_thread(self.store.artifact, row['output_id'])
                history = await asyncio.to_thread(self.store.attempts_for, tid)
                route = self._next(primary, backup, history)
                if route is None or row['status'] not in {'pending', 'awaiting_recovery'}:
                    return {'status':row['status'], 'task_id':tid}
                if row['status'] == 'awaiting_recovery':
                    await asyncio.to_thread(self.store.reopen, tid, self.owner)
                remaining = spec['deadline_at'] - time.time()
                if remaining <= 0:
                    await asyncio.to_thread(self.store.pause_pending, tid)
                    return {'status':'deadline_exhausted', 'task_id':tid}
                try:
                    async with asyncio.timeout(remaining):
                        async with AsyncExitStack() as stack:
                            # Acquire narrower gates first to avoid filling a lane with blocked jobs.
                            for semaphore in (self.jobs.setdefault(spec['job_id'], asyncio.Semaphore(self.job_limit)),
                                              self.endpoints.setdefault(route.endpoint, asyncio.Semaphore(self.endpoint_limit)),
                                              self.lanes[route.kind]):
                                await stack.enter_async_context(semaphore)
                            return await SingleAttemptRunner(self.store).run(tid, route.adapter,
                                route_identity=route.identity, route_digest=route.digest, timeout=timeout, validator=validator)
                except asyncio.CancelledError:
                    await asyncio.to_thread(self.store.pause_pending, tid, 'cancelled')
                    raise
                except ConflictError:
                    await asyncio.to_thread(self.store.pause_pending, tid)
                    return {'status':'budget_or_ownership_blocked', 'task_id':tid}
                except Exception as exc:
                    failure = classify_error(exc)
                    if failure.kind in {'rate_limit', 'overload'}:
                        # Do not sleep through Retry-After holding a worker, or retry the same route.
                        # A genuinely separate fault domain may be tried once.
                        if not backup or backup.fault_domain == route.fault_domain:
                            return {'status':'awaiting_recovery', 'task_id':tid}
                    if time.time() >= spec['deadline_at']:
                        await asyncio.to_thread(self.store.pause_pending, tid)
                        return {'status':'deadline_exhausted', 'task_id':tid}
        except asyncio.CancelledError:
            # Cancellation can also arrive during metadata reads between attempts.
            await asyncio.to_thread(self.store.pause_pending, tid, 'cancelled')
            raise
        finally:
            await asyncio.to_thread(self.store.release, tid, self.owner)

    @staticmethod
    def _next(primary, backup, history):
        if not history:
            return primary
        last = history[-1]
        if last['outcome'] != 'awaiting_recovery':
            return None
        if backup and any(r['route_id'] == backup.digest for r in history):
            return None
        meta = json.loads(last['metadata'])
        failure = meta.get('kind')
        diagnostics = meta.get('diagnostics', {})
        if failure in {'timeout', 'transport'} and (diagnostics.get('content_chars',0) or diagnostics.get('reasoning_chars',0)):
            return None  # A costly partial generation is not a failed connection.
        if failure == 'transport' and len(history) == 1:
            return primary
        if failure in {'transport', 'timeout', 'authentication', 'route_configuration', 'rate_limit', 'overload'}:
            if failure in {'rate_limit', 'overload'} and backup and backup.fault_domain == primary.fault_domain:
                return None
            return backup
        return None

    async def cancel(self, task_id):
        async with self.condition:
            self.queue = [i for i in self.queue if i[2] != task_id]
            task = self.active.get(task_id)
            if task:
                task.cancel()
            future = self.futures.get(task_id)
            if future and not task:
                future.cancel()
                self.futures.pop(task_id, None)
        if task:
            await asyncio.gather(task, return_exceptions=True)
        # A child cancelled before its coroutine starts cannot persist its own state.
        await asyncio.to_thread(self.store.pause_pending, task_id, 'cancelled')

    async def close(self):
        async with self.condition:
            self.closed = True
            for item in self.queue:
                self.futures[item[2]].cancel()
                await asyncio.to_thread(self.store.pause_pending, item[2], 'cancelled')
            self.queue.clear()
            for task in self.active.values():
                task.cancel()
            self.condition.notify_all()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.futures.clear()

    async def cancel_job(self, job_id):
        async with self.condition:
            tids = {tid for tid, job in self.active_jobs.items() if job == job_id}
            tids.update(item[2] for item in self.queue if item[-1]['job_id'] == job_id)
        for tid in tids:
            await self.cancel(tid)

    async def resume_registered(self, routes, *, limit=128, validators=None):
        """Explicit restart entry. Route registry and schema validators are caller-owned."""
        if not 1 <= limit <= self.queue_size:
            raise ValueError('recovery page exceeds queue capacity')
        rows = await asyncio.to_thread(self.store.resumable, limit)
        results = []
        for row in rows:
            policy, spec = json.loads(row['policy']), json.loads(row['spec'])
            primary, backup = routes.get(policy['primary']), routes.get(policy.get('backup'))
            validator = (validators or {}).get(spec['schema_version'])
            if primary is None or (policy.get('backup') and backup is None) or validator is None:
                results.append({'task_id':row['id'], 'status':'missing_route_or_validator'})
                continue
            job = await asyncio.to_thread(self.store.job, spec['job_id'])
            results.append(await self.submit(row['id'],primary,backup,timeout=policy['timeout'],
                                             job_cap=job['cap'],validator=validator))
        return results
