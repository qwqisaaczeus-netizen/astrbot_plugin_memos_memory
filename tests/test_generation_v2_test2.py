import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest

from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
from astrbot_plugin_memos_memory.generation_v2.store import TaskSpec, ConflictError
from astrbot_plugin_memos_memory.generation_v2.scheduling_store import SchedulingStore
from astrbot_plugin_memos_memory.generation_v2.scheduler import Scheduler, Route
from astrbot_plugin_memos_memory.generation_v2.bridge import ScheduledTaskProvider
from astrbot_plugin_memos_memory.direct_llm import DirectHTTPError, DirectRequestError


class Provider:
    def __init__(self, errors=(), delay=0):
        self.errors = list(errors)
        self.calls = []
        self.delay = delay
        self.active = self.peak = 0

    async def text_chat(self, *, prompt, contexts, system_prompt, request_timeout=None, request_max_retries=None):
        self.calls.append((prompt, contexts, system_prompt, request_max_retries))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.errors:
                raise self.errors.pop(0)
            return SimpleNamespace(completion_text='accepted')
        finally:
            self.active -= 1


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'runtime.db'
        self.store = SchedulingStore(self.path)
        self.scheduler = Scheduler(self.store, workers=16, endpoint_limit=10, job_limit=6)
        self.addAsyncCleanup(self.scheduler.close)

    def create(self, tid='t', job='job', deadline=60, priority=0):
        self.store.create(TaskSpec(tid, job, 'extract', 'scope', 'p1', 's1', 'r1', time.time()+deadline,
                                  priority=priority), {'prompt':'source','contexts':[], 'system_prompt':'role'})

    def route(self, p, identity='primary', kind='astr', domain='a'):
        return Route(identity, kind, domain, domain, AstrAdapter(p))

    async def test_transport_retry_then_backup_exactly_three(self):
        self.create()
        p, b = Provider([ConnectionError(), ConnectionError()]), Provider()
        result = await self.scheduler.submit('t', self.route(p), self.route(b,'backup','direct','b'))
        self.assertEqual(result['text'], 'accepted')
        self.assertEqual((len(p.calls),len(b.calls)), (2,1))
        self.assertEqual(p.calls[0], b.calls[0])
        self.assertEqual(self.store.job('job')['used'], 3)

    async def test_timeout_skips_primary_retry(self):
        self.create()
        p, b = Provider([TimeoutError()]), Provider()
        await self.scheduler.submit('t', self.route(p), self.route(b,'backup','direct','b'))
        self.assertEqual((len(p.calls),len(b.calls)), (1,1))

    async def test_backup_never_retried(self):
        self.create()
        p, b = Provider([TimeoutError()]), Provider([ConnectionError()])
        result = await self.scheduler.submit('t', self.route(p), self.route(b,'backup','direct','b'))
        self.assertEqual(result['status'], 'awaiting_recovery')
        self.assertEqual(len(b.calls), 1)

    async def test_policy_digest_is_preserved_and_failed_backup_is_not_retried(self):
        self.create()
        p,b=Provider([TimeoutError()]),Provider([ConnectionError()])
        primary,backup=self.route(p),self.route(b,'backup','direct','b')
        primary.adapter.policy_digest='custom-primary-policy'
        backup.adapter.policy_digest='custom-backup-policy'
        result=await self.scheduler.submit('t',primary,backup)
        self.assertEqual(result['status'],'awaiting_recovery')
        self.assertEqual((len(p.calls),len(b.calls)),(1,1))
        self.assertEqual([a['route_id'] for a in self.store.attempts_for('t')],
                         [primary.digest,backup.digest])

    async def test_auth_no_primary_retry(self):
        self.create()
        p, b = Provider([DirectRequestError(401)]), Provider()
        await self.scheduler.submit('t', self.route(p), self.route(b,'backup','direct','b'))
        self.assertEqual(len(p.calls), 1)

    async def test_same_domain_rate_limit_is_paused_not_retried(self):
        self.create()
        p, b = Provider([DirectHTTPError(429, '20')]), Provider()
        await self.scheduler.submit('t', self.route(p), self.route(b,'backup','direct','a'))
        self.assertEqual(len(b.calls), 0)
        meta = json.loads(self.store.attempts_for('t')[0]['metadata'])
        self.assertEqual(meta['retry_after'], 20)

    async def test_separate_domain_rate_limit_can_followup(self):
        self.create()
        p, b = Provider([DirectHTTPError(429, '20')]), Provider()
        await self.scheduler.submit('t', self.route(p), self.route(b,'backup','direct','b'))
        self.assertEqual(len(b.calls), 1)

    async def test_rejected_draft_not_network_retry(self):
        self.create()
        p, b = Provider(), Provider()
        result = await self.scheduler.submit('t', self.route(p), self.route(b,'backup','direct','b'), validator=lambda _:False)
        self.assertIsNone(result)
        self.assertEqual(self.store.read('t')['status'], 'output_rejected')
        self.assertEqual(len(b.calls), 0)

    async def test_duplicate_clicks_share_attempt(self):
        self.create()
        p = Provider(delay=.03)
        route = self.route(p)
        results = await asyncio.gather(*(self.scheduler.submit('t',route) for _ in range(12)))
        self.assertEqual(len(p.calls), 1)
        self.assertTrue(all(r['text']=='accepted' for r in results))

    async def test_checkpoints_survive_reopen(self):
        self.create()
        p = Provider()
        route = self.route(p)
        await self.scheduler.submit('t',route)
        other = Scheduler(SchedulingStore(self.path))
        try:
            await other.submit('t',route)
        finally:
            await other.close()
        self.assertEqual(len(p.calls),1)

    async def test_job_budget_shared_across_stages(self):
        p = Provider()
        route = self.route(p)
        for i in range(8):
            self.create(str(i))
        await asyncio.gather(*(self.scheduler.submit(str(i),route) for i in range(8)))
        self.assertEqual(len(p.calls),6)
        self.assertEqual(self.store.job('job')['used'],6)

    async def test_astr_limit_six(self):
        p=Provider(delay=.08)
        for i in range(14):
            self.create(str(i),str(i))
        await asyncio.gather(*(self.scheduler.submit(str(i),self.route(p)) for i in range(14)))
        self.assertLessEqual(p.peak,6)
        self.assertGreater(p.peak,1)

    async def test_direct_limit_ten(self):
        p=Provider(delay=.08)
        for i in range(14):
            self.create(str(i),str(i))
        await asyncio.gather(*(self.scheduler.submit(str(i),self.route(p,kind='direct')) for i in range(14)))
        self.assertLessEqual(p.peak,10)
        self.assertGreater(p.peak,1)

    async def test_cancel_no_followup_and_no_live_children(self):
        self.create()
        p,b=Provider(delay=10),Provider()
        waiter=asyncio.create_task(self.scheduler.submit('t', self.route(p),self.route(b,'backup','direct','b')))
        while not p.calls:
            await asyncio.sleep(.002)
        await self.scheduler.cancel('t')
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(p.active,0)
        self.assertEqual(len(b.calls),0)
        self.assertEqual(self.store.read('t')['status'],'cancelled')

    async def test_waiter_disconnect_does_not_cancel_background(self):
        self.create()
        p=Provider(delay=.05)
        waiter=asyncio.create_task(self.scheduler.submit('t',self.route(p)))
        while not p.calls:
            await asyncio.sleep(.002)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        while self.scheduler.active:
            await asyncio.sleep(.01)
        self.assertEqual(self.store.read('t')['status'],'succeeded')

    async def test_route_mutation_rejected(self):
        self.create()
        await self.scheduler.submit('t',self.route(Provider()))
        with self.assertRaises(ConflictError):
            await self.scheduler.submit('t',self.route(Provider(),'changed'))

    async def test_expired_inflight_unknown_not_replayed(self):
        self.create()
        p=Provider()
        self.store.attach('t',{'fixture':1})
        self.store.acquire('t','crashed')
        self.store.claim('t',self.route(p).digest)
        self.store.recover_expired(time.time()+1000)
        self.assertEqual(self.store.read('t')['status'],'outcome_unknown')
        self.assertEqual(self.store.job('job')['used'],1)
        with self.assertRaises(ConflictError):
            self.store.claim('t',self.route(p).digest)

    async def test_import_old_compensation_readonly_idempotent(self):
        path=Path(self.tmp.name)/'old.db'
        db=sqlite3.connect(path)
        db.executescript("""CREATE TABLE failed_llm_requests(id TEXT,source_batch_id TEXT,task TEXT,status TEXT);
        INSERT INTO failed_llm_requests VALUES('a','batch','memory','failed');
        INSERT INTO failed_llm_requests VALUES('b','batch','memory','pending');
        INSERT INTO failed_llm_requests VALUES('c','done','memory','restored');""")
        db.close()
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        first=self.store.import_legacy(path)
        self.assertEqual(first,self.store.import_legacy(path))
        self.assertEqual(sum(r['count'] for r in first),3)
        self.assertTrue(any(r['count']==2 for r in first))
        self.assertEqual(before,hashlib.sha256(path.read_bytes()).hexdigest())

    async def test_no_background_workers_after_close(self):
        self.create()
        p=Provider(delay=10)
        waiter=asyncio.create_task(self.scheduler.submit('t',self.route(p)))
        while not p.calls:
            await asyncio.sleep(.002)
        await self.scheduler.close()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(all(t.done() for t in self.scheduler.workers))
        self.assertEqual(p.active,0)

    async def test_scheduled_text_bridge(self):
        spec=TaskSpec('bridge','j','extract','s','p','v','r',time.time()+60)
        p,b=Provider([TimeoutError()]),Provider()
        provider=ScheduledTaskProvider(self.scheduler,spec,self.route(p),self.route(b,'backup','direct','b'))
        result=await provider.text_chat(prompt='same source')
        self.assertEqual(result.completion_text,'accepted')
        self.assertEqual(len(b.calls),1)

    async def test_cancel_job_drains_all_children(self):
        p=Provider(delay=10)
        for i in range(4):
            self.create(str(i))
        waiters=[asyncio.create_task(self.scheduler.submit(str(i),self.route(p))) for i in range(4)]
        while len(self.scheduler.active)<4:
            await asyncio.sleep(.01)
        await self.scheduler.cancel_job('job')
        await asyncio.gather(*waiters,return_exceptions=True)
        self.assertEqual(p.active,0)
        self.assertTrue(all(self.store.read(str(i))['status']=='cancelled' for i in range(4)))

    async def test_restart_continues_remaining_route_not_fresh_primary(self):
        self.create()
        p,b=Provider(),Provider()
        primary,backup=self.route(p),self.route(b,'backup','direct','b')
        self.store.attach('t',{'primary':primary.digest,'backup':backup.digest,'timeout':180,'same_fault_domain':False})
        owner=self.store.claim('t',primary.digest)
        self.store.finish('t',owner,'awaiting_recovery',{'kind':'timeout'})
        result=await self.scheduler.resume_registered({primary.digest:primary,backup.digest:backup}, validators={'s1':lambda _:True})
        self.assertEqual(result[0]['text'],'accepted')
        self.assertEqual(len(p.calls),0)
        self.assertEqual(len(b.calls),1)
        self.assertEqual(self.store.job('job')['used'],2)

    async def test_endpoint_and_job_caps(self):
        other=Scheduler(self.store,workers=8,endpoint_limit=2,job_limit=1)
        p=Provider(delay=.04)
        try:
            for i in range(4):
                self.create(str(i))
            await asyncio.gather(*(other.submit(str(i),self.route(p)) for i in range(4)))
            self.assertEqual(p.peak,1)
        finally:
            await other.close()

    async def test_queue_bound_rejects_excess_without_dispatch(self):
        other=Scheduler(self.store,workers=1,queue_size=1)
        p=Provider(delay=10)
        for i in range(3):
            self.create(str(i),str(i))
        a=asyncio.create_task(other.submit('0',self.route(p)))
        while not p.calls:
            await asyncio.sleep(.002)
        b=asyncio.create_task(other.submit('1',self.route(p)))
        while not other.queue:
            await asyncio.sleep(.002)
        with self.assertRaises(RuntimeError):
            await other.submit('2',self.route(p))
        await other.close()
        await asyncio.gather(a,b,return_exceptions=True)
        self.assertEqual(len(p.calls),1)

    async def test_cross_scheduler_owner_not_stolen(self):
        self.create()
        p=Provider(delay=.1)
        route=self.route(p)
        a=asyncio.create_task(self.scheduler.submit('t',route))
        while not p.calls:
            await asyncio.sleep(.002)
        other=Scheduler(self.store)
        try:
            with self.assertRaises(ConflictError):
                await asyncio.wait_for(other.submit('t',route),2)
            await asyncio.wait_for(a,2)
        finally:
            await asyncio.wait_for(other.close(),2)
        self.assertEqual(len(p.calls),1)
