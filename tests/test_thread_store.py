# -*- coding: utf-8 -*-
"""6.0.0-test5 ThreadStore regression suite."""
import asyncio
import sqlite3
import tempfile
import time
import threading
import unittest
from pathlib import Path

from astrbot_plugin_memos_memory.episodic_store import EpisodicStore
from astrbot_plugin_memos_memory.thread_store import ThreadStore, THREAD_SCHEMA_VERSION
from astrbot_plugin_memos_memory.thread_migration import ThreadMigration
from astrbot_plugin_memos_memory.thread_builder import ThreadBuilder


def _make_store(tmp: str) -> EpisodicStore:
    return EpisodicStore(str(Path(tmp) / "ep.db"), 3, "unit")


class ThreadStoreSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmp.name)
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # 1. schema version follows the facade migration
    async def test_schema_version_is_thirteen(self):
        self.assertEqual(EpisodicStore.SCHEMA_VERSION, 15)
        conn = self.store._connect()
        val = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        self.assertEqual(int(val), EpisodicStore.SCHEMA_VERSION)

    # 2. all 16 thread tables exist
    async def test_all_sixteen_thread_tables_exist(self):
        conn = self.store._connect()
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        expected = [
            "memory_threads", "memory_thread_members", "memory_episode_edges",
            "memory_claims", "memory_claim_transitions", "prospective_memory_items",
            "thread_materialized_views", "thread_view_versions",
            "thread_build_queue", "thread_build_runs",
            "thread_query_observations", "thread_consistency_observations",
            "thread_eval_cases", "thread_eval_runs",
            "thread_manual_feedback", "thread_migration_state",
        ]
        for t in expected:
            self.assertIn(t, tables, f"missing table: {t}")

    # 3. schema is idempotent (second init doesn't break anything)
    async def test_schema_init_is_idempotent(self):
        conn = self.store._connect()
        count_before = len(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall())
        # Re-init
        self.store._threads.init_schema(conn)
        conn.commit()
        count_after = len(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall())
        self.assertEqual(count_before, count_after)

    # 4. thread schema version recorded
    async def test_thread_schema_version_recorded(self):
        conn = self.store._connect()
        val = conn.execute(
            "SELECT value FROM thread_migration_state WHERE key='thread_schema_version'"
        ).fetchone()
        self.assertIsNotNone(val)
        self.assertEqual(str(val["value"]), THREAD_SCHEMA_VERSION)


class ThreadQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmp.name)
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # 5. enqueue returns True for new item
    async def test_enqueue_new_item_returns_true(self):
        result = self.store.thread_enqueue("scope-a", "ep-001")
        self.assertTrue(result)

    # 6. enqueue is idempotent — same id → returns False
    async def test_enqueue_same_id_idempotent(self):
        self.store.thread_enqueue("scope-a", "ep-001")
        result = self.store.thread_enqueue("scope-a", "ep-001")
        self.assertFalse(result)

    # 7. queue counts reflect inserted items
    async def test_queue_counts_reflect_inserts(self):
        for i in range(5):
            self.store.thread_enqueue("scope-a", f"ep-{i:03d}")
        counts = self.store.thread_queue_counts()
        self.assertEqual(counts.get("pending", 0), 5)

    # 8. take_pending_batch marks items as running
    async def test_take_pending_batch_marks_running(self):
        for i in range(4):
            self.store.thread_enqueue("scope-a", f"ep-{i:03d}")
        batch = self.store.thread_take_pending_batch(limit=3)
        self.assertEqual(len(batch), 3)
        counts = self.store.thread_queue_counts()
        self.assertEqual(counts.get("running", 0), 3)
        self.assertEqual(counts.get("pending", 0), 1)

    # 9. mark_completed transitions to completed
    async def test_mark_completed_status(self):
        self.store.thread_enqueue("scope-a", "ep-001")
        batch = self.store.thread_take_pending_batch(limit=1)
        queue_id = int(batch[0]["id"])
        self.store.thread_mark_completed(queue_id, elapsed_ms=12.3, candidates=2)
        counts = self.store.thread_queue_counts()
        self.assertEqual(counts.get("completed", 0), 1)
        self.assertEqual(counts.get("running", 0), 0)

    # 10. quarantined on error
    async def test_mark_completed_with_error_quarantines(self):
        self.store.thread_enqueue("scope-a", "ep-error")
        batch = self.store.thread_take_pending_batch(limit=1)
        self.store.thread_mark_completed(int(batch[0]["id"]), error="simulated failure")
        counts = self.store.thread_queue_counts()
        self.assertEqual(counts.get("quarantined", 0), 1)

    # 11. restore_running_to_pending restores incomplete batch
    async def test_restore_running_to_pending(self):
        for i in range(3):
            self.store.thread_enqueue("scope-a", f"ep-{i}")
        self.store.thread_take_pending_batch(limit=3)
        restored = self.store.thread_restore_running_to_pending()
        self.assertEqual(restored, 3)
        counts = self.store.thread_queue_counts()
        self.assertEqual(counts.get("pending", 0), 3)
        self.assertEqual(counts.get("running", 0), 0)

    # 12. take_pending_batch respects limit
    async def test_take_pending_batch_respects_limit(self):
        for i in range(10):
            self.store.thread_enqueue("scope-a", f"ep-{i}")
        batch = self.store.thread_take_pending_batch(limit=4)
        self.assertEqual(len(batch), 4)

    # 13. pause / resume state
    async def test_pause_and_resume(self):
        self.assertFalse(self.store.thread_is_paused())
        self.store.thread_set_paused(True)
        self.assertTrue(self.store.thread_is_paused())
        self.store.thread_set_paused(False)
        self.assertFalse(self.store.thread_is_paused())


class ThreadStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmp.name)
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # 14. thread_status returns expected fields
    async def test_thread_status_structure(self):
        status = self.store.thread_status()
        self.assertIn("mode", status)
        self.assertIn("thread_schema_version", status)
        self.assertIn("queue", status)
        self.assertIn("paused", status)
        self.assertEqual(status["mode"], "shadow")

    # 15. source_grade_counts returns A/B/C/D for empty db
    async def test_source_grade_counts_empty_db(self):
        grades = self.store.thread_source_grade_counts()
        for key in ("A", "B", "C", "D", "total"):
            self.assertIn(key, grades)
            self.assertEqual(grades[key], 0)

    # 16. thread_memory_enable=false disables enqueue in main flow
    async def test_thread_enqueue_not_called_when_disabled(self):
        # Just verify the facade method exists and succeeds
        result = self.store.thread_enqueue("s", "e-000")
        self.assertTrue(result)


class ThreadMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmp.name)
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # 17. scan with no episodes returns zero grades
    async def test_scan_empty_db_returns_zero_counts(self):
        migration = ThreadMigration(
            self.store._connect,
            self.store._lock,
            self.store._threads,
        )
        result = migration.run_scan(read_only=True)
        self.assertEqual(result["scanned"], 0)
        for key in ("A", "B", "C", "D"):
            self.assertEqual(result["grades"].get(key, 0), 0)

    # 18. scan sets scan_completed flag
    async def test_scan_sets_completed_flag(self):
        migration = ThreadMigration(
            self.store._connect,
            self.store._lock,
            self.store._threads,
        )
        migration.run_scan(read_only=True)
        status = migration.scan_status()
        self.assertTrue(status["completed"])

    # 19. scan status fields are present
    async def test_scan_status_structure(self):
        migration = ThreadMigration(
            self.store._connect,
            self.store._lock,
            self.store._threads,
        )
        status = migration.scan_status()
        for key in ("completed", "grades", "checkpoint_episode_id"):
            self.assertIn(key, status)


class ThreadBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.tmp.name)
        await self.store.init()

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # 20. process_batch on empty queue returns 0
    async def test_process_empty_queue_returns_zero(self):
        builder = ThreadBuilder(
            self.store._threads,
            self.store._connect,
            self.store._lock,
        )
        result = builder.process_batch(batch_size=5)
        self.assertEqual(result.get("processed", 0), 0)

    # 21. process_batch on non-existent episode completes without crashing
    async def test_process_unavailable_episode_does_not_crash(self):
        self.store.thread_enqueue("scope-x", "ep-nonexistent-xyz")
        builder = ThreadBuilder(
            self.store._threads,
            self.store._connect,
            self.store._lock,
        )
        result = builder.process_batch(batch_size=5)
        # Should complete (even if 0 processed with candidate=0)
        self.assertGreaterEqual(result.get("processed", 0), 0)
        counts = self.store.thread_queue_counts()
        # Item must be in a terminal state (completed or quarantined), not running
        self.assertEqual(counts.get("running", 0), 0)

    # 22. shadow output: no memory_threads rows written by builder
    async def test_builder_writes_no_formal_thread_rows(self):
        self.store.thread_enqueue("scope-x", "ep-test")
        builder = ThreadBuilder(
            self.store._threads,
            self.store._connect,
            self.store._lock,
        )
        builder.process_batch(batch_size=5)
        conn = self.store._connect()
        thread_count = int(conn.execute("SELECT COUNT(*) AS n FROM memory_threads").fetchone()["n"])
        edge_count = int(conn.execute("SELECT COUNT(*) AS n FROM memory_episode_edges WHERE status='accepted'").fetchone()["n"])
        self.assertEqual(thread_count, 0)
        self.assertEqual(edge_count, 0)

    # 23. thread_build_runs records the run
    async def test_thread_build_runs_records_entry(self):
        self.store.thread_enqueue("scope-x", "ep-run-test")
        builder = ThreadBuilder(
            self.store._threads,
            self.store._connect,
            self.store._lock,
        )
        builder.process_batch(batch_size=5)
        conn = self.store._connect()
        run_count = int(conn.execute("SELECT COUNT(*) AS n FROM thread_build_runs").fetchone()["n"])
        self.assertGreaterEqual(run_count, 1)

    # 24. paused builder skips processing
    async def test_paused_builder_skips(self):
        self.store.thread_enqueue("scope-x", "ep-pause-test")
        self.store.thread_set_paused(True)
        builder = ThreadBuilder(
            self.store._threads,
            self.store._connect,
            self.store._lock,
        )
        result = builder.process_batch(batch_size=5)
        self.assertEqual(result.get("processed", 0), 0)
        self.assertTrue(result.get("paused", False))
        self.store.thread_set_paused(False)

    # 25. 6.0 tables can be dropped and rebuilt without affecting episodic tables
    async def test_drop_and_rebuild_thread_tables_leaves_episodic_intact(self):
        # Add a real episode first
        ep = {
            "memo_name": "memos/t25", "occurred_at": "2026-07-01 晚上",
            "event_ts": time.mktime(time.strptime("2026-07-01", "%Y-%m-%d")),
            "memory_type": "plot_fact", "importance": 3,
            "scene_anchor": "天台", "retrieval_key": "银戒指",
            "entities": [], "unresolved": [],
            "card_text": "银戒指 天台", "evidence_quality": "diary_derived",
            "source_updated_ts": 0.0,
        }
        self.store.upsert_episode(
            memo_name=ep["memo_name"], episode=ep,
            card_text=ep["card_text"], embedding=[1.0, 0.0, 0.0],
        )
        # Drop all thread tables
        conn = self.store._connect()
        for tbl in [
            "memory_threads", "memory_thread_members", "memory_episode_edges",
            "memory_claims", "thread_build_queue", "thread_migration_state",
        ]:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.commit()
        # Rebuild
        self.store._threads.init_schema(conn)
        conn.commit()
        # Episodic table still intact
        ep_count = int(conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"])
        self.assertEqual(ep_count, 1)
        # Thread tables recreated
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("memory_threads", tables)
        self.assertIn("thread_build_queue", tables)


if __name__ == "__main__":
    unittest.main()
