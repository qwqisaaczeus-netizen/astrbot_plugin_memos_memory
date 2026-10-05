import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from test_generation_v2_test3 import archive
from test_generation_v2_test4 import Model, Remote
from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.coordinator import Coordinator
from astrbot_plugin_memos_memory.generation_v2.literary import DiaryStore
from astrbot_plugin_memos_memory.generation_v2.scheduler import Route, Scheduler
from astrbot_plugin_memos_memory.generation_v2.publishing import Publisher
from astrbot_plugin_memos_memory.generation_v2.store import ConflictError
from astrbot_plugin_memos_memory.generation_v2.workbench import overview
from astrbot_plugin_memos_memory.generation_v2.plugin_gateway import start_draft


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root/'source.db'
        archive(self.source)
        self.store = DiaryStore(root/'generation_runtime.db')
        self.service = Coordinator(self.store, Scheduler(self.store), self.source)
        self.addAsyncCleanup(self.service.close)
        self.model = Model()
        self.route = Route('model', 'astr', 'fixture', 'fixture', AstrAdapter(self.model))
        self.remote = Remote()
        self.pub = Publisher(self.store, self.remote, self.source)

    async def start(self, **kwargs):
        return await self.service.start('batch', 'scope', self.route, **kwargs)

    async def test_complete_draft_is_not_automatic_publication(self):
        before = self.source.read_bytes()
        job = await self.start()
        status = await self.service.wait(job)
        self.assertEqual(status['stage'], 'draft_ready')
        self.assertEqual(len(self.model.calls), 4)
        self.assertEqual(self.remote.calls, 0)
        self.assertEqual(before, self.source.read_bytes())

    async def test_duplicate_start_and_waiter_cancellation(self):
        job = await self.start()
        self.assertEqual(job, await self.start())
        waiter = asyncio.create_task(self.service.wait(job))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        self.assertEqual((await self.service.wait(job))['stage'], 'draft_ready')
        self.assertEqual(len(self.model.calls), 4)

    async def test_repeat_draft_no_new_calls(self):
        job = await self.start()
        await self.service.wait(job)
        await self.start()
        await self.service.wait(job)
        self.assertEqual(len(self.model.calls), 4)

    async def test_budget_reserves_review_before_first_call(self):
        with self.assertRaises(ConflictError):
            await self.start(job_cap=2)
        self.assertEqual(len(self.model.calls), 0)
        job = await self.start(job_cap=6)
        self.assertEqual((await self.service.wait(job))['stage'], 'draft_ready')

    async def test_plugin_entry_uses_explicit_model_on_same_loop(self):
        plugin = SimpleNamespace(runtime_state_dir=Path(self.temp.name),
            _episodes=SimpleNamespace(db_path=self.source), character_name='Fixture', rp_time_timezone='UTC',
            generation_v2_claim_audit=False,
            context=SimpleNamespace(get_provider_by_id=lambda name:self.model if name=='model' else None))
        result = await start_draft(plugin, {'confirm_model_calls':True,'batch_id':'batch','provider_id':'model'})
        service = plugin._generation_v2_service
        self.addAsyncCleanup(service.close)
        self.assertEqual((await service.wait(result['job_id']))['stage'], 'draft_ready')
        self.assertFalse(result['automatic_publication'])
        self.assertEqual(len(self.model.calls), 4)
        with service.store.connect() as db:
            policy=json.loads(db.execute('SELECT contract FROM writing_contracts WHERE job_id=?',(result['job_id'],)).fetchone()[0])
        self.assertEqual(policy['timezone'], 'UTC')

    async def test_plugin_entry_requires_explicit_cost_confirmation(self):
        with self.assertRaises(ValueError):
            await start_draft(SimpleNamespace(), {'batch_id':'batch','provider_id':'model'})
        self.assertEqual(len(self.model.calls), 0)

    async def test_changed_route_rejected_before_spending(self):
        job = await self.start()
        await self.service.wait(job)
        other = Route('other', 'astr', 'fixture', 'fixture', AstrAdapter(self.model))
        with self.assertRaises(ConflictError):
            await self.service.start('batch', 'scope', other)
        self.assertEqual(len(self.model.calls), 4)

    async def test_quality_failure_visible_and_preserves_original(self):
        self.model.bad_all = True
        before = self.source.read_bytes()
        job = await self.start()
        status = await self.service.wait(job)
        self.assertEqual(status['stage'], 'awaiting_recovery')
        self.assertEqual(status['error_kind'], 'SourceError')
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(self.remote.calls, 0)

    async def test_malformed_writer_artifact_not_marked_success(self):
        original = self.model.text_chat
        async def malformed(*, prompt, contexts, system_prompt, request_timeout=None, request_max_retries=None):
            if 'narratives' in json.loads(prompt):
                return SimpleNamespace(completion_text='{"drafts":[{"index":true}]}')
            return await original(prompt=prompt, contexts=contexts, system_prompt=system_prompt,
                                  request_timeout=request_timeout, request_max_retries=request_max_retries)
        self.model.text_chat = malformed
        job = await self.start()
        self.assertEqual((await self.service.wait(job))['stage'], 'awaiting_recovery')
        self.assertEqual(self.store.read(job+':write')['status'], 'output_rejected')
        self.assertEqual(self.remote.calls, 0)

    async def test_publish_index_state_retry_no_duplicate_write(self):
        job = await self.start()
        await self.service.wait(job)
        await self.service.publish(job, 'scope', self.pub)
        self.assertEqual(self.service.status(job)['stage'], 'state_pending')
        self.assertEqual(len(self.service.index.records('scope')), 2)
        self.assertEqual(self.service.index.records('other'), [])
        class StateSink:
            async def apply(inner, event_id, manifest):
                inner.event = event_id
        sink = StateSink()
        await self.service.publish(job, 'scope', self.pub, state_sink=sink)
        self.assertEqual(self.remote.calls, 1)
        self.assertEqual(self.service.status(job)['stage'], 'published')
        self.assertEqual(len(self.model.calls), 4)

    async def test_wrong_scope_blocks_publish(self):
        job = await self.start()
        await self.service.wait(job)
        with self.assertRaises(ConflictError):
            await self.service.publish(job, 'other', self.pub)
        self.assertEqual(self.remote.calls, 0)

    async def test_unknown_remote_receipt_recovered_without_model_or_create(self):
        job = await self.start()
        await self.service.wait(job)
        self.remote.lost = True
        with self.assertRaises(TimeoutError):
            await self.service.publish(job, 'scope', self.pub)
        self.remote.lost = False
        await self.service.publish(job, 'scope', self.pub)
        self.assertEqual(self.remote.calls, 1)
        self.assertEqual(len(self.model.calls), 4)

    async def test_restarted_workbench_does_not_claim_running(self):
        job = await self.start()
        await self.service.wait(job)
        self.service._stage(job, 'extracting')
        plugin = SimpleNamespace(runtime_state_dir=Path(self.temp.name))
        data = overview(plugin)
        self.assertEqual(data['jobs'][0]['status'], 'interrupted_pending_resume')

    async def test_close_refuses_further_dispatch(self):
        await self.service.close()
        with self.assertRaises(RuntimeError):
            await self.start()

    async def test_new_service_reuses_saved_results(self):
        job = await self.start()
        await self.service.wait(job)
        await self.service.close()
        second = Coordinator(self.store, Scheduler(self.store), self.source)
        self.addAsyncCleanup(second.close)
        await second.start('batch', 'scope', self.route)
        self.assertEqual((await second.wait(job))['stage'], 'draft_ready')
        self.assertEqual(len(self.model.calls), 4)
