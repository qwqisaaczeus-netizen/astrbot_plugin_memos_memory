"""Persistent derived store for the 6.0 memory-thread shadow pipeline.

All 16 tables in this module are local derived data.  Deleting them and
calling init_schema() a second time completely reconstructs the layer from
the existing episodes, source_turns and diaries. Source data is never
modified by this module.

test5 adds an evidence-aware claim ledger, prospective memory, and retrieval
lab observations on top of the test2 thread projection. Episodes, diaries, and
source turns remain the source of truth and are never modified here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import threading
import uuid
import sqlite3
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable


def _atomic_write(method):
    """Serialize read/modify/write across connections and roll back failures."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            conn = self._get_conn()
            if conn.in_transaction:
                raise RuntimeError("thread mutation requires a clean transaction boundary")
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = method(self, *args, **kwargs)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
    return wrapped

logger = logging.getLogger(__name__)

THREAD_SCHEMA_VERSION = "6.0.0-test10"
BUILDER_VERSION = "test5-v2"
ARBITRATION_PROMPT_VERSION = "test2-arbiter-v1"

# --------------------------------------------------------------------------- #
#  helpers                                                                      #
# --------------------------------------------------------------------------- #

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(str(value))
        return parsed if fallback is None or isinstance(parsed, type(fallback)) else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


# --------------------------------------------------------------------------- #
#  ThreadStore                                                                  #
# --------------------------------------------------------------------------- #

class ThreadVersionConflict(RuntimeError):
    """Raised when a mutation was based on a stale materialized version."""

    def __init__(self, message: str, *, thread_id: str = "", current_version: int | None = None):
        super().__init__(message)
        self.thread_id = str(thread_id)
        self.current_version = current_version


class ThreadStore:
    """Schema owner and thin query layer for all 6.0 derived tables."""

    def __init__(self, get_conn: Callable[[], Any], lock: threading.RLock):
        self._get_conn = get_conn
        self._lock = lock
        self._owner = uuid.uuid4().hex
        self._claims: dict[tuple[str, int], str] = {}

    # ----------------------------------------------------------------------- #
    #  schema                                                                   #
    # ----------------------------------------------------------------------- #

    def init_schema(self, conn: Any) -> None:
        """Create and migrate the derived thread schema idempotently."""

        # ── narrative thread registry ─────────────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_threads (
            thread_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT '',
            thread_type TEXT NOT NULL DEFAULT 'other',
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 0.5,
            first_event_ts REAL NOT NULL DEFAULT 0,
            last_event_ts REAL NOT NULL DEFAULT 0,
            materialized_version INTEGER NOT NULL DEFAULT 0,
            source_quality_floor TEXT NOT NULL DEFAULT 'diary_derived',
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0
        )""")

        self._ensure_column(conn, "memory_threads", "manual_lock", "INTEGER NOT NULL DEFAULT 0")

        conn.execute("""CREATE TABLE IF NOT EXISTS memory_thread_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'supporting',
            sequence_no INTEGER NOT NULL DEFAULT 0,
            membership_confidence REAL NOT NULL DEFAULT 0.5,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            decision_source TEXT NOT NULL DEFAULT 'rule',
            manual_lock INTEGER NOT NULL DEFAULT 0,
            created_ts REAL NOT NULL DEFAULT 0,
            UNIQUE(thread_id, episode_id),
            FOREIGN KEY(thread_id) REFERENCES memory_threads(thread_id) ON DELETE CASCADE
        )""")

        # ── episode relationship edges ────────────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_episode_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_episode_id TEXT NOT NULL,
            target_episode_id TEXT NOT NULL,
            edge_type TEXT NOT NULL DEFAULT 'parallel',
            direction TEXT NOT NULL DEFAULT 'directed',
            confidence REAL NOT NULL DEFAULT 0.5,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            counter_evidence_json TEXT NOT NULL DEFAULT '[]',
            route_sources_json TEXT NOT NULL DEFAULT '[]',
            arbiter_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'provisional',
            manual_lock INTEGER NOT NULL DEFAULT 0,
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0,
            UNIQUE(source_episode_id, target_episode_id, edge_type)
        )""")
        self._ensure_column(conn, "memory_episode_edges", "scope_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "memory_episode_edges", "decision_source", "TEXT NOT NULL DEFAULT 'rule'")
        self._ensure_column(conn, "memory_episode_edges", "decision_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column(conn, "memory_episode_edges", "content_hash", "TEXT NOT NULL DEFAULT ''")

        # ── atomic claims and state ledger ───────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_claims (
            claim_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            predicate TEXT NOT NULL DEFAULT '',
            object TEXT NOT NULL DEFAULT '',
            claim_type TEXT NOT NULL DEFAULT 'belief',
            valid_from REAL NOT NULL DEFAULT 0,
            valid_to REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 0.5,
            source_episode_id TEXT NOT NULL DEFAULT '',
            source_turn_refs_json TEXT NOT NULL DEFAULT '[]',
            source_quality TEXT NOT NULL DEFAULT 'diary_derived',
            manual_lock INTEGER NOT NULL DEFAULT 0,
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0
        )""")
        for column, definition in (
            ("slot_key", "TEXT NOT NULL DEFAULT ''"),
            ("explicitness", "REAL NOT NULL DEFAULT 0.5"),
            ("is_hypothetical", "INTEGER NOT NULL DEFAULT 0"),
            ("is_dream", "INTEGER NOT NULL DEFAULT 0"),
            ("evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("content_hash", "TEXT NOT NULL DEFAULT ''"),
            ("extractor_version", "TEXT NOT NULL DEFAULT ''"),
            ("decision_source", "TEXT NOT NULL DEFAULT 'rule'"),
        ):
            self._ensure_column(conn, "memory_claims", column, definition)

        conn.execute("""CREATE TABLE IF NOT EXISTS memory_claim_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_claim_id TEXT NOT NULL DEFAULT '',
            to_claim_id TEXT NOT NULL DEFAULT '',
            transition_type TEXT NOT NULL DEFAULT 'supersedes',
            reason TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '',
            decided_by TEXT NOT NULL DEFAULT 'rule',
            confidence REAL NOT NULL DEFAULT 0.5,
            created_ts REAL NOT NULL DEFAULT 0
        )""")
        self._ensure_column(conn, "memory_claim_transitions", "transition_key", "TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_claim_slots (
            scope_id TEXT NOT NULL DEFAULT '',
            slot_key TEXT NOT NULL DEFAULT '',
            current_claim_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'uncertain',
            view_text TEXT NOT NULL DEFAULT '',
            conflict_count INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0,
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(scope_id,slot_key)
        )""")

        # ── prospective memory ───────────────────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS prospective_memory_items (
            item_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT '',
            source_episode_id TEXT NOT NULL DEFAULT '',
            source_thread_id TEXT NOT NULL DEFAULT '',
            item_type TEXT NOT NULL DEFAULT 'commitment',
            description TEXT NOT NULL DEFAULT '',
            trigger_mode TEXT NOT NULL DEFAULT 'semantic',
            due_start REAL NOT NULL DEFAULT 0,
            due_end REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            salience REAL NOT NULL DEFAULT 0.5,
            explicitness REAL NOT NULL DEFAULT 0.5,
            emotional_weight REAL NOT NULL DEFAULT 0.0,
            cooldown_until REAL NOT NULL DEFAULT 0,
            last_surfaced_ts REAL NOT NULL DEFAULT 0,
            resolution_evidence_json TEXT NOT NULL DEFAULT '{}',
            manual_status TEXT NOT NULL DEFAULT '',
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0
        )""")
        for column, definition in (
            ("source_claim_id", "TEXT NOT NULL DEFAULT ''"),
            ("target_entities_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("trigger_terms_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("last_trigger_routes_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("surfaced_count", "INTEGER NOT NULL DEFAULT 0"),
            ("negative_feedback_count", "INTEGER NOT NULL DEFAULT 0"),
            ("status_reason", "TEXT NOT NULL DEFAULT ''"),
            ("content_hash", "TEXT NOT NULL DEFAULT ''"),
            ("surface_reservation", "TEXT NOT NULL DEFAULT ''"),
            ("surface_reservation_until", "REAL NOT NULL DEFAULT 0"),
        ):
            self._ensure_column(conn, "prospective_memory_items", column, definition)
        conn.execute("""CREATE TABLE IF NOT EXISTS prospective_trigger_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL DEFAULT '',
            scope_id TEXT NOT NULL DEFAULT '',
            query_text TEXT NOT NULL DEFAULT '',
            item_id TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT 'not_triggered',
            score REAL NOT NULL DEFAULT 0,
            routes_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            shadow INTEGER NOT NULL DEFAULT 1,
            feedback TEXT NOT NULL DEFAULT '',
            created_ts REAL NOT NULL DEFAULT 0
        )""")

        # ── thread materialized views ─────────────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_materialized_views (
            thread_id TEXT PRIMARY KEY,
            view_version INTEGER NOT NULL DEFAULT 0,
            active_facts_json TEXT NOT NULL DEFAULT '[]',
            recent_transitions_json TEXT NOT NULL DEFAULT '[]',
            open_loops_json TEXT NOT NULL DEFAULT '[]',
            active_promises_json TEXT NOT NULL DEFAULT '[]',
            counter_evidence_json TEXT NOT NULL DEFAULT '[]',
            source_episode_ids_json TEXT NOT NULL DEFAULT '[]',
            generator_version TEXT NOT NULL DEFAULT '',
            coverage REAL NOT NULL DEFAULT 0.0,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(thread_id) REFERENCES memory_threads(thread_id) ON DELETE CASCADE
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS thread_view_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            view_version INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_ts REAL NOT NULL DEFAULT 0,
            UNIQUE(thread_id, view_version)
        )""")

        # ── build queue and runs ─────────────────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_build_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL DEFAULT '',
            episode_id TEXT NOT NULL DEFAULT '',
            builder_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            next_attempt_ts REAL NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0,
            UNIQUE(scope_id, episode_id, builder_version)
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS thread_build_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL DEFAULT '',
            scope_id TEXT NOT NULL DEFAULT '',
            episode_id TEXT NOT NULL DEFAULT '',
            builder_version TEXT NOT NULL DEFAULT '',
            elapsed_ms REAL NOT NULL DEFAULT 0,
            candidates_found INTEGER NOT NULL DEFAULT 0,
            edges_accepted INTEGER NOT NULL DEFAULT 0,
            edges_rejected INTEGER NOT NULL DEFAULT 0,
            edges_ambiguous INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'completed',
            created_ts REAL NOT NULL DEFAULT 0
        )""")

        # ── query and consistency observations ───────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_query_observations (
            request_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT '',
            thread_ids_json TEXT NOT NULL DEFAULT '[]',
            subgraph_json TEXT NOT NULL DEFAULT '{}',
            injected_claims_json TEXT NOT NULL DEFAULT '[]',
            injected_prospective_json TEXT NOT NULL DEFAULT '[]',
            shadow INTEGER NOT NULL DEFAULT 1,
            created_ts REAL NOT NULL DEFAULT 0
        )""")
        for column, definition in (
            ("query_text", "TEXT NOT NULL DEFAULT ''"),
            ("evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("query_plan_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("route_results_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("dedup_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("injection_preview", "TEXT NOT NULL DEFAULT ''"),
            ("metrics_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("latency_ms", "REAL NOT NULL DEFAULT 0"),
        ):
            self._ensure_column(conn, "thread_query_observations", column, definition)

        conn.execute("""CREATE TABLE IF NOT EXISTS thread_consistency_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL DEFAULT '',
            scope_id TEXT NOT NULL DEFAULT '',
            error_type TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.5,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            severity TEXT NOT NULL DEFAULT 'low',
            created_ts REAL NOT NULL DEFAULT 0,
            UNIQUE(request_id, error_type, evidence_json)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_request_observations (
            request_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT '',
            query_text TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            extra_parts_json TEXT NOT NULL DEFAULT '[]',
            extra_parts_hash TEXT NOT NULL DEFAULT '',
            extra_parts_chars INTEGER NOT NULL DEFAULT 0,
            hit_order_json TEXT NOT NULL DEFAULT '[]',
            append_status TEXT NOT NULL DEFAULT '',
            answer_hash TEXT NOT NULL DEFAULT '',
            answer_chars INTEGER NOT NULL DEFAULT 0,
            answer_preview TEXT NOT NULL DEFAULT '',
            chunk_status TEXT NOT NULL DEFAULT '',
            response_status TEXT NOT NULL DEFAULT '',
            consistency_status TEXT NOT NULL DEFAULT '',
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0
        )""")
        for column, definition in (
            ("observation_status", "TEXT NOT NULL DEFAULT ''"),
            ("skip_reason", "TEXT NOT NULL DEFAULT ''"),
            ("snapshot_complete", "INTEGER NOT NULL DEFAULT 0"),
            ("thread_used", "INTEGER NOT NULL DEFAULT 0"),
            ("response_truncated", "INTEGER NOT NULL DEFAULT 0"),
            ("inspected_chars", "INTEGER NOT NULL DEFAULT 0"),
            ("terminal", "INTEGER NOT NULL DEFAULT 0"),
        ):
            self._ensure_column(conn, "thread_request_observations", column, definition)
        for column, definition in (
            ("rule_id", "TEXT NOT NULL DEFAULT ''"),
            ("decision_source", "TEXT NOT NULL DEFAULT ''"),
            ("answer_excerpt", "TEXT NOT NULL DEFAULT ''"),
            ("response_excerpt", "TEXT NOT NULL DEFAULT ''"),
            ("source_quality", "TEXT NOT NULL DEFAULT ''"),
            ("guard_version", "TEXT NOT NULL DEFAULT ''"),
        ):
            self._ensure_column(conn, "thread_consistency_observations", column, definition)
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_consistency_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            operator TEXT NOT NULL DEFAULT 'webui',
            created_ts REAL NOT NULL DEFAULT 0
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_consistency_feedback_request ON thread_consistency_feedback(request_id,created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_request_obs_updated ON thread_request_observations(updated_ts DESC)")

        # ── evaluation tables ────────────────────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_eval_cases (
            case_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT '',
            case_type TEXT NOT NULL DEFAULT 'general',
            query TEXT NOT NULL DEFAULT '',
            expected_thread_ids_json TEXT NOT NULL DEFAULT '[]',
            expected_claims_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT '',
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0
        )""")
        for column, definition in (
            ("expected_episode_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("expected_prospective_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("expected_order_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("source", "TEXT NOT NULL DEFAULT 'manual'"),
            ("confidence", "REAL NOT NULL DEFAULT 1.0"),
        ):
            self._ensure_column(conn, "thread_eval_cases", column, definition)

        conn.execute("""CREATE TABLE IF NOT EXISTS thread_eval_runs (
            run_id TEXT PRIMARY KEY,
            algorithm_version TEXT NOT NULL DEFAULT '',
            config_json TEXT NOT NULL DEFAULT '{}',
            results_json TEXT NOT NULL DEFAULT '{}',
            cases_total INTEGER NOT NULL DEFAULT 0,
            cases_passed INTEGER NOT NULL DEFAULT 0,
            started_ts REAL NOT NULL DEFAULT 0,
            finished_ts REAL NOT NULL DEFAULT 0
        )""")

        # ── manual feedback ──────────────────────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_manual_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_id TEXT NOT NULL DEFAULT '',
            target_type TEXT NOT NULL DEFAULT 'thread',
            target_id TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT 'confirm',
            note TEXT NOT NULL DEFAULT '',
            operator TEXT NOT NULL DEFAULT 'webui',
            reversible INTEGER NOT NULL DEFAULT 1,
            created_ts REAL NOT NULL DEFAULT 0
        )""")

        # ── migration state ──────────────────────────────────────────────── #
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_migration_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_ts REAL NOT NULL DEFAULT 0
        )""")

        conn.execute("""CREATE TABLE IF NOT EXISTS thread_episode_scopes (
            episode_id TEXT NOT NULL,
            scope_id TEXT NOT NULL DEFAULT '',
            assignment_source TEXT NOT NULL DEFAULT 'queue',
            confidence REAL NOT NULL DEFAULT 1.0,
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(episode_id, scope_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_episode_terms (
            episode_id TEXT NOT NULL,
            term TEXT NOT NULL,
            PRIMARY KEY(episode_id, term)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_episode_term_state (
            episode_id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL DEFAULT '',
            updated_ts REAL NOT NULL DEFAULT 0
        )""")
        # Keep the test9 compatibility projection during rolling upgrades. The
        # test10 postings are authoritative; these tables let older diagnostics
        # and interrupted upgrades resume without forcing a full rescan.
        self._ensure_column(
            conn, "thread_episode_term_state", "entity_hash",
            "TEXT NOT NULL DEFAULT ''",
        )
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_episode_entities (
            episode_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            PRIMARY KEY(episode_id, entity)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_episode_index_queue (
            episode_id TEXT PRIMARY KEY,
            source_updated_ts REAL NOT NULL DEFAULT 0,
            queued_ts REAL NOT NULL DEFAULT 0
        )""")
        # test10's thread_candidate_dirty queue supersedes the duplicate test9
        # index queue. Remove old triggers during upgrade so each source change
        # creates one durable work item, while retaining the empty table for old
        # diagnostic tools that introspect the schema.
        for trigger in (
            "trg_thread_episode_index_insert",
            "trg_thread_episode_index_update",
            "trg_thread_episode_index_delete",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DELETE FROM thread_episode_index_queue")
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_arbitration_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            edge_id INTEGER NOT NULL UNIQUE,
            scope_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_ts REAL NOT NULL DEFAULT 0,
            cache_key TEXT NOT NULL DEFAULT '',
            provider_id TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL DEFAULT '',
            input_hash TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            decision_json TEXT NOT NULL DEFAULT '{}',
            created_ts REAL NOT NULL DEFAULT 0,
            updated_ts REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(edge_id) REFERENCES memory_episode_edges(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_arbitration_cache (
            cache_key TEXT PRIMARY KEY,
            decision_json TEXT NOT NULL DEFAULT '{}',
            provider_id TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            created_ts REAL NOT NULL DEFAULT 0,
            last_used_ts REAL NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS thread_operations (
            operation_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL DEFAULT '',
            operation_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'applied',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            operator TEXT NOT NULL DEFAULT 'webui',
            created_ts REAL NOT NULL DEFAULT 0,
            reverted_ts REAL NOT NULL DEFAULT 0
        )""")

        # ── indices ──────────────────────────────────────────────────────── #
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_members_episode ON memory_thread_members(episode_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_members_thread ON memory_thread_members(thread_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_edges_source ON memory_episode_edges(source_episode_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_edges_target ON memory_episode_edges(target_episode_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_edges_scope ON memory_episode_edges(scope_id, status, confidence DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_scope ON memory_claims(scope_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_claims_slot ON memory_claims(scope_id,slot_key,status,valid_from DESC)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_transition_key ON memory_claim_transitions(transition_key) WHERE transition_key!=''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prospective_scope ON prospective_memory_items(scope_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prospective_due ON prospective_memory_items(scope_id,status,due_start,due_end)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prospective_observations ON prospective_trigger_observations(scope_id,created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_build_queue_status ON thread_build_queue(status, next_attempt_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_build_queue_episode ON thread_build_queue(scope_id, episode_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_build_runs_episode ON thread_build_runs(scope_id, episode_id, created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_episode_scope ON thread_episode_scopes(scope_id, episode_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_episode_terms_term ON thread_episode_terms(term,episode_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_arb_jobs_status ON thread_arbitration_jobs(status, next_attempt_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_ops_scope ON thread_operations(scope_id, created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_query_scope ON thread_query_observations(scope_id,created_ts DESC)")

        for table in ("thread_build_queue", "thread_arbitration_jobs"):
            self._ensure_column(conn, table, "claim_token", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, table, "claim_owner", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, table, "lease_until", "REAL NOT NULL DEFAULT 0")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_lease ON {table}(status,lease_until)")
        self._ensure_column(conn, "thread_build_queue", "input_hash", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "thread_build_queue", "rerun_requested", "INTEGER NOT NULL DEFAULT 0")
        schema = str(conn.execute("SELECT sql FROM sqlite_master WHERE name='memory_episode_edges'").fetchone()[0])
        if "UNIQUE(source_episode_id, target_episode_id, edge_type)" in schema:
            conn.execute(schema.replace("memory_episode_edges", "thread_edges_upgrade", 1).replace(
                "UNIQUE(source_episode_id, target_episode_id, edge_type)",
                "UNIQUE(scope_id, source_episode_id, target_episode_id, edge_type)"))
            conn.execute("INSERT INTO thread_edges_upgrade SELECT * FROM memory_episode_edges")
            jobs = [dict(r) for r in conn.execute("SELECT * FROM thread_arbitration_jobs")]
            conn.execute("DROP TABLE memory_episode_edges")
            conn.execute("ALTER TABLE thread_edges_upgrade RENAME TO memory_episode_edges")
            for job in jobs:
                columns = list(job)
                conn.execute(f"INSERT OR IGNORE INTO thread_arbitration_jobs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(job.values()))
        for name, columns in (("source", "source_episode_id"), ("target", "target_episode_id"),
                              ("scope", "scope_id,status,confidence DESC"),
                              ("pair", "scope_id,source_episode_id,target_episode_id"),
                              ("scope_created", "scope_id,created_ts DESC")):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_episode_edges_{name} ON memory_episode_edges({columns})")
        for name, table, columns in (
            ("threads_recent", "memory_threads", "last_event_ts DESC,updated_ts DESC"),
            ("threads_scope_recent", "memory_threads", "scope_id,last_event_ts DESC,updated_ts DESC"),
            ("claims_source", "memory_claims", "source_episode_id"),
            ("claim_slots_recent", "memory_claim_slots", "scope_id,updated_ts DESC"),
            ("edges_retention", "memory_episode_edges", "status,manual_lock,updated_ts"),
        ):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{name} ON {table}({columns})")
        for table in ("thread_query_observations", "thread_consistency_observations", "thread_build_runs", "prospective_trigger_observations"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_retention ON {table}(created_ts)")
        if self._get(conn, "test10_manual_thread_locks", "0") != "1":
            for operation in conn.execute("SELECT after_json FROM thread_operations WHERE status='applied' AND operation_type IN ('merge','split')"):
                payload = _loads(operation[0], {})
                for thread in payload.get('threads', []):
                    if isinstance(thread, dict) and thread.get('thread_id'):
                        conn.execute("UPDATE memory_threads SET manual_lock=1 WHERE thread_id=?", (str(thread['thread_id']),))
            self._set(conn, "test10_manual_thread_locks", "1")
        from .thread_candidates import ThreadCandidates
        ThreadCandidates.init_schema(conn)
        self._set(conn, "thread_schema_version", THREAD_SCHEMA_VERSION)

    @staticmethod
    def _ensure_column(conn: Any, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        # ----------------------------------------------------------------------- #
        #  meta helpers                                                             #
        # ----------------------------------------------------------------------- #

    def record_request_observation(self, request_id: str, **fields: Any) -> None:
        """Idempotently upsert the request snapshot and response metadata."""
        request_id = str(request_id or "").strip()
        if not request_id:
            return
        allowed = {
            "scope_id", "query_text", "evidence_json", "extra_parts_json",
            "extra_parts_hash", "extra_parts_chars", "hit_order_json",
            "append_status", "answer_hash", "answer_chars", "answer_preview",
            "chunk_status", "response_status", "observation_status", "skip_reason",
            "snapshot_complete", "thread_used", "consistency_status",
            "response_truncated", "inspected_chars", "terminal",
        }
        values = {key: fields[key] for key in allowed if key in fields}
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT request_id,terminal FROM thread_request_observations WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row:
                existing_terminal = int(row["terminal"] or 0)
                if "terminal" in values:
                    try:
                        incoming_terminal = int(bool(int(values["terminal"])))
                    except (TypeError, ValueError):
                        incoming_terminal = 0
                    values["terminal"] = max(existing_terminal, incoming_terminal)
                elif existing_terminal:
                    values["terminal"] = existing_terminal
                if values:
                    assignments = ",".join(f"{key}=?" for key in values)
                    conn.execute(
                        f"UPDATE thread_request_observations SET {assignments},updated_ts=? WHERE request_id=?",
                        (*values.values(), now, request_id),
                    )
                else:
                    conn.execute("UPDATE thread_request_observations SET updated_ts=? WHERE request_id=?", (now, request_id))
            else:
                columns = ["request_id", *values, "created_ts", "updated_ts"]
                params = [request_id, *values.values(), now, now]
                conn.execute(
                    f"INSERT INTO thread_request_observations({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(params),
                )
            conn.commit()

    def request_observation(self, request_id: str) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT * FROM thread_request_observations WHERE request_id=?", (str(request_id),)
        ).fetchone()
        return dict(row) if row else None

    def list_request_observations(self, *, request_id: str = "", scope_id: str = "",
                                  observation_status: str = "", consistency_status: str = "",
                                  skip_reason: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        for column, value in (("request_id", request_id), ("scope_id", scope_id),
                              ("observation_status", observation_status),
                              ("consistency_status", consistency_status),
                              ("skip_reason", skip_reason)):
            if value:
                clauses.append(column + "=?")
                params.append(str(value))
        with self._lock:
            rows = self._get_conn().execute(
                f"SELECT * FROM thread_request_observations WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_ts DESC,request_id LIMIT ?",
                (*params, max(1, min(1000, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def consistency_request_overview(
        self, *, request_id: str = "", scope_id: str = "",
        observation_status: str = "", consistency_status: str = "",
        skip_reason: str = "", severity: str = "", error_type: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return one locally reviewable row per request plus database-level totals."""
        clauses = ["1=1"]
        params: list[Any] = []
        for column, value in (("r.request_id", request_id), ("r.scope_id", scope_id),
                              ("r.observation_status", observation_status),
                              ("r.consistency_status", consistency_status),
                              ("r.skip_reason", skip_reason)):
            if value:
                clauses.append(column + "=?")
                params.append(str(value))
        if severity:
            clauses.append(
                "EXISTS(SELECT 1 FROM thread_consistency_observations f "
                "WHERE f.request_id=r.request_id AND f.severity=?)"
            )
            params.append(str(severity))
        if error_type:
            clauses.append(
                "EXISTS(SELECT 1 FROM thread_consistency_observations f "
                "WHERE f.request_id=r.request_id AND f.error_type=?)"
            )
            params.append(str(error_type))
        where = " AND ".join(clauses)
        row_limit = max(1, min(1000, int(limit)))
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                f"""SELECT r.*,
                       (SELECT COUNT(*) FROM thread_consistency_observations f
                        WHERE f.request_id=r.request_id) AS finding_count,
                       (SELECT COUNT(*) FROM thread_consistency_feedback b
                        WHERE b.request_id=r.request_id) AS feedback_count,
                       (SELECT b.label FROM thread_consistency_feedback b
                        WHERE b.request_id=r.request_id
                        ORDER BY b.created_ts DESC,b.id DESC LIMIT 1) AS latest_feedback
                    FROM thread_request_observations r
                    WHERE {where}
                    ORDER BY r.updated_ts DESC,r.request_id LIMIT ?""",
                (*params, row_limit),
            ).fetchall()
            status_rows = conn.execute(
                f"SELECT COALESCE(NULLIF(r.consistency_status,''),'pending') AS key,COUNT(*) AS count "
                f"FROM thread_request_observations r WHERE {where} GROUP BY key",
                tuple(params),
            ).fetchall()
            observation_rows = conn.execute(
                f"SELECT COALESCE(NULLIF(r.observation_status,''),'pending') AS key,COUNT(*) AS count "
                f"FROM thread_request_observations r WHERE {where} GROUP BY key",
                tuple(params),
            ).fetchall()
            skip_rows = conn.execute(
                f"SELECT r.skip_reason AS key,COUNT(*) AS count FROM thread_request_observations r "
                f"WHERE {where} AND r.skip_reason!='' GROUP BY r.skip_reason",
                tuple(params),
            ).fetchall()
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM thread_request_observations r WHERE {where}",
                tuple(params),
            ).fetchone()[0])
            finding_summary = conn.execute(
                f"""SELECT COUNT(*) AS findings,
                       COALESCE(SUM(CASE WHEN f.severity='critical' THEN 1 ELSE 0 END),0) AS critical,
                       COALESCE(SUM(CASE WHEN f.severity='high' THEN 1 ELSE 0 END),0) AS high,
                       COALESCE(SUM(CASE WHEN f.severity='medium' THEN 1 ELSE 0 END),0) AS medium,
                       COALESCE(SUM(CASE WHEN f.severity='low' THEN 1 ELSE 0 END),0) AS low
                    FROM thread_consistency_observations f
                    WHERE f.request_id IN (
                        SELECT r.request_id FROM thread_request_observations r WHERE {where}
                    )""",
                tuple(params),
            ).fetchone()
            feedback_rows = conn.execute(
                f"""SELECT b.label AS key,COUNT(*) AS count
                    FROM thread_consistency_feedback b
                    WHERE b.request_id IN (
                        SELECT r.request_id FROM thread_request_observations r WHERE {where}
                    ) GROUP BY b.label""",
                tuple(params),
            ).fetchall()
        finding_data = dict(finding_summary) if finding_summary else {}
        return {
            "items": [dict(row) for row in rows],
            "summary": {
                "requests": total,
                "status": {str(row["key"]): int(row["count"]) for row in status_rows},
                "observation": {str(row["key"]): int(row["count"]) for row in observation_rows},
                "skip_reason": {str(row["key"]): int(row["count"]) for row in skip_rows},
                "findings": int(finding_data.get("findings") or 0),
                "severity": {key: int(finding_data.get(key) or 0)
                             for key in ("critical", "high", "medium", "low")},
                "feedback": {str(row["key"]): int(row["count"]) for row in feedback_rows},
            },
        }

    def consistency_detail(self, request_id: str) -> dict[str, Any]:
        """Read request metadata, findings, and feedback under one store lock."""
        clean_id = str(request_id or "").strip()
        with self._lock:
            conn = self._get_conn()
            observation = conn.execute(
                "SELECT * FROM thread_request_observations WHERE request_id=?", (clean_id,),
            ).fetchone()
            findings = conn.execute(
                "SELECT * FROM thread_consistency_observations WHERE request_id=? "
                "ORDER BY created_ts DESC,id DESC LIMIT 100", (clean_id,),
            ).fetchall()
            feedback = conn.execute(
                "SELECT * FROM thread_consistency_feedback WHERE request_id=? "
                "ORDER BY created_ts DESC,id DESC LIMIT 100", (clean_id,),
            ).fetchall()
        finding_items = []
        for row in findings:
            item = dict(row)
            item["evidence"] = _loads(item.pop("evidence_json", "{}"), {})
            finding_items.append(item)
        return {
            "observation": dict(observation) if observation else None,
            "findings": finding_items,
            "feedback": [dict(row) for row in feedback],
        }

    def record_thread_consistency_observations(self, request_id: str,
                                               observations: list[dict[str, Any]], *,
                                               scope_id: str = "default") -> int:
        """Persist unique consistency findings without changing source data."""
        rows = [item for item in (observations or []) if isinstance(item, dict)]
        written = 0
        with self._lock:
            conn = self._get_conn()
            now = time.time()
            for item in rows:
                error_type = str(item.get("error_type") or "")[:100]
                if not error_type:
                    continue
                evidence = _json(item.get("evidence") or {})
                raw_confidence = item.get("confidence")
                try:
                    confidence = 0.5 if raw_confidence is None else max(0.0, min(1.0, float(raw_confidence)))
                except (TypeError, ValueError):
                    confidence = 0.5
                exists = conn.execute(
                    "SELECT id FROM thread_consistency_observations WHERE request_id=? AND error_type=? AND evidence_json=?",
                    (str(request_id), error_type, evidence),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """INSERT INTO thread_consistency_observations
                       (request_id,scope_id,error_type,description,confidence,evidence_json,severity,
                        rule_id,decision_source,answer_excerpt,response_excerpt,source_quality,guard_version,created_ts)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(request_id), str(scope_id or "default"), error_type,
                     str(item.get("description") or "")[:2000],
                     confidence, evidence,
                     str(item.get("severity") or "low"),
                     str(item.get("rule_id") or "")[:100],
                     str(item.get("decision_source") or "rule")[:100],
                     str(item.get("answer_excerpt") or "")[:500],
                     str(item.get("response_excerpt") or "")[:500],
                     str(item.get("source_quality") or "")[:100],
                     str(item.get("guard_version") or "")[:100], now),
                )
                written += 1
            conn.commit()
        return written

    def record_consistency_result(self, request_id: str,
                                  observations: list[dict[str, Any]], *,
                                  scope_id: str = "default", **fields: Any) -> int:
        """Commit a terminal request state and all of its findings atomically."""
        clean_id = str(request_id or "").strip()
        if not clean_id:
            return 0
        allowed = {
            "scope_id", "query_text", "evidence_json", "extra_parts_json",
            "extra_parts_hash", "extra_parts_chars", "hit_order_json",
            "append_status", "answer_hash", "answer_chars", "answer_preview",
            "chunk_status", "response_status", "observation_status", "skip_reason",
            "snapshot_complete", "thread_used", "consistency_status",
            "response_truncated", "inspected_chars", "terminal",
        }
        values = {key: fields[key] for key in allowed if key in fields}
        if "scope_id" in values:
            values["scope_id"] = str(values.get("scope_id") or scope_id or "default")
        values["terminal"] = 1
        rows = [item for item in (observations or []) if isinstance(item, dict)]
        now = time.time()
        written = 0
        with self._lock:
            conn = self._get_conn()
            existing = conn.execute(
                "SELECT request_id,scope_id FROM thread_request_observations WHERE request_id=?",
                (clean_id,),
            ).fetchone()
            existing_scope = str(existing["scope_id"] or "") if existing else ""
            result_scope = str(
                values.get("scope_id")
                or (scope_id if scope_id and scope_id != "default" else "")
                or existing_scope
                or scope_id
                or "default"
            )
            if not existing or result_scope != existing_scope:
                values["scope_id"] = result_scope
            if existing:
                assignments = ",".join(f"{key}=?" for key in values)
                conn.execute(
                    f"UPDATE thread_request_observations SET {assignments},updated_ts=? WHERE request_id=?",
                    (*values.values(), now, clean_id),
                )
            else:
                columns = ["request_id", *values, "created_ts", "updated_ts"]
                conn.execute(
                    f"INSERT INTO thread_request_observations({','.join(columns)}) "
                    f"VALUES({','.join('?' for _ in columns)})",
                    (clean_id, *values.values(), now, now),
                )
            for item in rows:
                error_type = str(item.get("error_type") or "")[:100]
                if not error_type:
                    continue
                evidence = _json(item.get("evidence") or {})
                raw_confidence = item.get("confidence")
                try:
                    confidence = 0.5 if raw_confidence is None else max(
                        0.0, min(1.0, float(raw_confidence))
                    )
                except (TypeError, ValueError):
                    confidence = 0.5
                cur = conn.execute(
                    """INSERT OR IGNORE INTO thread_consistency_observations
                       (request_id,scope_id,error_type,description,confidence,evidence_json,severity,
                        rule_id,decision_source,answer_excerpt,response_excerpt,source_quality,guard_version,created_ts)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (clean_id, result_scope, error_type,
                     str(item.get("description") or "")[:2000], confidence, evidence,
                     str(item.get("severity") or "low")[:40],
                     str(item.get("rule_id") or "")[:100],
                     str(item.get("decision_source") or "rule")[:100],
                     str(item.get("answer_excerpt") or "")[:500],
                     str(item.get("response_excerpt") or "")[:500],
                     str(item.get("source_quality") or "")[:100],
                     str(item.get("guard_version") or "")[:100], now),
                )
                written += max(0, int(cur.rowcount or 0))
            conn.commit()
        return written

    def list_thread_consistency_observations(
        self, *, request_id: str = "", scope_id: str = "", severity: str = "",
        error_type: str = "", observation_status: str = "", consistency_status: str = "",
        skip_reason: str = "", limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        for column, value in (("f.request_id", request_id), ("f.scope_id", scope_id),
                              ("f.severity", severity), ("f.error_type", error_type),
                              ("r.observation_status", observation_status),
                              ("r.consistency_status", consistency_status),
                              ("r.skip_reason", skip_reason)):
            if value:
                clauses.append(f"{column}=?")
                params.append(str(value))
        rows = self._get_conn().execute(
            f"SELECT f.*, r.observation_status, r.consistency_status, r.skip_reason "
            f"FROM thread_consistency_observations f "
            f"LEFT JOIN thread_request_observations r ON r.request_id=f.request_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY f.created_ts DESC,f.id DESC LIMIT ?",
            (*params, max(1, min(500, int(limit)))),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = _loads(item.pop("evidence_json", "{}"), {})
            output.append(item)
        return output

    def record_consistency_feedback(self, request_id: str, label: str, *, note: str = "", operator: str = "webui") -> int:
        labels = {"true_error", "false_positive", "uncertain", "irrelevant"}
        clean_request = str(request_id or "").strip()
        clean_label = str(label or "").strip()
        if not clean_request:
            raise ValueError("request_id is required")
        if clean_label not in labels:
            raise ValueError("invalid consistency feedback label")
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT request_id FROM thread_request_observations WHERE request_id=?", (clean_request,)).fetchone()
            if not row:
                raise KeyError("request observation not found")
            cur = conn.execute(
                "INSERT INTO thread_consistency_feedback(request_id,label,note,operator,created_ts) VALUES(?,?,?,?,?)",
                (clean_request, clean_label, str(note or "")[:1000], str(operator or "webui")[:100], time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_consistency_feedback(self, request_id: str = "", *, limit: int = 100) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if request_id:
            clauses.append("request_id=?")
            params.append(str(request_id))
        rows = self._get_conn().execute(
            f"SELECT * FROM thread_consistency_feedback WHERE {' AND '.join(clauses)} ORDER BY created_ts DESC,id DESC LIMIT ?",
            (*params, max(1, min(500, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def _get(self, conn: Any, key: str, default: str = "") -> str:
        row = conn.execute(
            "SELECT value FROM thread_migration_state WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"] if row else default)

    def _set(self, conn: Any, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO thread_migration_state(key, value, updated_ts) VALUES(?,?,?)",
            (str(key), str(value), time.time()),
        )

    # ----------------------------------------------------------------------- #
    #  queue operations                                                         #
    # ----------------------------------------------------------------------- #

    @_atomic_write
    def enqueue(self, scope_id: str, episode_id: str) -> bool:
        """Idempotently queue an episode for thread-build analysis.

        Returns True if a new row was inserted, False if already queued for the
        current builder version. The scope mapping is independently upserted.
        """
        with self._lock:
            conn = self._get_conn()
            now = time.time()
            clean_scope = str(scope_id or "default")
            clean_episode = str(episode_id)
            conn.execute(
                """INSERT INTO thread_episode_scopes(
                       episode_id,scope_id,assignment_source,confidence,created_ts,updated_ts)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(episode_id,scope_id) DO UPDATE SET
                       assignment_source=excluded.assignment_source,
                       confidence=MAX(thread_episode_scopes.confidence,excluded.confidence),
                       updated_ts=excluded.updated_ts""",
                (clean_episode, clean_scope, "queue", 1.0, now, now),
            )
            cursor = conn.execute(
                """INSERT OR IGNORE INTO thread_build_queue(
                    scope_id, episode_id, builder_version, status,
                    next_attempt_ts, created_ts, updated_ts)
                   VALUES(?,?,?,?,?,?,?)""",
                (clean_scope, clean_episode, BUILDER_VERSION,
                 "pending", now, now, now),
            )
            ep = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (clean_episode,)).fetchone()
            digest = hashlib.sha256(_json(dict(ep) if ep else {}).encode()).hexdigest()
            prior = conn.execute("SELECT input_hash,status FROM thread_build_queue WHERE scope_id=? AND episode_id=? AND builder_version=?",
                                 (clean_scope, clean_episode, BUILDER_VERSION)).fetchone()
            changed = bool(prior and prior['input_hash'] != digest)
            if changed:
                conn.execute("""UPDATE thread_build_queue SET input_hash=?,
                    rerun_requested=CASE WHEN status='running' THEN 1 ELSE 0 END,
                    status=CASE WHEN status='running' THEN status ELSE 'pending' END,
                    retry_count=0,next_attempt_ts=?,updated_ts=?
                    WHERE scope_id=? AND episode_id=? AND builder_version=?""",
                    (digest, now, now, clean_scope, clean_episode, BUILDER_VERSION))
            conn.commit()
        return cursor.rowcount > 0 or changed

    def health(self) -> dict[str, Any]:
        """Cheap indexed probes, without full table counts."""
        if not self._lock.acquire(blocking=False):
            return {"busy": True}
        try:
            conn = self._get_conn()
            now = time.time()
            oldest = conn.execute("SELECT next_attempt_ts FROM thread_build_queue WHERE status='pending' ORDER BY next_attempt_ts LIMIT 1").fetchone()
            return {"busy": bool(conn.in_transaction), "pending": oldest is not None,
                    "oldest_pending_age_seconds": max(0., now - float(oldest[0])) if oldest else 0.,
                    "expired_build_claim": bool(conn.execute("SELECT 1 FROM thread_build_queue WHERE status='running' AND lease_until<=? LIMIT 1", (now,)).fetchone()),
                    "candidate_dirty": bool(conn.execute("SELECT 1 FROM thread_candidate_dirty LIMIT 1").fetchone()),
                    "page_count": conn.execute("PRAGMA page_count").fetchone()[0],
                    "freelist_count": conn.execute("PRAGMA freelist_count").fetchone()[0]}
        finally:
            self._lock.release()

    def maintenance(self, *, observation_before: float | None = None,
                    rejected_before: float | None = None, limit: int = 200,
                    analyze: bool = False, checkpoint: bool = False,
                    busy: bool = False) -> dict[str, Any]:
        """Bounded idle retention; preserve source, locks, feedback and audit.

        Caller busy or active lease skips work. Zero busy timeout, sampled ANALYZE,
        PASSIVE checkpoint only. No VACUUM or source-table changes.
        """
        if busy or not self._lock.acquire(blocking=False):
            return {"skipped": "busy", "deleted": 0}
        conn = None
        old_timeout = None
        try:
            conn = self._get_conn()
            if conn.in_transaction:
                return {"skipped": "transaction", "deleted": 0}
            old_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            conn.execute("PRAGMA busy_timeout=0")
            conn.execute("BEGIN IMMEDIATE")
            for table in ("thread_build_queue", "thread_arbitration_jobs"):
                if conn.execute(f"SELECT 1 FROM {table} WHERE status='running' AND lease_until>? LIMIT 1", (time.time(),)).fetchone():
                    conn.rollback()
                    return {"skipped": "active_worker", "deleted": 0}
            remaining = max(1, min(1000, int(limit)))
            deleted = {}
            if observation_before is not None:
                for table, timestamp, extra in (
                    ("thread_query_observations", "created_ts", "AND NOT EXISTS(SELECT 1 FROM thread_consistency_feedback f WHERE f.request_id=thread_query_observations.request_id)"),
                    ("thread_request_observations", "updated_ts", "AND NOT EXISTS(SELECT 1 FROM thread_consistency_feedback f WHERE f.request_id=thread_request_observations.request_id)"),
                    ("thread_consistency_observations", "created_ts", "AND NOT EXISTS(SELECT 1 FROM thread_consistency_feedback f WHERE f.request_id=thread_consistency_observations.request_id)"),
                    ("prospective_trigger_observations", "created_ts", "AND feedback=''"),
                    ("thread_build_runs", "created_ts", ""),
                ):
                    if remaining <= 0:
                        break
                    cur = conn.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {timestamp}<? ORDER BY {timestamp} LIMIT ?) {extra}",
                                       (float(observation_before), remaining))
                    deleted[table] = cur.rowcount
                    remaining -= cur.rowcount
            if rejected_before is not None and remaining > 0:
                cur = conn.execute("""DELETE FROM memory_episode_edges WHERE id IN
                    (SELECT id FROM memory_episode_edges WHERE status='rejected' AND manual_lock=0
                     AND updated_ts<? ORDER BY updated_ts LIMIT ?)
                    AND decision_source!='manual'
                    AND NOT EXISTS(SELECT 1 FROM thread_manual_feedback f WHERE f.target_type='edge' AND f.target_id=CAST(memory_episode_edges.id AS TEXT))
                    AND NOT EXISTS(SELECT 1 FROM thread_arbitration_jobs j WHERE j.edge_id=memory_episode_edges.id AND j.status='running')""", (float(rejected_before), remaining))
                deleted['memory_episode_edges'] = cur.rowcount
            conn.commit()
            result = {"deleted": sum(deleted.values()), "tables": deleted, "skipped": ""}
            if analyze:
                old_limit = conn.execute("PRAGMA analysis_limit").fetchone()[0]
                try:
                    conn.execute("PRAGMA analysis_limit=200")
                    for table in ("thread_candidate_postings", "thread_candidate_episodes", "thread_build_queue", "memory_threads"):
                        conn.execute(f"ANALYZE {table}")
                    conn.commit()
                    result['analyzed'] = True
                finally:
                    conn.execute(f"PRAGMA analysis_limit={int(old_limit)}")
            if checkpoint:
                result['checkpoint'] = list(conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
            return result
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.rollback()
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                return {"skipped": "sqlite_busy", "deleted": 0}
            raise
        finally:
            if conn is not None and old_timeout is not None:
                conn.execute(f"PRAGMA busy_timeout={int(old_timeout)}")
            self._lock.release()

    def queue_counts(self) -> dict[str, int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM thread_build_queue GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    @_atomic_write
    def take_pending_batch(self, limit: int = 10, *, lease_seconds: float = 300.0) -> list[dict[str, Any]]:
        """Claim a bounded batch; expired claims can be recovered by any worker."""
        conn = self._get_conn()
        now = time.time()
        self._recover_expired(conn, now, max(1, min(200, int(limit))))
        rows = conn.execute("""SELECT * FROM thread_build_queue
            WHERE status='pending' AND next_attempt_ts<=?
            ORDER BY next_attempt_ts,id LIMIT ?""", (now, max(1, min(200, int(limit))))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            token = uuid.uuid4().hex
            lease = now + max(1., min(3600., float(lease_seconds)))
            conn.execute("""UPDATE thread_build_queue SET status='running',claim_token=?,claim_owner=?,
                lease_until=?,updated_ts=? WHERE id=?""", (token, self._owner, lease, now, item['id']))
            self._claims[("build", int(item['id']))] = token
            item.update(claim_token=token, lease_until=lease)
            result.append(item)
        return result

    def _recover_expired(self, conn: Any, now: float, limit: int) -> int:
        recovered = 0
        for table in ("thread_build_queue", "thread_arbitration_jobs"):
            cur = conn.execute(f"""UPDATE {table} SET status='pending',claim_token='',claim_owner='',
                lease_until=0,updated_ts=? WHERE id IN (SELECT id FROM {table}
                WHERE status='running' AND lease_until<=? ORDER BY lease_until LIMIT ?)""", (now, now, limit))
            recovered += cur.rowcount
        return recovered

    @_atomic_write
    def recover_expired_leases(self, limit: int = 200) -> int:
        return self._recover_expired(self._get_conn(), time.time(), max(1, min(1000, int(limit))))

    @_atomic_write
    def renew_claim(self, queue_id: int, claim_token: str, *, lease_seconds: float = 300.0) -> bool:
        now = time.time()
        return self._get_conn().execute("""UPDATE thread_build_queue SET lease_until=?,updated_ts=?
            WHERE id=? AND status='running' AND claim_token=? AND lease_until>?""",
            (now + max(1., min(3600., lease_seconds)), now, int(queue_id), claim_token, now)).rowcount == 1

    @_atomic_write
    def mark_completed(self, queue_id: int, *, elapsed_ms: float = 0.0,
                       candidates: int = 0, accepted: int = 0, rejected: int = 0,
                       ambiguous: int = 0, error: str = "", max_retries: int = 0,
                       claim_token: str | None = None) -> bool:
        conn = self._get_conn()
        token = claim_token or self._claims.get(("build", int(queue_id)), "")
        now = time.time()
        row = conn.execute("""SELECT * FROM thread_build_queue WHERE id=? AND status='running'
            AND claim_token=? AND lease_until>?""", (int(queue_id), token, now)).fetchone()
        if not row or not token:
            return False
        retries = int(row['retry_count'])
        retry = bool(error) and retries < max(0, int(max_retries))
        status = 'pending' if retry or row['rerun_requested'] else ('quarantined' if error else 'completed')
        retries += int(retry)
        next_ts = now + min(3600., 15. * 2 ** min(12, max(0, retries - 1))) if retry else now
        conn.execute("""UPDATE thread_build_queue SET status=?,retry_count=?,next_attempt_ts=?,last_error=?,
            updated_ts=?,claim_token='',claim_owner='',lease_until=0,rerun_requested=0 WHERE id=?""",
            (status, retries, next_ts, str(error)[:500], now, int(queue_id)))
        conn.execute("""INSERT INTO thread_build_runs(run_id,scope_id,episode_id,builder_version,
            elapsed_ms,candidates_found,edges_accepted,edges_rejected,edges_ambiguous,error,status,created_ts)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (token, row['scope_id'], row['episode_id'], row['builder_version'],
            elapsed_ms, candidates, accepted, rejected, ambiguous, str(error)[:500], status, now))
        self._claims.pop(("build", int(queue_id)), None)
        return True

    @_atomic_write
    def restore_running_to_pending(self) -> int:
        """Release this store owner's work only, AFTER its workers have drained."""
        conn = self._get_conn()
        restored = 0
        for table in ("thread_build_queue", "thread_arbitration_jobs"):
            cur = conn.execute(f"""UPDATE {table} SET status='pending',claim_token='',claim_owner='',
                lease_until=0,updated_ts=? WHERE status='running' AND claim_owner=?""", (time.time(), self._owner))
            restored += cur.rowcount
        self._claims.clear()
        return restored

    # ----------------------------------------------------------------------- #
    #  status                                                                   #
    # ----------------------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        conn = self._get_conn()
        queue = self.queue_counts()
        total = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1"
        ).fetchone()["n"] or 0)
        scanned_row = conn.execute(
            "SELECT value FROM thread_migration_state WHERE key='scan_scanned_count'"
        ).fetchone()
        try:
            scanned = int(scanned_row["value"] or 0) if scanned_row else 0
        except (TypeError, ValueError):
            scanned = 0
        thread_count = int(conn.execute(
            "SELECT COUNT(*) AS n FROM memory_threads WHERE status!='closed'"
        ).fetchone()["n"] or 0)
        arbitration = {
            str(row["status"]): int(row["n"])
            for row in conn.execute(
                "SELECT status,COUNT(*) AS n FROM thread_arbitration_jobs GROUP BY status"
            ).fetchall()
        }
        claim_status = {
            str(row["status"]): int(row["n"])
            for row in conn.execute("SELECT status,COUNT(*) AS n FROM memory_claims GROUP BY status").fetchall()
        }
        prospective_status = {
            str(row["status"]): int(row["n"])
            for row in conn.execute(
                "SELECT status,COUNT(*) AS n FROM prospective_memory_items GROUP BY status"
            ).fetchall()
        }
        covered = int(conn.execute("""SELECT COUNT(DISTINCT m.episode_id) AS n
            FROM memory_thread_members m JOIN episodes e ON e.episode_id=m.episode_id
            JOIN memory_threads t ON t.thread_id=m.thread_id
            WHERE e.active=1 AND t.status!='closed'""").fetchone()["n"] or 0)
        return {
            "coverage": {"covered_episodes": covered, "total_active_episodes": total,
                         "ratio": round(covered / total, 6) if total else None,
                         "definition": "distinct active episodes in nonclosed threads / active episodes"},
            "mode": "shadow",
            "thread_schema_version": self._get(conn, "thread_schema_version", ""),
            "builder_version": BUILDER_VERSION,
            "arbitration_prompt_version": ARBITRATION_PROMPT_VERSION,
            "episodes_total": total,
            "episodes_scanned": scanned,
            "queue": queue,
            "threads": thread_count,
            "edges": self.edge_counts(),
            "arbitration": arbitration,
            "claims": claim_status,
            "claim_slots": int(conn.execute("SELECT COUNT(*) AS n FROM memory_claim_slots").fetchone()["n"] or 0),
            "thread_views": int(conn.execute("SELECT COUNT(*) AS n FROM thread_materialized_views").fetchone()["n"] or 0),
            "prospective": prospective_status,
            "query_observations": int(conn.execute("SELECT COUNT(*) AS n FROM thread_query_observations").fetchone()["n"] or 0),
            "eval_cases": int(conn.execute("SELECT COUNT(*) AS n FROM thread_eval_cases WHERE enabled=1").fetchone()["n"] or 0),
            "paused": self._get(conn, "worker_paused", "0") == "1",
        }

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            conn = self._get_conn()
            self._set(conn, "worker_paused", "1" if paused else "0")
            conn.commit()

    def is_paused(self) -> bool:
        return self._get(self._get_conn(), "worker_paused", "0") == "1"

    # ----------------------------------------------------------------------- #
    #  migration scan                                                            #
    # ----------------------------------------------------------------------- #

    def source_grade_counts(self) -> dict[str, int]:
        """Count episodes by source traceability grade (A/B/C/D).

        Grade A: episode has precise source_turn links pointing to existing turns
        Grade B: episode has source_batch but no verified turn links
        Grade C: diary-derived, no source linkage
        Grade D: corrupted / date unparseable
        """
        conn = self._get_conn()
        # Grade A: has at least one valid episode_turn_link with matching source_turn
        a = int(conn.execute(
            """SELECT COUNT(DISTINCT e.episode_id) AS n FROM episodes e
               JOIN episode_turn_links tl ON tl.episode_id=e.episode_id
               JOIN source_turns st ON st.batch_id=tl.batch_id AND st.turn_index=tl.turn_index
               WHERE e.active=1"""
        ).fetchone()["n"] or 0)
        # Grade B: has source_batch_id but no verified turn links
        b = int(conn.execute(
            """SELECT COUNT(*) AS n FROM episodes e
               WHERE e.active=1 AND (e.source_batch_id IS NOT NULL AND e.source_batch_id!='')
               AND NOT EXISTS (
                   SELECT 1 FROM episode_turn_links tl
                   JOIN source_turns st ON st.batch_id=tl.batch_id AND st.turn_index=tl.turn_index
                   WHERE tl.episode_id=e.episode_id)"""
        ).fetchone()["n"] or 0)
        # Grade C: diary_derived with no source linkage
        c = int(conn.execute(
            """SELECT COUNT(*) AS n FROM episodes e
               WHERE e.active=1
               AND (e.source_batch_id IS NULL OR e.source_batch_id='')
               AND (e.evidence_quality='diary_derived' OR e.evidence_quality IS NULL OR e.evidence_quality='')"""
        ).fetchone()["n"] or 0)
        # Grade D: everything else (mixed_user_edited without source, or parse issues)
        total = int(conn.execute("SELECT COUNT(*) AS n FROM episodes WHERE active=1").fetchone()["n"] or 0)
        d = max(0, total - a - b - c)
        return {"A": a, "B": b, "C": c, "D": d, "total": total}

    # ----------------------------------------------------------------------- #
    #  scoped edge operations                                                    #
    # ----------------------------------------------------------------------- #

    def insert_edge(self, edge: dict[str, Any]) -> bool:
        """Backward-compatible boolean wrapper around pair-level upsert."""
        return bool(self.upsert_edge(edge).get("inserted"))

    @_atomic_write
    def upsert_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        """Store one effective relation for a scoped Episode pair.

        test1 used ``INSERT OR IGNORE`` keyed by relation type, which meant an
        LLM result could not upgrade a provisional edge and could leave two
        contradictory rows for one pair. test2 updates the existing unlocked
        pair in place. A manual lock is never overwritten by automation.
        """
        with self._lock:
            conn = self._get_conn()
            if edge.get("_queue_id"):
                valid = conn.execute("SELECT 1 FROM thread_build_queue WHERE id=? AND status='running' AND claim_token=? AND lease_until>?",
                                     (int(edge["_queue_id"]), str(edge.get("_claim_token") or ""), time.time())).fetchone()
                if not valid:
                    raise RuntimeError("stale build claim")
            source = str(edge["source_episode_id"])
            target = str(edge["target_episode_id"])
            scope_id = str(edge.get("scope_id") or "default")
            existing = conn.execute(
                """SELECT * FROM memory_episode_edges
                   WHERE scope_id=? AND ((source_episode_id=? AND target_episode_id=?)
                     OR (source_episode_id=? AND target_episode_id=?))
                   ORDER BY manual_lock DESC,updated_ts DESC,id ASC LIMIT 1""",
                (scope_id, source, target, target, source),
            ).fetchone()
            now = float(edge.get("updated_ts", time.time()))
            payload = (
                str(edge.get("edge_type", "parallel")),
                str(edge.get("direction", "none")),
                float(edge.get("confidence", 0.0)),
                str(edge.get("status", "provisional")),
                str(edge.get("evidence_json", "{}")),
                str(edge.get("counter_evidence_json", "[]")),
                str(edge.get("route_sources_json", "[]")),
                str(edge.get("arbiter_version", "")),
                int(edge.get("manual_lock", 0)),
                str(edge.get("decision_source", "rule")),
                str(edge.get("decision_json", "{}")),
                str(edge.get("content_hash", "")),
                now,
            )
            if existing and int(existing["manual_lock"] or 0) and not edge.get("manual_override"):
                return {"inserted": False, "updated": False, "locked": True, "edge_id": int(existing["id"])}
            if existing:
                edge_id = int(existing["id"])
                conn.execute(
                    """UPDATE memory_episode_edges SET
                       source_episode_id=?,target_episode_id=?,edge_type=?,direction=?,confidence=?,status=?,evidence_json=?,
                       counter_evidence_json=?,route_sources_json=?,arbiter_version=?,
                       manual_lock=?,decision_source=?,decision_json=?,content_hash=?,updated_ts=?
                       WHERE id=?""",
                    (source, target, *payload, edge_id),
                )
                # Older test1 builds may have produced relation-type duplicates.
                conn.execute(
                    """DELETE FROM memory_episode_edges
                       WHERE scope_id=? AND id<>? AND manual_lock=0
                         AND ((source_episode_id=? AND target_episode_id=?)
                           OR (source_episode_id=? AND target_episode_id=?))""",
                    (scope_id, edge_id, source, target, target, source),
                )
                inserted = False
            else:
                cursor = conn.execute(
                    """INSERT INTO memory_episode_edges(
                       scope_id,source_episode_id,target_episode_id,edge_type,direction,
                       confidence,status,evidence_json,counter_evidence_json,
                       route_sources_json,arbiter_version,manual_lock,decision_source,
                       decision_json,content_hash,created_ts,updated_ts)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (scope_id, source, target, *payload[:-1],
                     float(edge.get("created_ts", time.time())), payload[-1]),
                )
                edge_id = int(cursor.lastrowid)
                inserted = True
            conn.commit()
        return {"inserted": inserted, "updated": not inserted, "locked": False, "edge_id": edge_id}

    def list_edges(self, *, episode_id: str = "", status: str = "", scope_id: str = "",
                   limit: int = 50) -> list[dict[str, Any]]:
        conn = self._get_conn()
        clauses = ["1=1"]
        params: list[Any] = []
        if episode_id:
            clauses.append("(source_episode_id=? OR target_episode_id=?)")
            params.extend([episode_id, episode_id])
        if status:
            clauses.append("status=?")
            params.append(status)
        if scope_id:
            clauses.append("scope_id=?")
            params.append(scope_id)
        rows = conn.execute(
            f"""SELECT * FROM memory_episode_edges
                WHERE {' AND '.join(clauses)}
                ORDER BY created_ts DESC LIMIT ?""",
            (*params, max(1, min(1000, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_ambiguity_queue(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return provisional edges with their durable arbitration state."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT e.*,COALESCE(j.status,'pending') AS job_status,
                      COALESCE(j.attempt_count,0) AS attempt_count,
                      COALESCE(j.next_attempt_ts,0) AS next_attempt_ts,
                      COALESCE(j.last_error,'') AS last_error
               FROM memory_episode_edges e
               LEFT JOIN thread_arbitration_jobs j ON j.edge_id=e.id
               WHERE e.status IN ('provisional','uncertain') AND e.manual_lock=0
               ORDER BY e.confidence DESC,e.created_ts ASC
               LIMIT ?""",
            (max(1, min(1000, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

    def edge_counts(self) -> dict[str, int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM memory_episode_edges GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    # ------------------------------------------------------------------ #
    #  asynchronous arbitration                                           #
    # ------------------------------------------------------------------ #

    def ensure_arbitration_job(self, edge_id: int, scope_id: str) -> None:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR IGNORE INTO thread_arbitration_jobs(
                       edge_id,scope_id,status,next_attempt_ts,prompt_version,
                       created_ts,updated_ts)
                   VALUES(?,?,?,?,?,?,?)""",
                (int(edge_id), str(scope_id or "default"), "pending", now,
                 ARBITRATION_PROMPT_VERSION, now, now),
            )
            conn.commit()

    @_atomic_write
    def take_arbitration_batch(self, limit: int = 4) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            self._recover_expired(conn, now, 50)
            rows = conn.execute(
                """SELECT j.*,e.source_episode_id,e.target_episode_id,e.edge_type,
                          e.direction,e.confidence,e.evidence_json,e.counter_evidence_json,
                          e.content_hash,e.status AS edge_status
                   FROM thread_arbitration_jobs j
                   JOIN memory_episode_edges e ON e.id=j.edge_id
                   WHERE j.status IN ('pending','retry_wait')
                     AND j.next_attempt_ts<=? AND e.manual_lock=0
                     AND e.status IN ('provisional','uncertain')
                   ORDER BY e.confidence DESC,j.created_ts ASC LIMIT ?""",
                (now, max(1, min(50, int(limit)))),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                token = uuid.uuid4().hex
                conn.execute("""UPDATE thread_arbitration_jobs SET status='running',updated_ts=?,
                    claim_token=?,claim_owner=?,lease_until=? WHERE id=?""", (now, token, self._owner, now + 300., item['id']))
                self._claims[("arbitration", int(item['id']))] = token
                item['claim_token'] = token
                result.append(item)
        return result

    def arbitration_cache_get(self, cache_key: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT decision_json FROM thread_arbitration_cache WHERE cache_key=?",
                (str(cache_key),),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE thread_arbitration_cache SET last_used_ts=? WHERE cache_key=?",
                (time.time(), str(cache_key)),
            )
            conn.commit()
        value = _loads(row["decision_json"], None)
        return value if isinstance(value, dict) else None

    def arbitration_cache_put(self, cache_key: str, decision: dict[str, Any], *,
                              provider_id: str, model_id: str,
                              prompt_version: str) -> None:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO thread_arbitration_cache(
                       cache_key,decision_json,provider_id,model_id,prompt_version,
                       created_ts,last_used_ts) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       decision_json=excluded.decision_json,
                       provider_id=excluded.provider_id,model_id=excluded.model_id,
                       prompt_version=excluded.prompt_version,last_used_ts=excluded.last_used_ts""",
                (str(cache_key), _json(decision), str(provider_id), str(model_id),
                 str(prompt_version), now, now),
            )
            conn.commit()

    @_atomic_write
    def complete_arbitration_job(self, job_id: int, *, decision: dict[str, Any] | None = None,
                                 error: str = "", max_retries: int = 3,
                                 retry_base_seconds: float = 30.0,
                                 provider_id: str = "", model_id: str = "",
                                 cache_key: str = "", input_hash: str = "", claim_token: str | None = None) -> str:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT attempt_count FROM thread_arbitration_jobs WHERE id=? AND status='running' AND claim_token=? AND lease_until>?",
                (int(job_id), claim_token or self._claims.get(("arbitration", int(job_id)), ""), now),
            ).fetchone()
            if not row:
                return "stale"
            attempts = int(row["attempt_count"] or 0) + 1
            if error and attempts <= max(0, int(max_retries)):
                status = "retry_wait"
                next_attempt = now + min(3600.0, float(retry_base_seconds) * (2 ** max(0, attempts - 1)))
            elif error:
                status = "failed"
                next_attempt = now
            else:
                status = "completed"
                next_attempt = now
            conn.execute(
                """UPDATE thread_arbitration_jobs SET status=?,attempt_count=?,
                   next_attempt_ts=?,last_error=?,decision_json=?,provider_id=?,
                   model_id=?,cache_key=?,input_hash=?,updated_ts=?,claim_token='',claim_owner='',lease_until=0 WHERE id=?""",
                (status, attempts, next_attempt, str(error)[:500],
                 _json(decision or {}), str(provider_id), str(model_id),
                 str(cache_key), str(input_hash), now, int(job_id)),
            )
            conn.commit()
        return status

    @_atomic_write
    def defer_arbitration_job(self, job_id: int, *, reason: str = "budget_paused",
                              delay_seconds: float = 3600.0, claim_token: str | None = None) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """UPDATE thread_arbitration_jobs SET status='pending',next_attempt_ts=?,
                   last_error=?,updated_ts=?,claim_token='',claim_owner='',lease_until=0
                   WHERE id=? AND status='running' AND claim_token=?""",
                (time.time() + max(1.0, float(delay_seconds)), str(reason)[:500],
                 time.time(), int(job_id), claim_token or self._claims.get(("arbitration", int(job_id)), "")),
            )
            conn.commit()

    def arbitration_calls_today(self) -> int:
        conn = self._get_conn()
        key = f"llm_calls:{time.strftime('%Y-%m-%d', time.localtime())}"
        try:
            return int(self._get(conn, key, "0") or 0)
        except (TypeError, ValueError):
            return 0

    def record_arbitration_call(self) -> int:
        key = f"llm_calls:{time.strftime('%Y-%m-%d', time.localtime())}"
        with self._lock:
            conn = self._get_conn()
            try:
                value = int(self._get(conn, key, "0") or 0) + 1
            except (TypeError, ValueError):
                value = 1
            self._set(conn, key, str(value))
            conn.commit()
        return value

    # ------------------------------------------------------------------ #
    #  thread projection and browsing                                     #
    # ------------------------------------------------------------------ #

    def list_threads(self, *, scope_id: str = "", status: str = "",
                     thread_type: str = "", limit: int = 100) -> list[dict[str, Any]]:
        conn = self._get_conn()
        clauses = ["1=1"]
        params: list[Any] = []
        for column, value in (("scope_id", scope_id), ("status", status), ("thread_type", thread_type)):
            if value:
                clauses.append(f"{column}=?")
                params.append(str(value))
        rows = conn.execute(
            f"""SELECT t.*,(SELECT COUNT(*) FROM memory_thread_members m WHERE m.thread_id=t.thread_id) AS member_count
                 FROM memory_threads t
                 WHERE {' AND '.join(clauses)}
                 ORDER BY t.last_event_ts DESC,t.updated_ts DESC LIMIT ?""",
            (*params, max(1, min(1000, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def thread_detail(self, thread_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM memory_threads WHERE thread_id=?", (str(thread_id),)).fetchone()
        if not row:
            return None
        members = conn.execute(
            """SELECT m.*,e.memo_name,e.occurred_at,e.event_ts,e.memory_type,
                      e.scene_anchor,e.retrieval_key,e.card_text,e.evidence_quality
               FROM memory_thread_members m JOIN episodes e ON e.episode_id=m.episode_id
               WHERE m.thread_id=? ORDER BY m.sequence_no,e.event_ts,e.episode_id""",
            (str(thread_id),),
        ).fetchall()
        return {"thread": dict(row), "members": [dict(item) for item in members]}

    @_atomic_write
    def replace_thread_projection(self, thread: dict[str, Any], members: list[dict[str, Any]],
                                  *, expected_version: int | None = None) -> dict[str, Any]:
        now = time.time()
        thread_id = str(thread["thread_id"])
        scope_id = str(thread.get("scope_id") or "default")
        with self._lock:
            conn = self._get_conn()
            current = conn.execute(
                "SELECT materialized_version,manual_lock FROM memory_threads WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if current and expected_version is not None and int(current["materialized_version"] or 0) != int(expected_version):
                raise RuntimeError("thread projection version conflict")
            if current and current['manual_lock']:
                return {"thread_id": thread_id, "materialized_version": int(current['materialized_version']), "changed": False, "locked": True}
            locked = {
                str(row["episode_id"])
                for row in conn.execute(
                    "SELECT episode_id FROM memory_thread_members WHERE thread_id=? AND manual_lock=1",
                    (thread_id,),
                ).fetchall()
            }
            if current:
                current_thread = conn.execute(
                    "SELECT * FROM memory_threads WHERE thread_id=?", (thread_id,)
                ).fetchone()
                current_members = conn.execute(
                    """SELECT episode_id,role,sequence_no,membership_confidence,
                              evidence_json,decision_source FROM memory_thread_members
                       WHERE thread_id=? AND manual_lock=0 ORDER BY episode_id""",
                    (thread_id,),
                ).fetchall()
                desired_members = sorted(
                    (
                        str(member["episode_id"]), str(member.get("role", "supporting")),
                        int(member.get("sequence_no", 0)), round(float(member.get("membership_confidence", 0.5)), 8),
                        _json(member.get("evidence", {})), str(member.get("decision_source", "projection")),
                    )
                    for member in members if str(member["episode_id"]) not in locked
                )
                existing_members = [
                    (
                        str(row["episode_id"]), str(row["role"]), int(row["sequence_no"] or 0),
                        round(float(row["membership_confidence"] or 0), 8), str(row["evidence_json"] or "{}"),
                        str(row["decision_source"] or ""),
                    )
                    for row in current_members
                ]
                metadata_same = all((
                    str(current_thread["scope_id"]) == scope_id,
                    str(current_thread["thread_type"]) == str(thread.get("thread_type", "other")),
                    str(current_thread["title"]) == str(thread.get("title", "")),
                    str(current_thread["status"]) == str(thread.get("status", "active")),
                    abs(float(current_thread["confidence"] or 0) - float(thread.get("confidence", 0.5))) < 1e-8,
                    abs(float(current_thread["first_event_ts"] or 0) - float(thread.get("first_event_ts", 0))) < 1e-6,
                    abs(float(current_thread["last_event_ts"] or 0) - float(thread.get("last_event_ts", 0))) < 1e-6,
                    str(current_thread["source_quality_floor"] or "") == str(thread.get("source_quality_floor", "diary_derived")),
                ))
                if metadata_same and existing_members == desired_members:
                    total_members = int(conn.execute(
                        "SELECT COUNT(*) FROM memory_thread_members WHERE thread_id=?", (thread_id,)
                    ).fetchone()[0] or 0)
                    return {"thread_id": thread_id, "materialized_version": int(current["materialized_version"]),
                            "member_count": total_members, "changed": False}
            if current:
                old_version = int(current["materialized_version"] or 0)
                version = old_version + 1
                conn.execute(
                    """UPDATE memory_threads SET scope_id=?,thread_type=?,title=?,status=?,
                       confidence=?,first_event_ts=?,last_event_ts=?,materialized_version=?,
                       source_quality_floor=?,updated_ts=? WHERE thread_id=?""",
                    (scope_id, str(thread.get("thread_type", "other")), str(thread.get("title", "")),
                     str(thread.get("status", "active")), float(thread.get("confidence", 0.5)),
                     float(thread.get("first_event_ts", 0)), float(thread.get("last_event_ts", 0)),
                     version, str(thread.get("source_quality_floor", "diary_derived")), now, thread_id),
                )
            else:
                if expected_version not in (None, 0):
                    raise RuntimeError("thread projection version conflict")
                version = 1
                conn.execute(
                    """INSERT INTO memory_threads(thread_id,scope_id,thread_type,title,status,
                       confidence,first_event_ts,last_event_ts,materialized_version,
                       source_quality_floor,created_ts,updated_ts)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (thread_id, scope_id, str(thread.get("thread_type", "other")),
                     str(thread.get("title", "")), str(thread.get("status", "active")),
                     float(thread.get("confidence", 0.5)), float(thread.get("first_event_ts", 0)),
                     float(thread.get("last_event_ts", 0)), version,
                     str(thread.get("source_quality_floor", "diary_derived")), now, now),
                )
            conn.execute(
                "DELETE FROM memory_thread_members WHERE thread_id=? AND manual_lock=0",
                (thread_id,),
            )
            for member in members:
                episode_id = str(member["episode_id"])
                if episode_id in locked:
                    continue
                conn.execute(
                    """INSERT INTO memory_thread_members(thread_id,episode_id,role,sequence_no,
                       membership_confidence,evidence_json,decision_source,manual_lock,created_ts)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(thread_id,episode_id) DO UPDATE SET
                         role=excluded.role,sequence_no=excluded.sequence_no,
                         membership_confidence=excluded.membership_confidence,
                         evidence_json=excluded.evidence_json,decision_source=excluded.decision_source""",
                    (thread_id, episode_id, str(member.get("role", "supporting")),
                     int(member.get("sequence_no", 0)), float(member.get("membership_confidence", 0.5)),
                     _json(member.get("evidence", {})), str(member.get("decision_source", "projection")),
                     int(member.get("manual_lock", 0)), now),
                )
            conn.commit()
        return {"thread_id": thread_id, "materialized_version": version,
                "member_count": len(members), "changed": True}

    # ------------------------------------------------------------------ #
    #  test3 claim ledger and materialized current views                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def decode_claim(row: dict[str, Any] | Any) -> dict[str, Any]:
        data = dict(row)
        data["source_turn_refs"] = _loads(data.pop("source_turn_refs_json", "[]"), [])
        data["evidence"] = _loads(data.pop("evidence_json", "{}"), {})
        data["is_hypothetical"] = bool(data.get("is_hypothetical"))
        data["is_dream"] = bool(data.get("is_dream"))
        return data

    def upsert_claims(self, claims: list[dict[str, Any]]) -> int:
        if not claims:
            return 0
        now = time.time()
        written = 0
        with self._lock:
            conn = self._get_conn()
            for claim in claims:
                existing = conn.execute(
                    "SELECT manual_lock FROM memory_claims WHERE claim_id=?",
                    (str(claim["claim_id"]),),
                ).fetchone()
                if existing and int(existing["manual_lock"] or 0):
                    continue
                conn.execute(
                    """INSERT INTO memory_claims(
                       claim_id,scope_id,slot_key,subject,predicate,object,claim_type,
                       valid_from,valid_to,status,confidence,explicitness,source_episode_id,
                       source_turn_refs_json,source_quality,is_hypothetical,is_dream,
                       evidence_json,content_hash,extractor_version,decision_source,
                       manual_lock,created_ts,updated_ts)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(claim_id) DO UPDATE SET
                         scope_id=excluded.scope_id,slot_key=excluded.slot_key,
                         subject=excluded.subject,predicate=excluded.predicate,object=excluded.object,
                         claim_type=excluded.claim_type,valid_from=excluded.valid_from,
                         valid_to=excluded.valid_to,status=excluded.status,
                         confidence=excluded.confidence,explicitness=excluded.explicitness,
                         source_turn_refs_json=excluded.source_turn_refs_json,
                         source_quality=excluded.source_quality,
                         is_hypothetical=excluded.is_hypothetical,is_dream=excluded.is_dream,
                         evidence_json=excluded.evidence_json,content_hash=excluded.content_hash,
                         extractor_version=excluded.extractor_version,
                         decision_source=excluded.decision_source,updated_ts=excluded.updated_ts""",
                    (
                        str(claim["claim_id"]), str(claim.get("scope_id") or "default"),
                        str(claim.get("slot_key") or ""), str(claim.get("subject") or ""),
                        str(claim.get("predicate") or ""), str(claim.get("object") or ""),
                        str(claim.get("claim_type") or "belief"), float(claim.get("valid_from") or 0),
                        float(claim.get("valid_to") or 0), str(claim.get("status") or "uncertain"),
                        float(claim.get("confidence") or 0.5), float(claim.get("explicitness") or 0.5),
                        str(claim.get("source_episode_id") or ""), _json(claim.get("source_turn_refs") or []),
                        str(claim.get("source_quality") or "diary_derived"),
                        int(bool(claim.get("is_hypothetical"))), int(bool(claim.get("is_dream"))),
                        _json(claim.get("evidence") or {}), str(claim.get("content_hash") or ""),
                        str(claim.get("extractor_version") or ""), str(claim.get("decision_source") or "rule"),
                        0, now, now,
                    ),
                )
                written += 1
            conn.commit()
        return written

    def list_claims(self, *, scope_id: str = "", status: str = "", claim_type: str = "",
                    slot_key: str = "", query: str = "", limit: int = 300) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        for column, value in (("scope_id", scope_id), ("status", status),
                              ("claim_type", claim_type), ("slot_key", slot_key)):
            if value:
                clauses.append(f"{column}=?")
                params.append(str(value))
        rows = self._get_conn().execute(
            f"""SELECT * FROM memory_claims WHERE {' AND '.join(clauses)}
                ORDER BY manual_lock DESC,valid_from DESC,confidence DESC,updated_ts DESC LIMIT ?""",
            (*params, max(1, min(100000, int(limit)))),
        ).fetchall()
        terms = [term.lower() for term in str(query or "").split() if term]
        output = []
        for row in rows:
            item = self.decode_claim(row)
            if terms:
                text = " ".join(str(item.get(key) or "") for key in (
                    "slot_key", "subject", "predicate", "object", "claim_type"
                )).lower()
                if not any(term in text for term in terms):
                    continue
            output.append(item)
        return output

    def prune_auto_claims(self, scope_id: str, keep_claim_ids: set[str]) -> int:
        """Remove obsolete rebuildable claims while preserving manual truth."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT claim_id FROM memory_claims WHERE scope_id=?
               AND manual_lock=0 AND decision_source='deterministic_extractor'""",
            (str(scope_id),),
        ).fetchall()
        stale = [str(row["claim_id"]) for row in rows if str(row["claim_id"]) not in keep_claim_ids]
        if not stale:
            return 0
        with self._lock:
            placeholders = ",".join("?" for _ in stale)
            conn.execute(
                f"DELETE FROM memory_claim_transitions WHERE from_claim_id IN ({placeholders}) OR to_claim_id IN ({placeholders})",
                (*stale, *stale),
            )
            conn.execute(
                f"DELETE FROM prospective_memory_items WHERE source_claim_id IN ({placeholders}) AND manual_status=''",
                tuple(stale),
            )
            conn.execute(f"DELETE FROM memory_claims WHERE claim_id IN ({placeholders})", tuple(stale))
            conn.execute(
                """DELETE FROM memory_claim_slots WHERE scope_id=? AND NOT EXISTS (
                   SELECT 1 FROM memory_claims c
                   WHERE c.scope_id=memory_claim_slots.scope_id
                     AND c.slot_key=memory_claim_slots.slot_key)""",
                (str(scope_id),),
            )
            conn.commit()
        return len(stale)

    def update_claim_status(self, claim_id: str, status: str, *, valid_to: float | None = None) -> bool:
        if status not in {"active", "superseded", "resolved", "broken", "historical", "uncertain", "rejected"}:
            raise ValueError("invalid claim status")
        with self._lock:
            conn = self._get_conn()
            if valid_to is None:
                cur = conn.execute(
                    "UPDATE memory_claims SET status=?,updated_ts=? WHERE claim_id=? AND manual_lock=0",
                    (str(status), time.time(), str(claim_id)),
                )
            else:
                cur = conn.execute(
                    "UPDATE memory_claims SET status=?,valid_to=?,updated_ts=? WHERE claim_id=? AND manual_lock=0",
                    (str(status), float(valid_to), time.time(), str(claim_id)),
                )
            conn.commit()
        return bool(cur.rowcount)

    def claim_manual_status(self, claim_id: str, status: str, *, operator: str = "webui") -> dict[str, Any]:
        if status not in {"active", "superseded", "resolved", "broken", "historical", "uncertain", "rejected"}:
            raise ValueError("invalid claim status")
        with self._lock:
            conn = self._get_conn()
            before = conn.execute("SELECT * FROM memory_claims WHERE claim_id=?", (str(claim_id),)).fetchone()
            if not before:
                raise KeyError("claim not found")
            conn.execute(
                "UPDATE memory_claims SET status=?,manual_lock=1,decision_source='manual',updated_ts=? WHERE claim_id=?",
                (str(status), time.time(), str(claim_id)),
            )
            after = conn.execute("SELECT * FROM memory_claims WHERE claim_id=?", (str(claim_id),)).fetchone()
            operation_id = self._record_operation(
                conn, str(before["scope_id"]), "claim_status",
                {"claim": dict(before)}, {"claim": dict(after)}, operator,
            )
            conn.commit()
        return {"operation_id": operation_id, "claim": self.decode_claim(after)}

    def add_claim_transition(self, *, from_claim_id: str, to_claim_id: str,
                             transition_type: str, reason: str, evidence: dict[str, Any],
                             decided_by: str, confidence: float) -> bool:
        key = hashlib.sha256(
            f"{from_claim_id}|{to_claim_id}|{transition_type}|{reason}".encode("utf-8")
        ).hexdigest()
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                """INSERT OR IGNORE INTO memory_claim_transitions(
                   from_claim_id,to_claim_id,transition_type,reason,evidence_json,
                   decided_by,confidence,transition_key,created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(from_claim_id), str(to_claim_id), str(transition_type), str(reason),
                 _json(evidence), str(decided_by), float(confidence), key, time.time()),
            )
            conn.commit()
        return bool(cur.rowcount)

    def list_claim_transitions(self, *, thread_id: str = "", claim_id: str = "",
                               limit: int = 100) -> list[dict[str, Any]]:
        conn = self._get_conn()
        clauses = ["1=1"]
        params: list[Any] = []
        if claim_id:
            clauses.append("(t.from_claim_id=? OR t.to_claim_id=?)")
            params.extend([str(claim_id), str(claim_id)])
        if thread_id:
            clauses.append(
                "(f.source_episode_id IN (SELECT episode_id FROM memory_thread_members WHERE thread_id=?) "
                "OR n.source_episode_id IN (SELECT episode_id FROM memory_thread_members WHERE thread_id=?))"
            )
            params.extend([str(thread_id), str(thread_id)])
        rows = conn.execute(
            f"""SELECT t.* FROM memory_claim_transitions t
                LEFT JOIN memory_claims f ON f.claim_id=t.from_claim_id
                LEFT JOIN memory_claims n ON n.claim_id=t.to_claim_id
                WHERE {' AND '.join(clauses)} ORDER BY t.created_ts DESC,t.id DESC LIMIT ?""",
            (*params, max(1, min(5000, int(limit)))),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = _loads(item.pop("evidence_json", "{}"), {})
            output.append(item)
        return output

    def upsert_claim_slot(self, scope_id: str, slot_key: str, *, current_ids: list[str],
                          status: str, conflict_count: int, view_text: str) -> None:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            existing = conn.execute(
                "SELECT current_claim_ids_json,status,view_text,conflict_count,version "
                "FROM memory_claim_slots WHERE scope_id=? AND slot_key=?",
                (str(scope_id), str(slot_key)),
            ).fetchone()
            payload = (_json(current_ids), str(status), str(view_text), int(conflict_count))
            version = int(existing["version"] or 0) if existing else 0
            if not existing or tuple(existing[key] for key in (
                "current_claim_ids_json", "status", "view_text", "conflict_count"
            )) != payload:
                version += 1
            conn.execute(
                """INSERT INTO memory_claim_slots(scope_id,slot_key,current_claim_ids_json,
                   status,view_text,conflict_count,version,created_ts,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_id,slot_key) DO UPDATE SET
                   current_claim_ids_json=excluded.current_claim_ids_json,status=excluded.status,
                   view_text=excluded.view_text,conflict_count=excluded.conflict_count,
                   version=excluded.version,updated_ts=excluded.updated_ts""",
                (str(scope_id), str(slot_key), *payload, version, now, now),
            )
            conn.commit()

    def list_claim_slots(self, *, scope_id: str = "", status: str = "", limit: int = 300) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if scope_id:
            clauses.append("scope_id=?")
            params.append(str(scope_id))
        if status:
            clauses.append("status=?")
            params.append(str(status))
        rows = self._get_conn().execute(
            f"SELECT * FROM memory_claim_slots WHERE {' AND '.join(clauses)} ORDER BY updated_ts DESC LIMIT ?",
            (*params, max(1, min(5000, int(limit)))),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["current_claim_ids"] = _loads(item.pop("current_claim_ids_json", "[]"), [])
            output.append(item)
        return output

    @_atomic_write
    def materialize_thread_view(self, thread_id: str, payload: dict[str, Any]) -> bool:
        now = time.time()
        serialized = {
            "active_facts_json": _json(payload.get("active_facts") or []),
            "recent_transitions_json": _json(payload.get("recent_transitions") or []),
            "open_loops_json": _json(payload.get("open_loops") or []),
            "active_promises_json": _json(payload.get("active_promises") or []),
            "counter_evidence_json": _json(payload.get("counter_evidence") or []),
            "source_episode_ids_json": _json(payload.get("source_episode_ids") or []),
        }
        with self._lock:
            conn = self._get_conn()
            existing = conn.execute(
                "SELECT * FROM thread_materialized_views WHERE thread_id=?", (str(thread_id),)
            ).fetchone()
            unchanged = bool(existing) and all(str(existing[key]) == value for key, value in serialized.items())
            unchanged = unchanged and abs(float(existing["coverage"] or 0) - float(payload.get("coverage") or 0)) < 1e-9
            unchanged = unchanged and int(existing["conflict_count"] or 0) == int(payload.get("conflict_count") or 0)
            unchanged = unchanged and str(existing["generator_version"] or "") == str(payload.get("generator_version") or "")
            if unchanged:
                return False
            version = int(existing["view_version"] or 0) + 1 if existing else 1
            snapshot = {**payload, "thread_id": str(thread_id), "view_version": version}
            conn.execute(
                """INSERT INTO thread_materialized_views(thread_id,view_version,active_facts_json,
                   recent_transitions_json,open_loops_json,active_promises_json,
                   counter_evidence_json,source_episode_ids_json,generator_version,coverage,
                   conflict_count,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(thread_id) DO UPDATE SET view_version=excluded.view_version,
                   active_facts_json=excluded.active_facts_json,
                   recent_transitions_json=excluded.recent_transitions_json,
                   open_loops_json=excluded.open_loops_json,
                   active_promises_json=excluded.active_promises_json,
                   counter_evidence_json=excluded.counter_evidence_json,
                   source_episode_ids_json=excluded.source_episode_ids_json,
                   generator_version=excluded.generator_version,coverage=excluded.coverage,
                   conflict_count=excluded.conflict_count,updated_ts=excluded.updated_ts""",
                (str(thread_id), version, serialized["active_facts_json"],
                 serialized["recent_transitions_json"], serialized["open_loops_json"],
                 serialized["active_promises_json"], serialized["counter_evidence_json"],
                 serialized["source_episode_ids_json"], str(payload.get("generator_version") or ""),
                 float(payload.get("coverage") or 0), int(payload.get("conflict_count") or 0), now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO thread_view_versions(thread_id,view_version,snapshot_json,created_ts) VALUES(?,?,?,?)",
                (str(thread_id), version, _json(snapshot), now),
            )
            conn.commit()
        return True

    def thread_view(self, thread_id: str) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT * FROM thread_materialized_views WHERE thread_id=?", (str(thread_id),)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        for source, target in (
            ("active_facts_json", "active_facts"), ("recent_transitions_json", "recent_transitions"),
            ("open_loops_json", "open_loops"), ("active_promises_json", "active_promises"),
            ("counter_evidence_json", "counter_evidence"),
            ("source_episode_ids_json", "source_episode_ids"),
        ):
            item[target] = _loads(item.pop(source, "[]"), [])
        return item

    def thread_view_history(self, thread_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            "SELECT * FROM thread_view_versions WHERE thread_id=? ORDER BY view_version DESC LIMIT ?",
            (str(thread_id), max(1, min(500, int(limit)))),
        ).fetchall()
        return [{**dict(row), "snapshot": _loads(row["snapshot_json"], {})} for row in rows]

    # ------------------------------------------------------------------ #
    #  test4 prospective memory                                          #
    # ------------------------------------------------------------------ #

    def upsert_prospective_items(self, items: list[dict[str, Any]]) -> int:
        now = time.time()
        written = 0
        with self._lock:
            conn = self._get_conn()
            for item in items:
                existing = conn.execute(
                    "SELECT manual_status,status FROM prospective_memory_items WHERE item_id=?",
                    (str(item["item_id"]),),
                ).fetchone()
                status = str(existing["status"]) if existing and str(existing["manual_status"] or "") else str(item.get("status") or "pending")
                conn.execute(
                    """INSERT INTO prospective_memory_items(
                       item_id,scope_id,source_episode_id,source_thread_id,source_claim_id,
                       item_type,description,trigger_mode,due_start,due_end,status,salience,
                       explicitness,emotional_weight,cooldown_until,last_surfaced_ts,
                       resolution_evidence_json,manual_status,target_entities_json,
                       trigger_terms_json,last_trigger_routes_json,surfaced_count,
                       negative_feedback_count,status_reason,content_hash,created_ts,updated_ts)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(item_id) DO UPDATE SET
                       source_thread_id=excluded.source_thread_id,item_type=excluded.item_type,
                       description=excluded.description,trigger_mode=excluded.trigger_mode,
                       due_start=excluded.due_start,due_end=excluded.due_end,
                       status=CASE WHEN prospective_memory_items.manual_status!='' THEN prospective_memory_items.status ELSE excluded.status END,
                       salience=excluded.salience,explicitness=excluded.explicitness,
                       emotional_weight=excluded.emotional_weight,
                       target_entities_json=excluded.target_entities_json,
                       trigger_terms_json=excluded.trigger_terms_json,
                       status_reason=excluded.status_reason,content_hash=excluded.content_hash,
                       updated_ts=excluded.updated_ts""",
                    (
                        str(item["item_id"]), str(item.get("scope_id") or "default"),
                        str(item.get("source_episode_id") or ""), str(item.get("source_thread_id") or ""),
                        str(item.get("source_claim_id") or ""), str(item.get("item_type") or "commitment"),
                        str(item.get("description") or ""), str(item.get("trigger_mode") or "multi_route"),
                        float(item.get("due_start") or 0), float(item.get("due_end") or 0), status,
                        float(item.get("salience") or 0.5), float(item.get("explicitness") or 0.5),
                        float(item.get("emotional_weight") or 0), float(item.get("cooldown_until") or 0),
                        float(item.get("last_surfaced_ts") or 0), _json(item.get("resolution_evidence") or {}),
                        str(item.get("manual_status") or ""), _json(item.get("target_entities") or []),
                        _json(item.get("trigger_terms") or []), _json(item.get("last_trigger_routes") or {}),
                        int(item.get("surfaced_count") or 0), int(item.get("negative_feedback_count") or 0),
                        str(item.get("status_reason") or ""), str(item.get("content_hash") or ""), now, now,
                    ),
                )
                written += 1
            conn.commit()
        return written

    def prune_auto_prospective(self, scope_id: str, keep_item_ids: set[str]) -> int:
        """Prune stale generated items; manual lifecycle decisions are audit data."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT item_id FROM prospective_memory_items
               WHERE scope_id=? AND manual_status=''
                 AND status NOT IN ('resolved','expired')""",
            (str(scope_id),),
        ).fetchall()
        stale = [str(row["item_id"]) for row in rows if str(row["item_id"]) not in keep_item_ids]
        if not stale:
            return 0
        with self._lock:
            placeholders = ",".join("?" for _ in stale)
            conn.execute(f"DELETE FROM prospective_memory_items WHERE item_id IN ({placeholders})", tuple(stale))
            conn.commit()
        return len(stale)

    @staticmethod
    def decode_prospective(row: dict[str, Any] | Any) -> dict[str, Any]:
        item = dict(row)
        for source, target, fallback in (
            ("resolution_evidence_json", "resolution_evidence", {}),
            ("target_entities_json", "target_entities", []),
            ("trigger_terms_json", "trigger_terms", []),
            ("last_trigger_routes_json", "last_trigger_routes", {}),
        ):
            item[target] = _loads(item.pop(source, ""), fallback)
        return item

    def list_prospective(self, *, scope_id: str = "", status: str = "", limit: int = 300) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if scope_id:
            clauses.append("scope_id=?")
            params.append(str(scope_id))
        if status:
            clauses.append("status=?")
            params.append(str(status))
        rows = self._get_conn().execute(
            f"""SELECT * FROM prospective_memory_items WHERE {' AND '.join(clauses)}
                ORDER BY CASE status WHEN 'due' THEN 0 WHEN 'pending' THEN 1 WHEN 'uncertain' THEN 2 ELSE 3 END,
                due_start,salience DESC,updated_ts DESC LIMIT ?""",
            (*params, max(1, min(5000, int(limit)))),
        ).fetchall()
        return [self.decode_prospective(row) for row in rows]

    def prospective_manual_status(self, item_id: str, status: str, *, cooldown_until: float = 0,
                                  reason: str = "webui", operator: str = "webui") -> dict[str, Any]:
        if status not in {"pending", "due", "resolved", "snoozed", "expired", "uncertain"}:
            raise ValueError("invalid prospective status")
        with self._lock:
            conn = self._get_conn()
            before = conn.execute("SELECT * FROM prospective_memory_items WHERE item_id=?", (str(item_id),)).fetchone()
            if not before:
                raise KeyError("prospective item not found")
            conn.execute(
                """UPDATE prospective_memory_items SET status=?,manual_status=?,cooldown_until=?,
                   status_reason=?,updated_ts=? WHERE item_id=?""",
                (str(status), str(status), float(cooldown_until or 0), str(reason), time.time(), str(item_id)),
            )
            after = conn.execute("SELECT * FROM prospective_memory_items WHERE item_id=?", (str(item_id),)).fetchone()
            operation_id = self._record_operation(
                conn, str(before["scope_id"]), "prospective_status",
                {"prospective": dict(before)}, {"prospective": dict(after)}, operator,
            )
            conn.commit()
        return {"operation_id": operation_id, "item": self.decode_prospective(after)}

    def reserve_prospective_surface(
        self,
        item_id: str,
        *,
        request_id: str = "",
        reserved_ts: float | None = None,
        lease_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """Claim a short lease before a prospective item becomes visible."""
        reserved_ts = float(time.time() if reserved_ts is None else reserved_ts)
        token = str(request_id or "surface_" + uuid.uuid4().hex)
        lease_until = reserved_ts + max(1.0, min(30.0, float(lease_seconds)))
        with self._lock:
            conn = self._get_conn()
            before = conn.execute(
                "SELECT * FROM prospective_memory_items WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            if not before:
                raise KeyError("prospective item not found")
            cur = conn.execute(
                """UPDATE prospective_memory_items
                   SET surface_reservation=?,surface_reservation_until=?,updated_ts=?
                   WHERE item_id=?
                     AND status NOT IN ('resolved','expired','uncertain')
                     AND cooldown_until<=?
                     AND (surface_reservation='' OR surface_reservation_until<=?)""",
                (
                    token,
                    lease_until,
                    reserved_ts,
                    str(item_id),
                    reserved_ts,
                    reserved_ts,
                ),
            )
            after = conn.execute(
                "SELECT * FROM prospective_memory_items WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            conn.commit()
        return {
            "reserved": bool(cur.rowcount),
            "reservation": token if cur.rowcount else "",
            "item": self.decode_prospective(after),
        }

    def commit_prospective_surface(
        self,
        item_id: str,
        reservation: str,
        *,
        surfaced_ts: float | None = None,
        cooldown_seconds: float = 86400.0,
    ) -> dict[str, Any]:
        """Convert an owned visibility lease into the configured cooldown."""
        surfaced_ts = float(time.time() if surfaced_ts is None else surfaced_ts)
        cooldown_until = surfaced_ts + max(0.0, float(cooldown_seconds))
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                """UPDATE prospective_memory_items
                   SET last_surfaced_ts=?,cooldown_until=?,
                       surfaced_count=surfaced_count+1,surface_reservation='',
                       surface_reservation_until=0,updated_ts=?
                   WHERE item_id=? AND surface_reservation=?
                     AND status NOT IN ('resolved','expired','uncertain')""",
                (
                    surfaced_ts,
                    cooldown_until,
                    surfaced_ts,
                    str(item_id),
                    str(reservation),
                ),
            )
            after = conn.execute(
                "SELECT * FROM prospective_memory_items WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            conn.commit()
        if not after:
            raise KeyError("prospective item not found")
        return {"updated": bool(cur.rowcount), "item": self.decode_prospective(after)}

    def release_prospective_surface(
        self,
        item_id: str,
        reservation: str,
    ) -> bool:
        """Release a lease when request mutation did not complete."""
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                """UPDATE prospective_memory_items
                   SET surface_reservation='',surface_reservation_until=0,
                       updated_ts=?
                   WHERE item_id=? AND surface_reservation=?""",
                (time.time(), str(item_id), str(reservation)),
            )
            conn.commit()
        return bool(cur.rowcount)

    def record_prospective_surface(self, item_id: str, *, surfaced_ts: float | None = None,
                                   cooldown_seconds: float = 86400.0) -> dict[str, Any]:
        """Atomically record a prospective item that was actually injected.

        Trigger evaluation and Shadow previews are read-only.  The conditional
        update also makes concurrent successful requests safe: once one request
        installs the cooldown, another request that evaluated the same item
        cannot increment ``surfaced_count`` during the same window.
        """
        surfaced_ts = float(time.time() if surfaced_ts is None else surfaced_ts)
        cooldown_until = surfaced_ts + max(0.0, float(cooldown_seconds))
        with self._lock:
            conn = self._get_conn()
            before = conn.execute(
                "SELECT * FROM prospective_memory_items WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            if not before:
                raise KeyError("prospective item not found")
            cur = conn.execute(
                """UPDATE prospective_memory_items
                   SET last_surfaced_ts=?, cooldown_until=?,
                       surfaced_count=surfaced_count+1, updated_ts=?
                   WHERE item_id=?
                     AND status NOT IN ('resolved','expired','uncertain')
                     AND cooldown_until<=?""",
                (surfaced_ts, cooldown_until, surfaced_ts, str(item_id), surfaced_ts),
            )
            after = conn.execute(
                "SELECT * FROM prospective_memory_items WHERE item_id=?",
                (str(item_id),),
            ).fetchone()
            conn.commit()
        return {"updated": bool(cur.rowcount), "item": self.decode_prospective(after)}

    def refresh_prospective_lifecycle(self, scope_id: str, *, now_ts: float,
                                      expiry_grace_days: int = 30) -> dict[str, int]:
        """Advance derived lifecycle state without declaring elapsed items resolved."""
        now_ts = float(now_ts)
        expiry_grace = max(1, int(expiry_grace_days)) * 86400.0
        changed = {"due": 0, "resumed": 0, "expired": 0, "source_closed": 0}
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT p.*,c.status AS claim_status,c.object AS claim_object
                   FROM prospective_memory_items p
                   LEFT JOIN memory_claims c ON c.claim_id=p.source_claim_id
                   WHERE p.scope_id=?""",
                (str(scope_id),),
            ).fetchall()
            for row in rows:
                item = dict(row)
                if str(item.get("manual_status") or "") in {"resolved", "expired", "uncertain"}:
                    continue
                old_status = str(item.get("status") or "pending")
                status = old_status
                reason = str(item.get("status_reason") or "")
                resolution: dict[str, Any] = _loads(item.get("resolution_evidence_json"), {})
                claim_status = str(item.get("claim_status") or "")
                if not str(item.get("manual_status") or "") and claim_status in {
                    "resolved", "broken", "superseded", "historical", "rejected"
                }:
                    status = "resolved" if claim_status in {"resolved", "broken"} else "expired"
                    reason = f"source_claim_{claim_status}"
                    resolution = {
                        "source_claim_id": str(item.get("source_claim_id") or ""),
                        "claim_status": claim_status,
                        "claim_text": str(item.get("claim_object") or "")[:500],
                    }
                    changed["source_closed"] += int(status != old_status)
                elif old_status == "snoozed" and float(item.get("cooldown_until") or 0) <= now_ts:
                    status = "pending"
                    reason = "snooze_elapsed"
                    changed["resumed"] += 1
                due_start = float(item.get("due_start") or 0)
                due_end = float(item.get("due_end") or 0)
                if status in {"pending", "due"} and due_start:
                    if due_start <= now_ts <= max(due_start, due_end):
                        status = "due"
                        reason = "inside_due_window"
                        changed["due"] += int(old_status != "due")
                    elif due_end and now_ts > due_end + expiry_grace:
                        status = "expired"
                        reason = "due_window_elapsed_unresolved"
                        changed["expired"] += int(old_status != "expired")
                    elif now_ts < due_start:
                        status = "pending"
                if status != old_status or reason != str(item.get("status_reason") or ""):
                    conn.execute(
                        """UPDATE prospective_memory_items SET status=?,status_reason=?,
                           resolution_evidence_json=?,
                           manual_status=CASE WHEN ?='snooze_elapsed' THEN '' ELSE manual_status END,
                           updated_ts=? WHERE item_id=?""",
                        (status, reason, _json(resolution), reason, time.time(), str(item["item_id"])),
                    )
            conn.commit()
        return changed

    def record_prospective_observation(self, data: dict[str, Any]) -> int:
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                """INSERT INTO prospective_trigger_observations(request_id,scope_id,query_text,
                   item_id,decision,score,routes_json,reason,shadow,created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (str(data.get("request_id") or ""), str(data.get("scope_id") or "default"),
                 str(data.get("query_text") or "")[:500], str(data.get("item_id") or ""),
                 str(data.get("decision") or "not_triggered"), float(data.get("score") or 0),
                 _json(data.get("routes") or {}), str(data.get("reason") or ""),
                 int(bool(data.get("shadow", True))), time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)

    # ------------------------------------------------------------------ #
    #  test5 retrieval lab and evaluation                                #
    # ------------------------------------------------------------------ #

    def record_thread_query_observation(self, data: dict[str, Any]) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR REPLACE INTO thread_query_observations(
                   request_id,scope_id,thread_ids_json,subgraph_json,injected_claims_json,
                   injected_prospective_json,shadow,query_text,evidence_json,query_plan_json,
                   route_results_json,dedup_json,injection_preview,metrics_json,latency_ms,created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(data.get("request_id") or uuid.uuid4().hex), str(data.get("scope_id") or "default"),
                 _json(data.get("thread_ids") or []), _json(data.get("subgraph") or {}),
                 _json(data.get("claims") or []), _json(data.get("prospective") or []),
                 int(bool(data.get("shadow", True))),
                 str(data.get("query_text") or "")[:1000], _json(data.get("evidence") or []), _json(data.get("query_plan") or {}),
                 _json(data.get("routes") or {}), _json(data.get("dedup") or {}),
                 str(data.get("injection_preview") or "")[:20000], _json(data.get("metrics") or {}),
                 float(data.get("latency_ms") or 0), time.time()),
            )
            conn.commit()

    def finalize_thread_query_observation(
        self,
        request_id: str,
        *,
        injected: bool,
        append_status: str,
        actual_text: str = "",
        actual_evidence: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Confirm the request mutation after the temporary part was appended."""
        clean_id = str(request_id or "").strip()
        if not clean_id:
            return False
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT metrics_json FROM thread_query_observations WHERE request_id=?",
                (clean_id,),
            ).fetchone()
            if row is None:
                return False
            metrics = _loads(row["metrics_json"], {})
            if not isinstance(metrics, dict):
                metrics = {}
            metrics.update({
                "injected": bool(injected),
                "append_confirmed": bool(injected),
                "append_status": str(append_status or "unknown")[:64],
                "actual_chars": len(str(actual_text or "")) if injected else 0,
            })
            if not injected and str(append_status or "") in {"append_failed", "observation_failed"}:
                metrics.update({"outcome": "fail_open", "reason": str(append_status)})
            conn.execute(
                """UPDATE thread_query_observations
                   SET metrics_json=?, shadow=?,
                       evidence_json=CASE WHEN ? THEN ? ELSE evidence_json END,
                        injection_preview=CASE WHEN ? THEN ? ELSE injection_preview END
                   WHERE request_id=?""",
                 (
                     _json(metrics), int(not bool(injected)),
                     int(bool(injected)), _json(actual_evidence or []),
                     int(bool(injected)), str(actual_text or "")[:20000], clean_id,
                 ),
            )
            conn.commit()
            return True

    def list_thread_query_observations(self, *, scope_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        conn = self._get_conn()
        if scope_id:
            rows = conn.execute(
                "SELECT * FROM thread_query_observations WHERE scope_id=? ORDER BY created_ts DESC LIMIT ?",
                (str(scope_id), max(1, min(500, int(limit)))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM thread_query_observations ORDER BY created_ts DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            for source, target, fallback in (
                ("thread_ids_json", "thread_ids", []), ("subgraph_json", "subgraph", {}),
                ("injected_claims_json", "claims", []),
                ("injected_prospective_json", "prospective", []),
                ("evidence_json", "evidence", []),
                ("query_plan_json", "query_plan", {}), ("route_results_json", "routes", {}),
                ("dedup_json", "dedup", {}), ("metrics_json", "metrics", {}),
            ):
                item[target] = _loads(item.pop(source, ""), fallback)
            output.append(item)
        return output

    def upsert_thread_eval_case(self, case: dict[str, Any]) -> str:
        case_id = str(case.get("case_id") or "eval_" + uuid.uuid4().hex[:20])
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO thread_eval_cases(case_id,scope_id,case_type,query,
                   expected_thread_ids_json,expected_claims_json,enabled,notes,created_ts,updated_ts,
                   expected_episode_ids_json,expected_prospective_ids_json,expected_order_json,source,confidence)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET
                   scope_id=excluded.scope_id,case_type=excluded.case_type,query=excluded.query,
                   expected_thread_ids_json=excluded.expected_thread_ids_json,
                   expected_claims_json=excluded.expected_claims_json,enabled=excluded.enabled,
                   notes=excluded.notes,updated_ts=excluded.updated_ts,
                   expected_episode_ids_json=excluded.expected_episode_ids_json,
                   expected_prospective_ids_json=excluded.expected_prospective_ids_json,
                   expected_order_json=excluded.expected_order_json,source=excluded.source,
                   confidence=excluded.confidence""",
                (case_id, str(case.get("scope_id") or "default"), str(case.get("case_type") or "general"),
                 str(case.get("query") or ""), _json(case.get("expected_thread_ids") or []),
                 _json(case.get("expected_claim_ids") or []), int(bool(case.get("enabled", True))),
                 str(case.get("notes") or ""), now, now, _json(case.get("expected_episode_ids") or []),
                 _json(case.get("expected_prospective_ids") or []), _json(case.get("expected_order") or []),
                 str(case.get("source") or "manual"), float(case.get("confidence") or 1.0)),
            )
            conn.commit()
        return case_id

    def list_thread_eval_cases(self, *, scope_id: str = "", enabled_only: bool = False,
                               limit: int = 500) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if scope_id:
            clauses.append("scope_id=?")
            params.append(str(scope_id))
        if enabled_only:
            clauses.append("enabled=1")
        rows = self._get_conn().execute(
            f"SELECT * FROM thread_eval_cases WHERE {' AND '.join(clauses)} ORDER BY updated_ts DESC LIMIT ?",
            (*params, max(1, min(5000, int(limit)))),
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            for source, target in (
                ("expected_thread_ids_json", "expected_thread_ids"),
                ("expected_claims_json", "expected_claim_ids"),
                ("expected_episode_ids_json", "expected_episode_ids"),
                ("expected_prospective_ids_json", "expected_prospective_ids"),
                ("expected_order_json", "expected_order"),
            ):
                item[target] = _loads(item.pop(source, "[]"), [])
            output.append(item)
        return output

    def prune_auto_eval_cases(self, scope_id: str, keep_case_ids: set[str]) -> int:
        """Keep deterministic holdouts bounded without touching manual cases."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT case_id FROM thread_eval_cases
               WHERE scope_id=? AND source='deterministic_source_holdout'""",
            (str(scope_id),),
        ).fetchall()
        stale = [str(row["case_id"]) for row in rows if str(row["case_id"]) not in keep_case_ids]
        if not stale:
            return 0
        with self._lock:
            placeholders = ",".join("?" for _ in stale)
            conn.execute(f"DELETE FROM thread_eval_cases WHERE case_id IN ({placeholders})", tuple(stale))
            conn.commit()
        return len(stale)

    def record_thread_eval_run(self, algorithm_version: str, config: dict[str, Any],
                               results: dict[str, Any], cases_total: int, cases_passed: int) -> str:
        run_id = "thr_eval_" + uuid.uuid4().hex[:20]
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO thread_eval_runs(run_id,algorithm_version,config_json,results_json,
                   cases_total,cases_passed,started_ts,finished_ts) VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, str(algorithm_version), _json(config), _json(results),
                 int(cases_total), int(cases_passed), now, now),
            )
            conn.commit()
        return run_id

    def record_manual_feedback(self, *, scope_id: str, target_type: str, target_id: str,
                               action: str, note: str = "", operator: str = "webui") -> int:
        allowed_actions = {
            "should_hit", "missed", "wrong_link", "current_state_wrong",
            "should_not_surface", "correct", "uncertain",
        }
        if action not in allowed_actions:
            raise ValueError("invalid feedback action")
        if target_type not in {"query_observation", "thread", "claim", "prospective", "episode"}:
            raise ValueError("invalid feedback target")
        if not str(target_id or "").strip():
            raise ValueError("feedback target_id is required")
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                """INSERT INTO thread_manual_feedback(scope_id,target_type,target_id,action,
                   note,operator,reversible,created_ts) VALUES(?,?,?,?,?,?,?,?)""",
                (str(scope_id or "default"), str(target_type), str(target_id), str(action),
                 str(note or "")[:1000], str(operator or "webui"), 1, time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_manual_feedback(self, *, scope_id: str = "", target_id: str = "",
                             limit: int = 200) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if scope_id:
            clauses.append("scope_id=?")
            params.append(str(scope_id))
        if target_id:
            clauses.append("target_id=?")
            params.append(str(target_id))
        rows = self._get_conn().execute(
            f"SELECT * FROM thread_manual_feedback WHERE {' AND '.join(clauses)} "
            "ORDER BY created_ts DESC,id DESC LIMIT ?",
            (*params, max(1, min(5000, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    #  reversible manual corrections                                      #
    # ------------------------------------------------------------------ #

    def manual_edge_decision(self, edge_id: int, decision: dict[str, Any], *,
                             operator: str = "webui") -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            before = conn.execute("SELECT * FROM memory_episode_edges WHERE id=?", (int(edge_id),)).fetchone()
            if not before:
                raise KeyError("edge not found")
            updated = dict(before)
            updated.update({
                "edge_type": str(decision.get("relation") or before["edge_type"]),
                "direction": str(decision.get("direction") or before["direction"]),
                "status": str(decision.get("decision") or before["status"]),
                "confidence": float(decision.get("confidence", before["confidence"])),
                "manual_lock": 1,
                "manual_override": True,
                "decision_source": "manual",
                "decision_json": _json(decision),
                "updated_ts": time.time(),
            })
            self.upsert_edge(updated)
            after = conn.execute("SELECT * FROM memory_episode_edges WHERE id=?", (int(edge_id),)).fetchone()
            operation_id = self._record_operation(conn, str(before["scope_id"]), "edge_decision",
                                                  {"edge": dict(before)}, {"edge": dict(after)}, operator)
            conn.commit()
        return {"operation_id": operation_id, "edge": dict(after)}

    def _record_operation(self, conn: Any, scope_id: str, operation_type: str,
                          before: dict[str, Any], after: dict[str, Any], operator: str) -> str:
        operation_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO thread_operations(operation_id,scope_id,operation_type,status,
               before_json,after_json,operator,created_ts) VALUES(?,?,?,?,?,?,?,?)""",
            (operation_id, str(scope_id), str(operation_type), "applied",
             _json(before), _json(after), str(operator), time.time()),
        )
        return operation_id

    def list_operations(self, *, scope_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        conn = self._get_conn()
        if scope_id:
            rows = conn.execute(
                "SELECT * FROM thread_operations WHERE scope_id=? ORDER BY created_ts DESC LIMIT ?",
                (str(scope_id), max(1, min(500, int(limit)))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM thread_operations ORDER BY created_ts DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def merge_preview(self, thread_ids: list[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item) for item in thread_ids if str(item)))
        if len(ids) < 2:
            raise ValueError("select at least two threads")
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in ids)
        threads = [dict(row) for row in conn.execute(
            f"SELECT * FROM memory_threads WHERE thread_id IN ({placeholders})", tuple(ids)
        ).fetchall()]
        if len(threads) != len(ids):
            raise KeyError("thread not found")
        scopes = {str(item["scope_id"]) for item in threads}
        if len(scopes) != 1:
            raise ValueError("cross-scope merge is forbidden")
        members = [dict(row) for row in conn.execute(
            f"SELECT * FROM memory_thread_members WHERE thread_id IN ({placeholders}) ORDER BY thread_id,sequence_no",
            tuple(ids),
        ).fetchall()]
        return {
            "scope_id": next(iter(scopes)), "thread_ids": ids,
            "target_thread_id": ids[0], "threads": threads, "members": members,
            "versions": {str(item["thread_id"]): int(item.get("materialized_version") or 0) for item in threads},
            "unique_episode_count": len({str(item["episode_id"]) for item in members}),
            "manual_lock_count": sum(int(item["manual_lock"] or 0) for item in members),
        }

    @_atomic_write
    def apply_merge(self, thread_ids: list[str], *, expected_versions: dict[str, Any] | None = None,
                    operator: str = "webui") -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item) for item in thread_ids if str(item)))
        if len(ids) < 2:
            raise ValueError("select at least two threads")
        with self._lock:
            # Preview, version check, snapshot, and mutation share one lock so a
            # stale UI preview cannot be mixed with a newer membership snapshot.
            preview = self.merge_preview(ids)
            expected = expected_versions if expected_versions is not None else preview.get("versions") or {}
            current = {
                str(item["thread_id"]): int(item.get("materialized_version") or 0)
                for item in preview["threads"]
            }
            for thread_id in ids:
                if thread_id not in current:
                    raise KeyError("thread not found")
                if thread_id not in expected:
                    raise ThreadVersionConflict(
                        "thread merge version missing",
                        thread_id=thread_id,
                        current_version=current[thread_id],
                    )
                try:
                    expected_version = int(expected[thread_id])
                except (TypeError, ValueError):
                    raise ValueError("invalid expected thread version")
                if expected_version != current[thread_id]:
                    raise ThreadVersionConflict(
                        "thread merge version conflict",
                        thread_id=thread_id,
                        current_version=current[thread_id],
                    )
            if any(str(item.get("status") or "active") != "active" for item in preview["threads"]):
                raise ThreadVersionConflict("thread merge target is no longer active", thread_id=ids[0],
                                            current_version=current[ids[0]])
            target = str(preview["target_thread_id"])
            before = self._thread_snapshot(ids)
            conn = self._get_conn()
            target_row = next(item for item in preview["threads"] if str(item["thread_id"]) == target)
            episodes: dict[str, dict[str, Any]] = {}
            for item in preview["members"]:
                episode_id = str(item["episode_id"])
                existing = episodes.get(episode_id)
                if existing is None or float(item["membership_confidence"] or 0) > float(existing["membership_confidence"] or 0):
                    episodes[episode_id] = item
            conn.execute("DELETE FROM memory_thread_members WHERE thread_id=? AND manual_lock=0", (target,))
            for item in episodes.values():
                conn.execute(
                    """INSERT INTO memory_thread_members(thread_id,episode_id,role,sequence_no,
                       membership_confidence,evidence_json,decision_source,manual_lock,created_ts)
                       VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(thread_id,episode_id) DO UPDATE SET
                       role=excluded.role,sequence_no=excluded.sequence_no,
                       membership_confidence=excluded.membership_confidence,
                       evidence_json=excluded.evidence_json,decision_source='manual_merge',manual_lock=1""",
                    (target, str(item["episode_id"]), str(item["role"]), int(item["sequence_no"]),
                     float(item["membership_confidence"]), str(item["evidence_json"]),
                     "manual_merge", 1, time.time()),
                )
            for source in ids[1:]:
                conn.execute(
                    "UPDATE memory_threads SET status='closed',materialized_version=materialized_version+1,updated_ts=? WHERE thread_id=? AND materialized_version=?",
                    (time.time(), source, current[source]),
                )
            conn.execute(
                """UPDATE memory_threads SET materialized_version=materialized_version+1,
                   confidence=?,first_event_ts=?,last_event_ts=?,updated_ts=? WHERE thread_id=?""",
                (max(float(item["confidence"] or 0) for item in preview["threads"]),
                 min((float(item["first_event_ts"] or 0) for item in preview["threads"] if float(item["first_event_ts"] or 0) > 0), default=0),
                 max(float(item["last_event_ts"] or 0) for item in preview["threads"]), time.time(), target),
            )
            conn.execute(f"UPDATE memory_threads SET manual_lock=1 WHERE thread_id IN ({','.join('?' for _ in ids)})", tuple(ids))
            after = self._thread_snapshot(ids)
            operation_id = self._record_operation(conn, str(target_row["scope_id"]), "merge", before, after, operator)
            conn.commit()
        detail = self.thread_detail(target)
        return {"operation_id": operation_id, "thread": detail["thread"] if detail else {}}

    def split_preview(self, thread_id: str, episode_ids: list[str]) -> dict[str, Any]:
        detail = self.thread_detail(str(thread_id))
        if not detail:
            raise KeyError("thread not found")
        selected = set(str(item) for item in episode_ids if str(item))
        members = detail["members"]
        existing = {str(item["episode_id"]) for item in members}
        if not selected or not selected < existing:
            raise ValueError("split must move a non-empty proper subset")
        return {
            "thread": detail["thread"],
            "version": int(detail["thread"].get("materialized_version") or 0),
            "moving": [item for item in members if str(item["episode_id"]) in selected],
            "remaining": [item for item in members if str(item["episode_id"]) not in selected],
            "manual_lock_count": sum(int(item["manual_lock"] or 0) for item in members if str(item["episode_id"]) in selected),
        }

    @_atomic_write
    def apply_split(self, thread_id: str, episode_ids: list[str], *, expected_version: int | None = None,
                    operator: str = "webui") -> dict[str, Any]:
        preview = self.split_preview(thread_id, episode_ids)
        if expected_version is None:
            expected_version = preview.get("version")
        old_thread = dict(preview["thread"])
        new_id = f"thr_manual_{uuid.uuid4().hex[:16]}"
        with self._lock:
            conn = self._get_conn()
            current = conn.execute(
                "SELECT materialized_version,status FROM memory_threads WHERE thread_id=?",
                (str(thread_id),),
            ).fetchone()
            if not current or str(current["status"] or "active") != "active":
                raise KeyError("thread not found or inactive")
            try:
                expected_version = int(expected_version)
            except (TypeError, ValueError):
                raise ValueError("invalid expected thread version")
            current_version = int(current["materialized_version"] or 0)
            if expected_version != current_version:
                raise ThreadVersionConflict("thread split version conflict", thread_id=str(thread_id),
                                            current_version=current_version)
            before = self._thread_snapshot([str(thread_id), new_id])
            now = time.time()
            conn.execute(
                """INSERT INTO memory_threads(thread_id,scope_id,thread_type,title,status,confidence,
                   first_event_ts,last_event_ts,materialized_version,source_quality_floor,created_ts,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id, str(old_thread["scope_id"]), str(old_thread["thread_type"]),
                 f"{old_thread['title']} - 分支", "active", float(old_thread["confidence"]),
                 0, 0, 1, str(old_thread["source_quality_floor"]), now, now),
            )
            for item in preview["moving"]:
                conn.execute(
                    """UPDATE memory_thread_members SET thread_id=?,decision_source='manual_split',
                       manual_lock=1 WHERE thread_id=? AND episode_id=?""",
                    (new_id, str(thread_id), str(item["episode_id"])),
                )
            conn.execute(
                "UPDATE memory_threads SET materialized_version=materialized_version+1,updated_ts=? WHERE thread_id=?",
                (now, str(thread_id)),
            )
            conn.execute("UPDATE memory_threads SET manual_lock=1 WHERE thread_id IN (?,?)", (str(thread_id), new_id))
            after = self._thread_snapshot([str(thread_id), new_id])
            operation_id = self._record_operation(conn, str(old_thread["scope_id"]), "split", before, after, operator)
            conn.commit()
        return {"operation_id": operation_id, "source_thread_id": str(thread_id), "new_thread_id": new_id}

    def _thread_snapshot(self, thread_ids: list[str]) -> dict[str, Any]:
        conn = self._get_conn()
        ids = list(dict.fromkeys(str(item) for item in thread_ids if str(item)))
        if not ids:
            return {"threads": [], "members": [], "replace_member_thread_ids": []}
        placeholders = ",".join("?" for _ in ids)
        threads = [dict(row) for row in conn.execute(
            f"SELECT * FROM memory_threads WHERE thread_id IN ({placeholders})", tuple(ids)
        ).fetchall()]
        members = [dict(row) for row in conn.execute(
            f"SELECT * FROM memory_thread_members WHERE thread_id IN ({placeholders})", tuple(ids)
        ).fetchall()]
        existing_ids = {str(item["thread_id"]) for item in threads}
        return {"threads": threads, "members": members, "replace_member_thread_ids": ids,
                "delete_thread_ids": [item for item in ids if item not in existing_ids]}

    def revert_operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM thread_operations WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
            if not row:
                raise KeyError("operation not found")
            if str(row["status"]) != "applied":
                raise RuntimeError("operation already reverted")
            before = _loads(row["before_json"], {})
            if str(row["operation_type"]) == "edge_decision" and isinstance(before.get("edge"), dict):
                payload = dict(before["edge"])
                payload["manual_override"] = True
                self.upsert_edge(payload)
            elif str(row["operation_type"]) == "claim_status" and isinstance(before.get("claim"), dict):
                payload = dict(before["claim"])
                columns = list(payload)
                conn.execute(
                    f"INSERT OR REPLACE INTO memory_claims({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(payload[column] for column in columns),
                )
            elif str(row["operation_type"]) == "prospective_status" and isinstance(before.get("prospective"), dict):
                payload = dict(before["prospective"])
                columns = list(payload)
                conn.execute(
                    f"INSERT OR REPLACE INTO prospective_memory_items({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(payload[column] for column in columns),
                )
            else:
                after = _loads(row["after_json"], {})
                expected_ids = [
                    str(item.get("thread_id")) for item in (after.get("threads") or [])
                    if isinstance(item, dict) and item.get("thread_id")
                ]
                expected_ids.extend(str(item) for item in (after.get("replace_member_thread_ids") or []))
                expected_ids = list(dict.fromkeys(expected_ids))
                if expected_ids:
                    current = self._thread_snapshot(expected_ids)
                    if _json(current) != _json(after):
                        raise ThreadVersionConflict("thread revert state conflict", thread_id=expected_ids[0])
                    current_versions = {
                        str(item.get("thread_id")): int(item.get("materialized_version") or 0)
                        for item in (current.get("threads") or []) if isinstance(item, dict)
                    }
                    self._restore_thread_snapshot(conn, before)
                    for thread_id, current_version in current_versions.items():
                        conn.execute(
                            "UPDATE memory_threads SET materialized_version=? WHERE thread_id=? AND materialized_version<?",
                            (current_version + 1, thread_id, current_version + 1),
                        )
                else:
                    self._restore_thread_snapshot(conn, before)
            conn.execute(
                "UPDATE thread_operations SET status='reverted',reverted_ts=? WHERE operation_id=?",
                (time.time(), str(operation_id)),
            )
            conn.commit()
        return {"operation_id": str(operation_id), "status": "reverted"}

    @staticmethod
    def _restore_thread_snapshot(conn: Any, snapshot: dict[str, Any]) -> None:
        for thread_id in snapshot.get("delete_thread_ids", []):
            conn.execute("DELETE FROM memory_threads WHERE thread_id=?", (str(thread_id),))
        for thread in snapshot.get("threads", []):
            values = dict(thread)
            columns = list(values)
            conn.execute(
                f"INSERT OR REPLACE INTO memory_threads({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        for thread_id in snapshot.get("replace_member_thread_ids", []):
            conn.execute("DELETE FROM memory_thread_members WHERE thread_id=?", (str(thread_id),))
        for member in snapshot.get("members", []):
            values = dict(member)
            columns = list(values)
            conn.execute(
                f"INSERT OR REPLACE INTO memory_thread_members({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
