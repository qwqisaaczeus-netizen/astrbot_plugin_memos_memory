"""Test1 executor: one attempt, no scheduling/retry/recovery side effects."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import time

from .adapters import classify_error
from .contracts import effective_timeout
from .store import TaskStore


class SingleAttemptRunner:
    def __init__(self, store: TaskStore):
        self.store = store

    async def run(self, task_id, adapter, *, route_identity: str, timeout=180, validator=None, route_digest=None):
        timeout = effective_timeout(timeout, 180)
        row = await asyncio.to_thread(self.store.read, task_id)
        if row['status'] == 'succeeded':
            return await asyncio.to_thread(self.store.artifact, row['output_id'])
        request = await asyncio.to_thread(self.store.artifact, row['input_id'])
        spec = json.loads(row['spec'])
        remaining = min(timeout, spec['deadline_at'] - time.time())
        if remaining <= 0:
            raise TimeoutError('task deadline expired before dispatch')
        route_id = route_digest or hashlib.sha256(route_identity.encode()).hexdigest()
        # Shield the short transaction: cancellation must not leave an unowned claim.
        claim = asyncio.create_task(asyncio.to_thread(self.store.claim, task_id, route_id))
        try:
            owner = await asyncio.shield(claim)
        except asyncio.CancelledError:
            owner = await claim
            await asyncio.to_thread(self.store.finish, task_id, owner, 'cancelled', {'kind': 'cancelled'})
            raise
        started = time.monotonic()
        try:
            remaining = min(timeout, spec['deadline_at'] - time.time())
            if remaining <= 0:
                raise TimeoutError('task deadline expired during claim')
            result = await adapter.call(**request, timeout=remaining, task_label=spec['category'])
            # Only deterministic synchronous validation; no hidden model invocation.
            verdict = validator(result.text) if validator else True
            if not isinstance(verdict, bool):
                raise TypeError('validator must return bool')
            metadata = {'elapsed_ms': result.elapsed_ms, 'effective_timeout': remaining,
                        'timeout_source': 'min(explicit_attempt,remaining_task)',
                        'capabilities': result.capabilities, 'usage': result.usage,
                        'validation': ('schema_accepted' if validator else 'nonempty_only') if verdict else 'rejected',
                        'provider_request_id': None, 'cost_estimate': None}
            output = {'text': result.text, 'schema_version': spec['schema_version'],
                      'prompt_version': spec['prompt_version']}
            await self._finish(task_id, owner, 'succeeded' if verdict else 'output_rejected', metadata, output)
            return output if verdict else None
        except asyncio.CancelledError:
            await self._failure(task_id, owner, 'cancelled', {'kind': 'cancelled', 'possibly_billed': True})
            raise
        except Exception as exc:
            metadata = asdict(classify_error(exc))
            diagnostics = getattr(exc, 'diagnostics', {})
            if isinstance(diagnostics, dict): metadata['diagnostics'] = diagnostics
            for field in ('finish_reason', 'completion_tokens', 'reasoning_tokens'):
                value = getattr(exc, field, None)
                if isinstance(value, (str, int)): metadata[field] = value
            metadata['elapsed_ms'] = (time.monotonic() - started) * 1000
            partial=getattr(exc,'partial_text','')
            output={'text':partial,'complete':False,'publishable':False} if isinstance(partial,str) and partial else None
            await self._failure(task_id, owner, 'awaiting_recovery', metadata, output)
            raise

    async def _failure(self, task_id, owner, status, metadata, output=None):
        row = await asyncio.to_thread(self.store.read, task_id)
        if row['status'] == 'running' and row['owner'] == owner:
            await self._finish(task_id, owner, status, metadata, output)

    async def _finish(self, *args):
        task = asyncio.create_task(asyncio.to_thread(self.store.finish, *args))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
