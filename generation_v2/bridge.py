"""Explicit task-bound compatibility entry; no implicit task ID or route choice."""
import asyncio
from types import SimpleNamespace

from .runner import SingleAttemptRunner


class TaskBoundProvider:
    """text_chat facade for plugin-owned text tasks, not main-chat/tool requests.

    The caller owns task identity, deadline, provider and validator. This facade
    never enables V2 globally and never attempts automatic recovery.
    """

    def __init__(self, store, spec, adapter, *, route_identity, validator=None):
        self.store = store
        self.spec = spec
        self.adapter = adapter
        self.route_identity = route_identity
        self.validator = validator

    async def text_chat(self, *, prompt, contexts=None, system_prompt='',
                        timeout=180, request_max_retries=0):
        if request_max_retries not in (0, 1, None):
            raise ValueError('task-bound provider permits one dispatch only')
        request = {'prompt': prompt, 'contexts': contexts or [], 'system_prompt': system_prompt}
        # Creation is idempotent and safe even if caller cancellation races it.
        await asyncio.to_thread(self.store.create, self.spec, request)
        result = await SingleAttemptRunner(self.store).run(
            self.spec.task_id, self.adapter, route_identity=self.route_identity,
            timeout=timeout, validator=self.validator)
        if result is None:
            raise ValueError('task output rejected; draft retained')
        return SimpleNamespace(completion_text=result['text'],
                               task_id=self.spec.task_id, raw_completion=None)


class ScheduledTaskProvider:
    """Explicit V2 entry. Never wrap this in the legacy retry/follow-up loop."""

    def __init__(self, scheduler, spec, primary, backup=None, *, validator=None, job_cap=6):
        self.scheduler, self.spec = scheduler, spec
        self.primary, self.backup = primary, backup
        self.validator, self.job_cap = validator, job_cap

    async def text_chat(self, *, prompt, contexts=None, system_prompt='', timeout=180,
                        request_max_retries=0):
        if request_max_retries not in (0, 1, None):
            raise ValueError('retry ownership belongs to scheduler')
        await asyncio.to_thread(self.scheduler.store.create, self.spec,
                                {'prompt':prompt, 'contexts':contexts or [], 'system_prompt':system_prompt})
        result = await self.scheduler.submit(self.spec.task_id, self.primary, self.backup,
                                            timeout=timeout, job_cap=self.job_cap, validator=self.validator)
        if not result or 'text' not in result:
            raise RuntimeError('task retained for recovery; no publishable result')
        return SimpleNamespace(completion_text=result['text'], task_id=self.spec.task_id, raw_completion=None)
