from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest

from astrbot_plugin_memos_memory.generation_v2.adapters import (
    AstrAdapter, DirectAdapter, CapabilityError, EmptyOutputError, classify_error)
from astrbot_plugin_memos_memory.generation_v2.store import TaskStore, TaskSpec, ConflictError
from astrbot_plugin_memos_memory.generation_v2.runner import SingleAttemptRunner
from astrbot_plugin_memos_memory.generation_v2.bridge import TaskBoundProvider
from astrbot_plugin_memos_memory.direct_llm import DirectHTTPError, DirectRequestError


class Provider:
    def __init__(self):
        self.calls = []
        self.error = None
        self.delay = 0
        self.text = 'valid draft'
        self.cancelled = False

    async def text_chat(self, *, prompt, contexts, system_prompt,
                        request_timeout=None, request_max_retries=None):
        self.calls.append((request_timeout, request_max_retries, asyncio.get_running_loop()))
        contexts.append({'role': 'user', 'content': 'mutated'})
        try:
            await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            return SimpleNamespace(completion_text=self.text)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_timeout_single_dispatch_loop_and_no_input_mutation(self):
        p = Provider()
        contexts = [{'role': 'user', 'content': 'original'}]
        result = await AstrAdapter(p).call(prompt='p', contexts=contexts, timeout=180)
        self.assertEqual(p.calls, [(180, 0, asyncio.get_running_loop())])
        self.assertEqual(len(contexts), 1)
        self.assertEqual(result.text, 'valid draft')
        self.assertIsNone(result.capabilities['first_token_ms'])

    async def test_kwargs_does_not_prove_capability(self):
        class Unknown:
            async def text_chat(self, **kwargs):
                raise AssertionError('must not dispatch')
        with self.assertRaises(CapabilityError):
            await AstrAdapter(Unknown()).call(prompt='p')

    async def test_timeout_cancels_provider_once(self):
        p = Provider()
        p.delay = 10
        with self.assertRaises(TimeoutError):
            await AstrAdapter(p).call(prompt='p', timeout=.01)
        self.assertTrue(p.cancelled)
        self.assertEqual(len(p.calls), 1)

    async def test_caller_cancel_is_propagated(self):
        p = Provider()
        p.delay = 10
        task = asyncio.create_task(AstrAdapter(p).call(prompt='p'))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(p.cancelled)

    async def test_empty_and_reasoning_only_fail(self):
        p = Provider()
        p.text = ' '
        with self.assertRaises(EmptyOutputError):
            await AstrAdapter(p).call(prompt='p')

    async def test_transport_not_retried(self):
        p = Provider()
        p.error = ConnectionError('secret upstream payload')
        with self.assertRaises(ConnectionError):
            await AstrAdapter(p).call(prompt='p')
        self.assertEqual(len(p.calls), 1)

    async def test_direct_parameters(self):
        calls = []
        class Direct:
            async def text_chat(self, *, prompt, contexts, system_prompt, timeout=None,
                                request_max_retries=None, max_tokens=0):
                calls.append((timeout, request_max_retries, max_tokens))
                return SimpleNamespace(completion_text='ok')
        await DirectAdapter(Direct()).call(prompt='p', timeout=180, max_tokens=8000)
        self.assertEqual(calls, [(180, 0, 8000)])

    def test_error_taxonomy(self):
        for exc, kind in [(DirectRequestError(401), 'authentication'),
                          (DirectRequestError(404), 'route_configuration'),
                          (DirectRequestError(400), 'invalid_request'),
                          (DirectHTTPError(503), 'overload'),
                          (DirectHTTPError(429, '12'), 'rate_limit'),
                          (TimeoutError(), 'timeout'), (ConnectionError(), 'transport'),
                          (EmptyOutputError(), 'empty_output')]:
            self.assertEqual(classify_error(exc).kind, kind)
        self.assertEqual(classify_error(DirectHTTPError(429, '12')).retry_after, 12)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'runtime.db'
        self.store = TaskStore(self.path)
        self.spec = TaskSpec('task', 'job', 'extract', 'private-session', 'p1', 's1', 'r1', time.time()+100)
        self.request = {'prompt': 'private text', 'contexts': [], 'system_prompt': 'role'}
        self.store.create(self.spec, self.request)
        self.route = hashlib.sha256(b'route').hexdigest()

    def test_idempotent_create_and_reopen(self):
        TaskStore(self.path).create(self.spec, self.request)
        self.assertEqual(self.store.read('task')['attempts'], 0)

    def test_changed_input_or_contract_rejected(self):
        with self.assertRaises(ConflictError):
            self.store.create(self.spec, dict(self.request, prompt='changed'))
        with self.assertRaises(ConflictError):
            self.store.create(replace(self.spec, schema_version='s2'), self.request)

    def test_secret_config_not_accepted(self):
        with self.assertRaises(ValueError):
            self.store.create(self.spec, dict(self.request, api_key='secret'))
        with self.assertRaises(ValueError):
            self.store.create(self.spec, dict(self.request, contexts=[{'role':'user','content':'x','token':'secret'}]))

    def test_atomic_claim(self):
        def claim(_):
            try:
                return self.store.claim('task', self.route)
            except ConflictError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(claim, range(6)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.store.read('task')['attempts'], 1)

    def test_stale_completion_blocked(self):
        self.store.claim('task', self.route)
        with self.assertRaises(ConflictError):
            self.store.finish('task', 'wrong', 'succeeded', {}, {'text':'x'})

    def test_success_and_artifact_atomic(self):
        owner = self.store.claim('task', self.route)
        self.store.finish('task', owner, 'succeeded', {}, {'text':'ok'})
        row = self.store.read('task')
        self.assertEqual(self.store.artifact(row['output_id']), {'text':'ok'})
        with self.assertRaises(ConflictError):
            self.store.finish('task', owner, 'succeeded', {}, {'text':'late'})

    def test_success_without_artifact_rolls_back(self):
        owner = self.store.claim('task', self.route)
        with self.assertRaises(ValueError):
            self.store.finish('task', owner, 'succeeded', {})
        self.assertEqual(self.store.read('task')['status'], 'running')

    def test_corrupt_artifact_detected(self):
        row = self.store.read('task')
        with self.store.connect() as db:
            db.execute("UPDATE artifacts SET body='{}'")
        with self.assertRaises(ConflictError):
            self.store.artifact(row['input_id'])

    def test_export_does_not_expose_input_or_scope(self):
        exported = json.dumps(self.store.export_metadata('task'))
        self.assertNotIn('private', exported)

    def test_existing_nonledger_db_untouched(self):
        other = Path(self.tmp.name) / 'source.db'
        with sqlite3.connect(other) as db:
            db.execute('CREATE TABLE source (id TEXT)')
        db.close()
        before = other.read_bytes()
        with self.assertRaises(ValueError):
            TaskStore(other)
        self.assertEqual(before, other.read_bytes())

    def test_unrelated_versioned_database_not_adopted(self):
        other = Path(self.tmp.name) / 'versioned.db'
        db = sqlite3.connect(other)
        db.execute('PRAGMA user_version=1')
        db.close()
        before = other.read_bytes()
        with self.assertRaises(ValueError):
            TaskStore(other)
        self.assertEqual(before, other.read_bytes())


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name) / 'runtime.db')
        spec = TaskSpec('t', 'j', 'extract', 'scope', 'p1', 's1', 'r1', time.time()+100)
        self.store.create(spec, {'prompt':'private','contexts':[], 'system_prompt':''})
        self.runner = SingleAttemptRunner(self.store)
        self.p = Provider()

    async def run_once(self, **kwargs):
        return await self.runner.run('t', AstrAdapter(self.p), route_identity='secret endpoint', **kwargs)

    async def test_success_reuse_no_second_call(self):
        a = await self.run_once()
        self.assertEqual(a, await self.run_once())
        self.assertEqual(len(self.p.calls), 1)
        self.assertNotIn('secret endpoint', json.dumps(self.store.export_metadata('t')))

    async def test_failure_saved_without_raw_error_or_retry(self):
        self.p.error = ConnectionError('secret error')
        with self.assertRaises(ConnectionError):
            await self.run_once()
        self.assertEqual(self.store.read('t')['status'], 'awaiting_recovery')
        self.assertNotIn('secret error', json.dumps(self.store.export_metadata('t')))
        with self.assertRaises(ConflictError):
            await self.run_once()
        self.assertEqual(len(self.p.calls), 1)

    async def test_cancel_persisted(self):
        self.p.delay = 10
        task = asyncio.create_task(self.run_once())
        while not self.p.calls:
            await asyncio.sleep(.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.read('t')['status'], 'cancelled')

    async def test_validation_rejected_retains_draft(self):
        self.assertIsNone(await self.run_once(validator=lambda text: False))
        row = self.store.read('t')
        self.assertEqual(row['status'], 'output_rejected')
        self.assertEqual(self.store.artifact(row['output_id'])['text'], 'valid draft')

    async def test_timeout_is_persisted(self):
        self.p.delay = 10
        with self.assertRaises(TimeoutError):
            await self.run_once(timeout=.01)
        report = self.store.export_metadata('t')
        self.assertEqual(json.loads(report['attempts'][0]['metadata'])['kind'], 'timeout')

    async def test_legacy_text_facade_preserves_input_identity(self):
        spec = TaskSpec('bridge', 'j', 'extract', 'scope', 'p1', 's1', 'r1', time.time()+100)
        facade = TaskBoundProvider(self.store, spec, AstrAdapter(self.p), route_identity='fixture')
        first = await facade.text_chat(prompt='text')
        second = await facade.text_chat(prompt='text')
        self.assertEqual(first.completion_text, second.completion_text)
        self.assertEqual(len(self.p.calls), 1)
        with self.assertRaises(ConflictError):
            await facade.text_chat(prompt='changed')
        with self.assertRaises(ValueError):
            await facade.text_chat(prompt='text', request_max_retries=3)


if __name__ == '__main__':
    unittest.main()
