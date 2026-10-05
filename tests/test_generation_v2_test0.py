from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from astrbot_plugin_memos_memory.generation_v2.contracts import (
    AttemptBudget, effective_timeout, job_key, publish_eligible, recovery_action,
)
from astrbot_plugin_memos_memory.generation_v2.preflight import (
    compensation_inventory, inspect_database, readonly, snapshot,
)
from astrbot_plugin_memos_memory.external_models import ExternalModelRegistry


class ContractTests(unittest.TestCase):
    def test_explicit_deadline_is_not_capped_by_model_default(self):
        self.assertEqual(effective_timeout(180, 60), 180)
        self.assertEqual(effective_timeout(None, 60), 60)

    def test_invalid_budgets_are_rejected(self):
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_timeout(value, 60)
        with self.assertRaises(ValueError):
            AttemptBudget(60, 4)

    def test_budget_is_remaining_not_reset(self):
        self.assertEqual(AttemptBudget(90).remaining(10, 60), 40)
        self.assertEqual(AttemptBudget(90).remaining(10, 200), 0)

    def test_job_identity_preserves_real_repeated_messages(self):
        self.assertEqual(job_key('s', ['t1'], 'v1'), job_key('s', ['t1'], 'v1'))
        self.assertNotEqual(job_key('s', ['t1'], 'v1'), job_key('s', ['t2'], 'v1'))
        self.assertNotEqual(job_key('s', ['t1'], 'v1'), job_key('other', ['t1'], 'v1'))
        with self.assertRaises(ValueError):
            job_key('s', ['t1', 't1'], 'v1')

    def test_local_fallback_and_stale_revision_cannot_publish(self):
        args = dict(grounded=True, draft_valid=True, source_revision='r1', expected_revision='r1')
        self.assertFalse(publish_eligible(extraction='local_grounded_recovery', **args))
        self.assertTrue(publish_eligible(extraction='llm_primary', **args))
        args['expected_revision'] = 'r2'
        self.assertFalse(publish_eligible(extraction='llm_primary', **args))

    def test_unknown_write_requires_reconciliation(self):
        self.assertEqual(recovery_action('write', artifact_valid=True, publish_status='outcome_unknown'),
                         'reconcile_before_create')
        self.assertEqual(recovery_action('extract', artifact_valid=True, publish_status='pending'),
                         'reuse_artifact')
        self.assertEqual(recovery_action('index', artifact_valid=False, publish_status='pending'),
                         'resume_index')


class MigrationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'source.db'
        self.db = sqlite3.connect(self.path)
        self.db.executescript('''
            CREATE TABLE source_batches(batch_id TEXT PRIMARY KEY,message_count INTEGER,status TEXT);
            CREATE TABLE source_turns(batch_id TEXT,turn_index INTEGER,content TEXT,content_hash TEXT);
            CREATE TABLE episodes(episode_id TEXT,memo_name TEXT,source_batch_id TEXT,
                scene_start_turn INTEGER,scene_end_turn INTEGER,active INTEGER DEFAULT 1);
            INSERT INTO source_batches VALUES('batch',2,'committed');
            INSERT INTO episodes VALUES('ep','memos/e','batch',0,1,1);
        ''')
        for index, body in enumerate(['private source user', 'private source assistant']):
            self.db.execute('INSERT INTO source_turns VALUES(?,?,?,?)',
                            ('batch', index, body, hashlib.sha256(body.encode()).hexdigest()))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_complete_archive_is_not_claimed_semantically_verified(self):
        before = self.path.read_bytes()
        report = inspect_database(self.path)
        self.assertEqual(report['summary'], {'source_complete': 1})
        self.assertFalse(report['episodes'][0]['evidence_semantics_verified'])
        self.assertNotIn('private source', json.dumps(report))
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_turn_is_partial(self):
        self.db.execute('DELETE FROM source_turns WHERE turn_index=1')
        self.db.commit()
        self.assertEqual(inspect_database(self.path)['summary'], {'source_partial': 1})

    def test_changed_content_hash_is_partial(self):
        self.db.execute("UPDATE source_turns SET content='changed' WHERE turn_index=1")
        self.db.commit()
        self.assertEqual(inspect_database(self.path)['summary'], {'source_partial': 1})

    def test_missing_range_is_partial(self):
        self.db.execute('UPDATE episodes SET scene_start_turn=-1')
        self.db.commit()
        self.assertEqual(inspect_database(self.path)['summary'], {'source_partial': 1})

    def test_missing_batch_is_not_diary_only(self):
        self.db.execute('DELETE FROM source_batches')
        self.db.commit()
        self.assertEqual(inspect_database(self.path)['summary'], {'broken_link': 1})

    def test_diary_only_remains_diary_derived(self):
        self.db.execute("UPDATE episodes SET source_batch_id='' ")
        self.db.commit()
        self.assertEqual(inspect_database(self.path)['summary'], {'diary_only': 1})

    def test_inactive_not_counted_for_upgrade(self):
        self.db.execute('UPDATE episodes SET active=0')
        self.db.commit()
        self.assertEqual(inspect_database(self.path)['summary'], {'inactive': 1})

    def test_unknown_database_fails_closed(self):
        self.db.execute('DROP TABLE episodes')
        self.db.commit()
        self.assertEqual(inspect_database(self.path)['status'], 'unsupported_schema')

    def test_reader_cannot_write(self):
        reader = readonly(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute('DELETE FROM episodes')
        finally:
            reader.close()

    def test_snapshot_includes_wal_and_refuses_overwrite(self):
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute("INSERT INTO episodes VALUES('ep2','memos/t','',-1,-1,1)")
        self.db.commit()
        target = Path(self.tmp.name) / 'copy.db'
        snapshot(self.path, target)
        self.assertEqual(inspect_database(target)['summary'], {'diary_only': 1, 'source_complete': 1})
        with self.assertRaises(ValueError):
            snapshot(self.path, target)
        with self.assertRaises(ValueError):
            snapshot(self.path, self.path)

    def test_duplicate_compensation_is_inventoried_without_reading_payload(self):
        self.db.executescript('''
            CREATE TABLE failed_llm_requests(id TEXT,task TEXT,label TEXT,source_batch_id TEXT,status TEXT,payload TEXT);
            INSERT INTO failed_llm_requests VALUES('1','memory_generation','extract','batch','failed','secret');
            INSERT INTO failed_llm_requests VALUES('2','memory_generation','extract','batch','pending','secret');
        ''')
        report = compensation_inventory(self.path)
        self.assertEqual(report['duplicate_pending_batches'], {'batch': 2})
        self.assertNotIn('secret', json.dumps(report))


class LegacyFailureReproduction(unittest.IsolatedAsyncioTestCase):
    async def test_followup_preserves_explicit_180_seconds(self):
        # Test0 reproduced 60; test1 fixes this specific legacy boundary.
        with tempfile.TemporaryDirectory() as tmp:
            registry = ExternalModelRegistry(Path(tmp))
            calls = []
            async def text_chat(**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(completion_text='fixture')
            registry.model = lambda _: {'id': 'test', 'enabled': True,
                                       'timeout_seconds': 60, 'max_output_tokens': 8192}
            registry._client = lambda _: SimpleNamespace(text_chat=text_chat)
            await registry.single_text_chat('test', prompt='fixture', contexts=[],
                system_prompt='', timeout=180, max_tokens=100)
            self.assertEqual(calls[0]['timeout'], 180)


if __name__ == '__main__':
    unittest.main()
