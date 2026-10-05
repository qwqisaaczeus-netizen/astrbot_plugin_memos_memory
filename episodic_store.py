"""Persistent episodic memory and source-evidence store (4.6.0-test2 facade).

This module is the public facade that 4.5.4 callers import. Internal logic
lives in SourceArchive / EpisodeRepo / VectorGeneration (see those modules).
Semantic-state machinery is kept inline because it is orthogonal to the new
layering and was already stable in 4.5.4.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .archive_guard import ArchiveGuard
from .episode_repo import EpisodeRepo, SUPPORTED_TIERS, normalize_tier
from .memory_access import MemoryAccessRepository
from .thread_store import ThreadStore
from .consistency_guard import evaluate as evaluate_consistency
from .consistency_service import ConsistencyService
from .preview_store import PreviewStore
from .source_archive import SourceArchive
from .store_utils import (
    cosine as _cosine,
    deserialize_f32 as _deserialize_f32,
    escape_like as _escape_like,
    extract_terms as _terms,
    json_list as _json_list,
    serialize_f32 as _serialize_f32,
)
from .traceability import compute_traceability
from .vector_generation import VectorGeneration, _gen_id

# Backward-compatible public helper used by migration tests/tooling.
_generation_id = _gen_id


logger = logging.getLogger(__name__)


class EpisodicStore:
    """Facade (4.6.0-test2): forwards to SourceArchive + EpisodeRepo +
    VectorGeneration. Semantic-state machinery is kept inline because it is
    orthogonal to the layering and stable since 4.5.4.
    """

    SCHEMA_VERSION = 15

    def __init__(self, db_path: str, emb_dim: Optional[int], emb_model_id: Optional[str],
                 *, consistency_mode: str = "shadow", consistency_queue_capacity: int = 64,
                 consistency_timeout: float = 0.25, consistency_ttl: float = 900.0,
                 consistency_llm_enable: bool = False, consistency_llm=None,
                 consistency_loop=None):
        self.db_path = db_path
        self.emb_dim = int(emb_dim) if emb_dim else None
        self.emb_model_id = str(emb_model_id or "unknown")
        self.consistency_mode = str(consistency_mode or "shadow")
        self.consistency_queue_capacity = int(consistency_queue_capacity or 64)
        self.consistency_timeout = float(consistency_timeout or 0.25)
        self.consistency_ttl = float(consistency_ttl or 900.0)
        self.consistency_llm_enable = bool(consistency_llm_enable)
        self.consistency_llm = consistency_llm
        self.consistency_loop = consistency_loop
        self._conn: sqlite3.Connection | None = None
        self._vec_ok = False
        self._lock = threading.RLock()
        self._gen: VectorGeneration | None = None
        self._src: SourceArchive | None = None
        self._eps: EpisodeRepo | None = None
        self._guard: ArchiveGuard | None = None
        self._previews: PreviewStore | None = None
        self._access: MemoryAccessRepository | None = None
        self._threads: ThreadStore | None = None
        self.consistency_service: ConsistencyService | None = None
        self._snapshot_keep = 3
        self._preview_keep = 20

    async def init(self) -> None:
        self._connect()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._closing = False
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.enable_load_extension(True)
            import sqlite_vec  # type: ignore
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._vec_ok = True
        except Exception as exc:
            self._vec_ok = False
            logger.info("[memos-memory][episode] sqlite-vec unavailable, Python cosine fallback: %s", exc)
        self._conn = conn
        # Delegates use `self._connect()` (not the closed-over `conn`) so a
        # post-restore reopen still sees the live connection.
        self._gen = VectorGeneration(self._vec_ok, self._connect)
        self._src = SourceArchive(self.db_path, self._gen, self._connect, self._lock)
        self._eps = EpisodeRepo(self._gen, self._connect, self._lock)
        self._guard = ArchiveGuard(self.db_path, self._connect, self._lock,
                                    snapshot_keep=self._snapshot_keep)
        self._previews = PreviewStore(self._connect, self._lock, keep=self._preview_keep)
        self._access = MemoryAccessRepository(self._connect, self._lock)
        self._threads = ThreadStore(self._connect, self._lock)
        # Capture an older schema before any Repository CREATE/ALTER statement.
        pre_migration = self._guard.capture_pre_migration(conn, self.SCHEMA_VERSION)
        self._init_schema(conn, pre_migration=pre_migration)
        self._ensure_embedding_contract(conn)
        self.consistency_service = ConsistencyService(
            self,
            evaluator=evaluate_consistency,
            capacity=self.consistency_queue_capacity,
            timeout=self.consistency_timeout,
            mode=self.consistency_mode,
            ttl=self.consistency_ttl,
            llm_enable=self.consistency_llm_enable,
            llm_callback=self.consistency_llm,
            loop=self.consistency_loop,
        )
        return conn

    def _init_schema(self, conn: sqlite3.Connection,
                     pre_migration: dict[str, Any] | None = None) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        assert self._guard is not None
        self._guard.init_schema(conn)
        self._src.init_schema(conn)
        self._eps.init_schema(conn)
        assert self._access is not None
        self._access.init_schema(conn)
        assert self._threads is not None
        self._threads.init_schema(conn)
        assert self._previews is not None
        self._previews.init_schema(conn)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS semantic_states (
                scope_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 0,
                relationship_position TEXT DEFAULT '',
                commitments_boundaries TEXT DEFAULT '',
                behavior_tendencies TEXT DEFAULT '',
                emotional_baseline TEXT DEFAULT '',
                open_loops TEXT DEFAULT '',
                rendered_text TEXT NOT NULL DEFAULT '',
                source_batch_id TEXT,
                source_episode_ids_json TEXT DEFAULT '[]',
                updated_ts REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(source_batch_id) REFERENCES source_batches(batch_id) ON DELETE SET NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS semantic_state_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                reason TEXT DEFAULT '',
                source_batch_id TEXT,
                created_ts REAL NOT NULL,
                UNIQUE(scope_id, version),
                FOREIGN KEY(source_batch_id) REFERENCES source_batches(batch_id) ON DELETE SET NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS semantic_state_queue (
                batch_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                episode_ids_json TEXT DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES source_batches(batch_id) ON DELETE CASCADE
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_versions_scope ON semantic_state_versions(scope_id, version DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_queue_status ON semantic_state_queue(status, updated_ts)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_state_queue_scope_status "
            "ON semantic_state_queue(scope_id, status, created_ts)"
        )
        # 8.3.A: identity; 8.3.B: register the backup captured before Repository DDL.
        assert self._guard is not None
        self._guard.ensure_identity(conn, self.SCHEMA_VERSION)
        self._guard.register_pre_migration(conn, pre_migration, self.SCHEMA_VERSION)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(self.SCHEMA_VERSION),),
        )
        conn.commit()

    def _ensure_embedding_contract(self, conn: sqlite3.Connection) -> None:
        assert self._gen is not None
        self._gen.prepare_active(self.emb_model_id, int(self.emb_dim or 0))

    async def ensure_dim(self, dim: int, model_id: str) -> None:
        with self._lock:
            self.emb_dim = int(dim)
            self.emb_model_id = str(model_id or "unknown")
            self._ensure_embedding_contract(self._connect())

    @staticmethod
    def episode_id_for_memo(memo_name: str) -> str:
        return EpisodeRepo.episode_id_for_memo(memo_name)

    # ---- archive / source forwards (3.1) ------------------------------

    def archive_batch(self, session_id: str, messages: list[dict[str, Any]], source_kind: str) -> str:
        if self._src is None:
            self._connect()
        assert self._src is not None
        return self._src.archive_batch(session_id, messages, source_kind)

    def mark_batch(
        self,
        batch_id: str,
        status: str,
        *,
        error: str = "",
        retry_after: float = 0.0,
        increment_attempt: bool = False,
    ) -> None:
        assert self._src is not None
        self._src.mark_batch(
            batch_id,
            status,
            error=error,
            retry_after=retry_after,
            increment_attempt=increment_attempt,
        )

    def source_turns(self, batch_id: str) -> list[dict[str, Any]]:
        assert self._src is not None
        return self._src.source_turns(batch_id)

    def source_turn_embedding_rows(self, *, batch_id: str = "", after_id: int = 0,
                                   limit: int = 64, missing_only: bool = True,
                                   target_generation: str = "") -> list[dict[str, Any]]:
        assert self._src is not None
        return self._src.turn_embedding_rows(
            batch_id=batch_id, after_id=after_id, limit=limit,
            missing_only=missing_only, target_generation=target_generation,
        )

    def source_turn_chunk_rows(self, *, after_id: int = 0, limit: int = 64,
                               missing_only: bool = True,
                               target_generation: str = "") -> list[dict[str, Any]]:
        assert self._src is not None
        return self._src.chunk_embedding_rows(
            after_id=after_id, limit=limit, missing_only=missing_only,
            target_generation=target_generation,
        )

    def add_turn_chunks(self, turn_id: int, chunks: list[str]) -> int:
        assert self._src is not None
        return self._src.add_turn_chunks(turn_id, chunks)

    def turn_chunks_for_turn(self, turn_id: int) -> list[dict[str, Any]]:
        assert self._src is not None
        return self._src.turn_chunks_for_turn(turn_id)

    def replace_source_turn_embeddings(self, rows: list[tuple[int, list[float]]],
                                        runtime_gen: str = "") -> int:
        assert self._src is not None and self._gen is not None
        runtime_gen = runtime_gen or self._gen.runtime_generation(
            self.emb_model_id, int(self.emb_dim or 0)
        )
        return self._src.replace_turn_embeddings(rows, runtime_gen=runtime_gen)

    def replace_source_chunk_embeddings(self, rows: list[tuple[int, list[float]]],
                                        runtime_gen: str = "") -> int:
        assert self._src is not None and self._gen is not None
        runtime_gen = runtime_gen or self._gen.runtime_generation(
            self.emb_model_id, int(self.emb_dim or 0)
        )
        return self._src.replace_chunk_embeddings(rows, runtime_gen=runtime_gen)

    def source_turn_vector_count(self) -> int:
        assert self._src is not None
        return int(self._src.counts().get("turn_vectors") or 0)

    def source_chunk_vector_count(self) -> int:
        assert self._src is not None
        return int(self._src.counts().get("chunk_vectors") or 0)

    def episode_card_embedding_rows(self, *, after_id: int = 0, limit: int = 64,
                                    target_generation: str = "") -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.card_embedding_rows(
            after_id=after_id, limit=limit, target_generation=target_generation,
        )

    def replace_episode_card_embeddings(self, rows: list[tuple[int, list[float]]],
                                        runtime_gen: str = "") -> int:
        assert self._eps is not None and self._gen is not None
        runtime_gen = runtime_gen or self._gen.runtime_generation(
            self.emb_model_id, int(self.emb_dim or 0)
        )
        return self._eps.replace_card_embeddings(rows, runtime_gen)

    # ---- episode forwards (3.2 / 8.2) ---------------------------------

    def upsert_episode(
        self, *,
        memo_name: str, episode: dict[str, Any], card_text: str,
        embedding: list[float] | None, source_batch_id: str = "",
        source_kind: str = "legacy", legacy: bool = True,
        evidence_quality: str = "diary_derived", diary_content_hash: str = "",
        source_updated_ts: float = 0.0,
        diary_render_version: str = "", must_coverage: float = -1.0,
        support_coverage: float = -1.0, transcript_risk: float = -1.0,
        render_retry_reason: str = "", original_memo_version: str = "",
        source_overlap_ratio: float = -1.0, direct_quote_ratio: float = -1.0,
        compression_ratio: float = -1.0, render_fallback: bool = False,
        runtime_gen: str = "",
    ) -> str:
        assert self._eps is not None and self._gen is not None
        if embedding and not runtime_gen:
            runtime_gen = self._gen.runtime_generation(
                self.emb_model_id, int(self.emb_dim or 0)
            )
        return self._eps.upsert_episode(
            memo_name=memo_name, episode=episode, card_text=card_text,
            embedding=embedding, source_batch_id=source_batch_id,
            source_kind=source_kind, legacy=legacy, evidence_quality=evidence_quality,
            diary_content_hash=diary_content_hash, source_updated_ts=source_updated_ts,
            diary_render_version=diary_render_version, must_coverage=must_coverage,
            support_coverage=support_coverage, transcript_risk=transcript_risk,
            render_retry_reason=render_retry_reason,
            original_memo_version=original_memo_version,
            source_overlap_ratio=source_overlap_ratio,
            direct_quote_ratio=direct_quote_ratio,
            compression_ratio=compression_ratio,
            render_fallback=render_fallback,
            runtime_gen=runtime_gen,
        )

    def delete_by_memo_name(self, memo_name: str) -> int:
        assert self._eps is not None and self._access is not None
        self._access.delete_memo(memo_name)
        return self._eps.delete_by_memo_name(memo_name)

    # ---- 5.0 memory accessibility forwards --------------------------

    def sync_memory_access(self, episode: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.sync_episode(episode, **kwargs)

    def rebuild_memory_access(self, episodes: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.rebuild(episodes, **kwargs)

    def maintain_memory_access(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.maintain(**kwargs)

    def evaluate_memory_access(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.evaluate(**kwargs)

    def record_memory_response_use(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.record_response_use(**kwargs)

    def memory_access_overview(self) -> dict[str, Any]:
        assert self._access is not None
        return self._access.overview()

    def list_memory_access_states(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.list_states(**kwargs)

    def memory_access_detail(self, memo_name: str) -> dict[str, Any] | None:
        assert self._access is not None
        return self._access.detail(memo_name)

    def list_memory_interference(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.list_edges(**kwargs)

    def list_memory_access_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.list_events(**kwargs)

    def memory_access_observation_summary(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.memory_access_observation_summary(**kwargs)

    def list_memory_access_observations(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.list_memory_access_observations(**kwargs)

    def memory_access_observation_detail(self, request_id: str) -> dict[str, Any] | None:
        assert self._access is not None
        return self._access.memory_access_observation_detail(request_id)

    def record_memory_access_observation_feedback(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.record_memory_access_observation_feedback(**kwargs)

    def memory_access_export_payload(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.memory_access_export_payload(**kwargs)

    def record_memory_access_feedback(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.record_manual_feedback(**kwargs)

    # ---- eligibility / disambiguation / evaluation -------

    def classify_memory_query_cues(self, query: str, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.classify_query_cues(query, **kwargs)

    def disambiguate_same_day_memories(self, query: str, memo_names: list[str],
                                       **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.disambiguate_same_day(query, memo_names, **kwargs)

    def sync_memory_interference_incremental(self, memo_name: str, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.sync_interference_incremental(memo_name, **kwargs)

    def sync_memory_interference_batch(self, memo_names: list[str], **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.sync_interference_batch(memo_names, **kwargs)

    def mark_memory_interference_dirty(self, memo_names: list[str]) -> int:
        assert self._access is not None
        return self._access.mark_interference_dirty(memo_names)

    def pop_memory_interference_dirty(self, limit: int = 50) -> list[str]:
        assert self._access is not None
        return self._access.pop_interference_dirty(limit)

    def memory_access_index_meta(self) -> dict[str, Any]:
        assert self._access is not None
        return self._access.index_meta()

    def generate_memory_eval_cases(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.generate_eval_cases(**kwargs)

    def list_memory_eval_cases(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.list_eval_cases(**kwargs)

    def set_memory_eval_case_enabled(self, case_id: str, enabled: bool) -> bool:
        assert self._access is not None
        return self._access.set_eval_case_enabled(case_id, enabled)

    def run_memory_eval(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.run_eval(**kwargs)

    def latest_memory_eval_run(self) -> dict[str, Any] | None:
        assert self._access is not None
        return self._access.latest_eval_run()

    def memory_eval_run_results(self, run_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.eval_run_results(run_id, **kwargs)

    def memory_access_safety_gates(self) -> dict[str, Any]:
        assert self._access is not None
        return self._access.safety_gates()

    # ---- guarded takeover forwards ----

    def memory_access_takeover_prerequisites(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.takeover_prerequisites(**kwargs)

    def memory_access_breaker_status(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.breaker_status(**kwargs)

    def memory_access_trip_breaker(self, **kwargs: Any) -> None:
        assert self._access is not None
        self._access.trip_breaker(**kwargs)

    def memory_access_reset_breaker(self) -> bool:
        assert self._access is not None
        return self._access.reset_breaker()

    def compute_memory_takeover_appends(self, evaluation: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.compute_takeover_appends(evaluation, **kwargs)

    def compute_memory_supplement_appends(self, evaluation: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.compute_supplement_appends(evaluation, **kwargs)

    def log_memory_takeover_append(self, **kwargs: Any) -> None:
        assert self._access is not None
        self._access.log_takeover_append(**kwargs)

    def list_memory_takeover_log(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._access is not None
        return self._access.list_takeover_log(**kwargs)

    def evaluate_memory_takeover_response_use(self, **kwargs: Any) -> dict[str, Any]:
        assert self._access is not None
        return self._access.evaluate_takeover_response_use(**kwargs)

    def confirm_memory_eval_case(self, case_id: str, **kwargs: Any) -> bool:
        assert self._access is not None
        return self._access.confirm_eval_case(case_id, **kwargs)

    # ---- 6.0 thread-layer forwards ----

    def thread_enqueue(self, scope_id: str, episode_id: str) -> bool:
        assert self._threads is not None
        return self._threads.enqueue(scope_id, episode_id)

    def thread_queue_counts(self) -> dict:
        assert self._threads is not None
        return self._threads.queue_counts()

    def thread_status(self) -> dict:
        assert self._threads is not None
        return self._threads.status()

    def thread_set_paused(self, paused: bool) -> None:
        assert self._threads is not None
        self._threads.set_paused(paused)

    def thread_is_paused(self) -> bool:
        assert self._threads is not None
        return self._threads.is_paused()

    def thread_source_grade_counts(self) -> dict:
        assert self._threads is not None
        return self._threads.source_grade_counts()

    def thread_restore_running_to_pending(self) -> int:
        assert self._threads is not None
        return self._threads.restore_running_to_pending()

    def thread_take_pending_batch(self, limit: int = 10) -> list:
        assert self._threads is not None
        return self._threads.take_pending_batch(limit=limit)

    def thread_mark_completed(self, queue_id: int, **kwargs) -> None:
        assert self._threads is not None
        self._threads.mark_completed(queue_id, **kwargs)

    def thread_list_edges(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_edges(**kwargs)

    def thread_edge_counts(self) -> dict:
        assert self._threads is not None
        return self._threads.edge_counts()

    def thread_list_ambiguity_queue(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_ambiguity_queue(**kwargs)

    def thread_insert_edge(self, edge: dict) -> bool:
        assert self._threads is not None
        return self._threads.insert_edge(edge)

    def thread_list_threads(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_threads(**kwargs)

    def thread_detail(self, thread_id: str) -> dict | None:
        assert self._threads is not None
        return self._threads.thread_detail(thread_id)

    def thread_manual_edge_decision(self, edge_id: int, decision: dict, **kwargs) -> dict:
        assert self._threads is not None
        return self._threads.manual_edge_decision(edge_id, decision, **kwargs)

    def thread_merge_preview(self, thread_ids: list[str]) -> dict:
        assert self._threads is not None
        return self._threads.merge_preview(thread_ids)

    def thread_apply_merge(self, thread_ids: list[str], **kwargs) -> dict:
        assert self._threads is not None
        return self._threads.apply_merge(thread_ids, **kwargs)

    def thread_split_preview(self, thread_id: str, episode_ids: list[str]) -> dict:
        assert self._threads is not None
        return self._threads.split_preview(thread_id, episode_ids)

    def thread_apply_split(self, thread_id: str, episode_ids: list[str], **kwargs) -> dict:
        assert self._threads is not None
        return self._threads.apply_split(thread_id, episode_ids, **kwargs)

    def thread_list_operations(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_operations(**kwargs)

    def thread_revert_operation(self, operation_id: str) -> dict:
        assert self._threads is not None
        return self._threads.revert_operation(operation_id)

    def thread_list_claims(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_claims(**kwargs)

    def thread_list_claim_slots(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_claim_slots(**kwargs)

    def thread_list_claim_transitions(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_claim_transitions(**kwargs)

    def thread_claim_manual_status(self, claim_id: str, status: str, **kwargs) -> dict:
        assert self._threads is not None
        return self._threads.claim_manual_status(claim_id, status, **kwargs)

    def thread_view(self, thread_id: str) -> dict | None:
        assert self._threads is not None
        return self._threads.thread_view(thread_id)

    def thread_view_history(self, thread_id: str, limit: int = 30) -> list:
        assert self._threads is not None
        return self._threads.thread_view_history(thread_id, limit=limit)

    def thread_list_prospective(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_prospective(**kwargs)

    def thread_prospective_manual_status(self, item_id: str, status: str, **kwargs) -> dict:
        assert self._threads is not None
        return self._threads.prospective_manual_status(item_id, status, **kwargs)

    def thread_reserve_prospective_surface(self, item_id: str, **kwargs) -> dict:
        assert self._threads is not None
        return self._threads.reserve_prospective_surface(item_id, **kwargs)

    def thread_commit_prospective_surface(self, item_id: str, reservation: str, **kwargs) -> dict:
        assert self._threads is not None
        return self._threads.commit_prospective_surface(
            item_id, reservation, **kwargs
        )

    def thread_release_prospective_surface(self, item_id: str, reservation: str) -> bool:
        assert self._threads is not None
        return self._threads.release_prospective_surface(
            item_id, reservation
        )

    def thread_record_prospective_surface(self, item_id: str, **kwargs) -> dict:
        assert self._threads is not None
        return self._threads.record_prospective_surface(item_id, **kwargs)

    def thread_query_observations(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_thread_query_observations(**kwargs)

    def thread_record_request_observation(self, request_id: str, **fields: Any) -> None:
        assert self._threads is not None
        self._threads.record_request_observation(request_id, **fields)

    def thread_request_observation(self, request_id: str) -> dict[str, Any] | None:
        assert self._threads is not None
        return self._threads.request_observation(request_id)

    def thread_list_request_observations(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._threads is not None
        return self._threads.list_request_observations(**kwargs)

    def thread_consistency_request_overview(self, **kwargs: Any) -> dict[str, Any]:
        assert self._threads is not None
        return self._threads.consistency_request_overview(**kwargs)

    def thread_consistency_detail(self, request_id: str) -> dict[str, Any]:
        assert self._threads is not None
        return self._threads.consistency_detail(request_id)

    def thread_record_consistency_result(self, request_id: str,
                                         observations: list[dict[str, Any]], **kwargs: Any) -> int:
        assert self._threads is not None
        return self._threads.record_consistency_result(request_id, observations, **kwargs)

    def thread_record_consistency_observations(self, request_id: str,
                                               observations: list[dict[str, Any]], **kwargs: Any) -> int:
        assert self._threads is not None
        return self._threads.record_thread_consistency_observations(request_id, observations, **kwargs)

    def thread_list_consistency_observations(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert self._threads is not None
        return self._threads.list_thread_consistency_observations(**kwargs)

    def thread_record_consistency_feedback(self, request_id: str, label: str, **kwargs: Any) -> int:
        assert self._threads is not None
        return self._threads.record_consistency_feedback(request_id, label, **kwargs)

    def thread_list_consistency_feedback(self, request_id: str = "", **kwargs: Any) -> list[dict[str, Any]]:
        assert self._threads is not None
        return self._threads.list_consistency_feedback(request_id, **kwargs)

    def thread_eval_cases(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_thread_eval_cases(**kwargs)

    def thread_record_manual_feedback(self, **kwargs) -> int:
        assert self._threads is not None
        return self._threads.record_manual_feedback(**kwargs)

    def thread_list_manual_feedback(self, **kwargs) -> list:
        assert self._threads is not None
        return self._threads.list_manual_feedback(**kwargs)

    def memo_names(self) -> set[str]:
        assert self._eps is not None
        return self._eps.memo_names()

    def get_episode(self, memo_name: str) -> dict[str, Any] | None:
        assert self._eps is not None
        return self._eps.get_episode(memo_name)

    def get_episode_by_id(self, episode_id: str) -> dict[str, Any] | None:
        assert self._eps is not None
        return self._eps.get_episode_by_id(episode_id)

    def list_episodes(self, *, query: str = "", quality: str = "",
                      limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.list_episodes(query=query, quality=quality, limit=limit, offset=offset)

    def episode_detail(self, memo_name: str) -> dict[str, Any] | None:
        assert self._eps is not None
        return self._eps.episode_detail(memo_name)

    def episodes_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.episodes_for_batch(batch_id)

    def fallback_repair_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.fallback_repair_candidates(limit)

    def episode_snapshot(self, memo_name: str) -> dict[str, Any] | None:
        assert self._eps is not None
        return self._eps.episode_snapshot(memo_name)

    def restore_episode_snapshot(self, snapshot: dict[str, Any]) -> bool:
        assert self._eps is not None
        return self._eps.restore_episode_snapshot(snapshot)

    def evidence_for_memo(self, memo_name: str, query: str = "", limit: int = 3) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.evidence_for_memo(memo_name, query, limit)

    def record_diary_rollback(self, *, old_memo_name: str, episode_id: str,
                              new_memo_names: list[str], note: str = "") -> int:
        assert self._eps is not None
        return self._eps.record_rollback(old_memo_name=old_memo_name, episode_id=episode_id,
                                        new_memo_names=new_memo_names, note=note)

    def record_content_rollback(
        self,
        *,
        memo_name: str,
        episode_id: str,
        old_content: str,
        new_content: str,
        old_episode: dict[str, Any] | None = None,
        note: str = "",
    ) -> int:
        assert self._eps is not None
        return self._eps.record_content_rollback(
            memo_name=memo_name,
            episode_id=episode_id,
            old_content=old_content,
            new_content=new_content,
            old_episode=old_episode,
            note=note,
        )

    def rollback_by_id(self, rollback_id: int) -> dict[str, Any] | None:
        assert self._eps is not None
        return self._eps.rollback_by_id(rollback_id)

    def record_diary_rollback_atomic(self, *, old_memo_name: str, episode_id: str,
                                     new_memo_names: list[str], note: str = "") -> int:
        """Insert rollback mapping inside PreviewStore's active SAVEPOINT.

        No commit occurs here; PreviewStore releases/rolls back the per-item
        savepoint and commits the whole preview status update.
        """
        assert self._eps is not None
        return self._eps.record_rollback(
            old_memo_name=old_memo_name, episode_id=episode_id,
            new_memo_names=new_memo_names, note=note,
            commit=False, return_last_id=True,
        )

    def rollback_rows_for_memo(self, memo_name: str) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.rollback_rows_for_memo(memo_name)

    def rollback_history(self, limit: int = 30) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.rollback_history(limit)

    def mark_rollback_reverted(self, rollback_id: int) -> None:
        assert self._eps is not None
        self._eps.mark_rollback_reverted(rollback_id)

    # ---- persistent rewrite-preview forwards (8.2) ------------------

    def save_diary_preview(self, preview: dict[str, Any]) -> dict[str, Any]:
        assert self._previews is not None
        return self._previews.create(preview)

    @staticmethod
    def _flatten_diary_preview(preview: dict[str, Any] | None) -> dict[str, Any] | None:
        if not preview:
            return preview
        data = dict(preview)
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        # Compatibility surface: operational fields remain top-level while the
        # persistent payload is still available under `payload`.
        for key, value in payload.items():
            data.setdefault(key, value)
        data["new_diaries"] = list(payload.get("new_diaries") or data.get("new_diaries") or [])
        data["new_diaries_count"] = len(data["new_diaries"])
        return data

    def get_diary_preview(self, preview_id: str) -> dict[str, Any] | None:
        assert self._previews is not None
        return self._flatten_diary_preview(self._previews.get(preview_id))

    def list_diary_previews(self, limit: int = 30) -> list[dict[str, Any]]:
        assert self._previews is not None
        return [self._flatten_diary_preview(item) for item in self._previews.list(limit)]

    def update_diary_preview_status(self, preview_id: str, status: str,
                                    confirmed: bool = False) -> bool:
        assert self._previews is not None
        return self._previews.update_status(preview_id, status, confirmed)

    def record_inplace_preview_success(
        self, preview_id: str, memo_name: str, rollback_id: int,
    ) -> bool:
        assert self._previews is not None
        return self._previews.record_inplace_success(
            preview_id, memo_name, rollback_id,
        )

    def discard_diary_preview(self, preview_id: str) -> bool:
        assert self._previews is not None
        return self._previews.discard(preview_id)

    def confirm_diary_preview(self, preview_id: str, apply_item: Any,
                              record_rollback: Any,
                              compensate_item: Any | None = None) -> dict[str, Any]:
        assert self._previews is not None
        return self._previews.confirm(
            preview_id, apply_item, record_rollback, compensate_item=compensate_item,
        )

    def diary_preview_rollback_targets(self, preview_id: str) -> list[dict[str, Any]]:
        assert self._previews is not None
        return self._previews.rollback_targets(preview_id)

    def discard_old_previews(self, keep: int = 20) -> int:
        assert self._previews is not None
        self._previews.keep = max(1, int(keep or 20))
        return self._previews.prune()

    def upsert_eval_case(self, query: str, expected_memos: list[str], *,
                         case_id: str = "", note: str = "", source: str = "manual",
                         enabled: bool = True) -> dict[str, Any]:
        assert self._eps is not None
        return self._eps.upsert_eval_case(query, expected_memos, case_id=case_id,
                                          note=note, source=source, enabled=enabled)

    def list_eval_cases(self, enabled_only: bool = False, limit: int = 300) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.list_eval_cases(enabled_only=enabled_only, limit=limit)

    def delete_eval_case(self, case_id: str) -> bool:
        assert self._eps is not None
        return self._eps.delete_eval_case(case_id)

    def record_eval_run(self, mode: str, metrics: dict[str, Any], details: list[dict[str, Any]]) -> int:
        assert self._eps is not None
        return self._eps.record_eval_run(mode, metrics, details)

    def record_recall_observation(self, data: dict[str, Any]) -> dict[str, Any]:
        assert self._eps is not None
        return self._eps.record_recall_observation(data)

    def list_recall_observations(self, limit: int = 100,
                                 safety_only: bool = False) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.list_recall_observations(limit=limit, safety_only=safety_only)

    def get_recall_observation(self, request_id: str) -> dict[str, Any] | None:
        assert self._eps is not None
        return self._eps.get_recall_observation(request_id)

    def recall_observation_summary(self) -> dict[str, Any]:
        assert self._eps is not None
        return self._eps.recall_observation_summary()

    def set_recall_observation_feedback(self, request_id: str, feedback: str) -> bool:
        assert self._eps is not None
        return self._eps.set_recall_observation_feedback(request_id, feedback)

    def search_source_turns(self, query_vec: list[float], query_text: str,
                            limit: int = 24) -> list[dict[str, Any]]:
        assert self._src is not None
        return self._src.search_strict_source(query_vec, query_text, limit)

    def search_cards(self, query_vec: list[float], query_text: str,
                     limit: int = 18) -> list[dict[str, Any]]:
        assert self._eps is not None
        return self._eps.search_cards(query_vec, query_text, limit)

    # ---- 4.6.0 new generation forwards ------------------------------

    def switch_generation(self, dim: int | None = None,
                          model_id: str | None = None) -> dict[str, Any]:
        assert self._gen is not None
        dim = int(dim or self.emb_dim or 0)
        model_id = str(model_id or self.emb_model_id or "unknown")
        return self._gen.switch_after_validation(dim, model_id,
            take_snapshot=lambda: self.snapshot_db("pre_switch"))

    def rollback_generation(self) -> dict[str, Any]:
        assert self._gen is not None
        return self._gen.rollback()

    def stats(self) -> dict[str, Any]:
        assert self._src is not None and self._eps is not None and self._gen is not None
        src = self._src.counts()
        eps = self._eps.counts()
        gen_status = self._gen.snapshot_status()
        conn = self._conn or self._connect()
        state_count = int(conn.execute("SELECT COUNT(*) AS n FROM semantic_states").fetchone()["n"])
        state_versions = int(conn.execute("SELECT COUNT(*) AS n FROM semantic_state_versions").fetchone()["n"])
        pending_state = int(conn.execute(
            "SELECT COUNT(*) AS n FROM semantic_state_queue WHERE status IN ('pending','failed')"
        ).fetchone()["n"])
        db_uuid_row = conn.execute("SELECT value FROM meta WHERE key='database_uuid'").fetchone()
        db_uuid = str(db_uuid_row["value"]) if db_uuid_row else ""
        access = self._access.overview() if self._access is not None else {}
        return {
            "episodes": eps.get("episodes", 0),
            "source_grounded": eps.get("source_grounded", 0),
            "mixed_user_edited": eps.get("mixed_user_edited", 0),
            "diary_derived": eps.get("diary_derived", 0),
            "evidence": eps.get("evidence", 0),
            "source_batches": src.get("batches", 0),
            "source_turns": src.get("turns", 0),
            "source_turn_vectors": src.get("turn_vectors", 0),
            "source_turn_chunks": src.get("chunks", 0),
            "source_chunk_vectors": src.get("chunk_vectors", 0),
            "source_turn_terms": src.get("turn_terms", 0),
            "exact_turn_links": eps.get("exact_turn_links", 0),
            "batch_linked_episodes": eps.get("batch_linked_episodes", 0),
            "recoverable_episodes": eps.get("recoverable_episodes", 0),
            "missing_embeddings": eps.get("missing_embeddings", 0),
            "rollbacks": eps.get("rollbacks", 0),
            "semantic_states": state_count,
            "semantic_state_versions": state_versions,
            "pending_state_updates": pending_state,
            "recall_eval_cases": eps.get("recall_eval_cases", 0),
            "recall_observations": eps.get("recall_observations", 0),
            "vector_backend": "sqlite-vec" if self._vec_ok else "python-cosine",
            "db_path": self.db_path,
            "database_uuid": db_uuid,
            "active_generation": gen_status.get("active_generation", ""),
            "pending_generation": gen_status.get("pending_generation", ""),
            "prev_generation": gen_status.get("prev_generation", ""),
            "migration_status": gen_status.get("migration_status", ""),
            "source_vector_ready": bool(src.get("turn_vectors") or src.get("chunk_vectors")),
            "source_lexical_ready": bool(src.get("turn_terms", 0) > 0),
            "snapshots": int(conn.execute(
                "SELECT COUNT(*) AS n FROM db_snapshots").fetchone()["n"]),
            "canonical_db_path": conn.execute(
                "SELECT value FROM meta WHERE key='canonical_db_path'").fetchone()["value"]
            if conn.execute("SELECT value FROM meta WHERE key='canonical_db_path'").fetchone() else "",
            "path_conflict": conn.execute(
                "SELECT value FROM meta WHERE key='path_conflict'").fetchone()["value"]
            if conn.execute("SELECT value FROM meta WHERE key='path_conflict'").fetchone() else "",
            "memory_access_total": int(access.get("total") or 0),
            "memory_access_states": access.get("states") or {},
            "memory_interference_edges": int(access.get("edges") or 0),
            "memory_interference_groups": int(access.get("groups") or 0),
            "memory_access_shadow_requests": int(access.get("shadow_requests") or 0),
        }

    # ---- 8.3.A/B/E guard forwards -----------------------------------

    @property
    def guard(self) -> ArchiveGuard:
        assert self._guard is not None, "store not initialized"
        return self._guard

    def identity(self) -> dict[str, Any]:
        assert self._guard is not None
        return self._guard.identity_snapshot()

    def snapshot_db(self, kind: str = "manual") -> dict[str, Any]:
        assert self._guard is not None
        return self._guard.take_snapshot(self._connect(), kind)

    def list_snapshots(self, limit: int = 30) -> list[dict[str, Any]]:
        assert self._guard is not None
        return self._guard.list_snapshots(limit)

    def restore_snapshot(self, file_name: str) -> dict[str, Any]:
        assert self._guard is not None
        guard = self._guard
        def _close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        def _reopen() -> None:
            self._conn = None
            self._gen = None
            self._src = None
            self._eps = None
            self._guard = None
            self._previews = None
            self._access = None
            self._connect()
        return guard.restore_snapshot(file_name, live_store_close=_close, live_store_reopen=_reopen)

    def startup_self_check(self) -> dict[str, Any]:
        assert self._guard is not None
        return self._guard.startup_self_check()

    def startup_diagnosis(self) -> dict[str, Any]:
        assert self._guard is not None
        return self._guard.startup_diagnosis()

    def record_full_counts(self) -> dict[str, Any]:
        assert self._guard is not None
        return self._guard.record_full_counts()

    def traceability(self) -> dict[str, Any]:
        assert self._src is not None and self._eps is not None
        return compute_traceability(self._src, self._eps, self.stats())

    def close(self) -> None:
        # Drain/stop the consistency worker before SQLite becomes unavailable.
        with self._lock:
            if getattr(self, "_closing", False):
                return
            self._closing = True
        service = self.consistency_service
        self.consistency_service = None
        if service is not None:
            try:
                if not service.close():
                    logger.warning("[memos-memory][consistency] drain incomplete; SQLite close deferred")
                    def finish_close():
                        service._worker.join()
                        self._close_storage()
                    threading.Thread(target=finish_close, name="memos-consistency-close", daemon=True).start()
                    return
            except Exception:
                logger.exception("[memos-memory][consistency] close failed; SQLite retained")
                return
        self._close_storage()

    def _close_storage(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._gen = None
            self._src = None
            self._eps = None
            self._guard = None
            self._previews = None
            self._access = None

    def batch_status(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            """SELECT batch_id,session_id,source_kind,message_count,first_event_ts,last_event_ts,
                      status,attempts,last_error,next_retry_ts,created_ts,updated_ts
               FROM source_batches ORDER BY updated_ts DESC LIMIT ?""",
            (max(1, min(500, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

    def batch_info(self, batch_id: str) -> dict[str, Any] | None:
        assert self._src is not None
        return self._src.batch_info(batch_id)

    @staticmethod
    def _render_semantic_state(state: dict[str, Any]) -> str:
        sections = (
            ("关系位置", "relationship_position"),
            ("承诺与边界", "commitments_boundaries"),
            ("行为倾向", "behavior_tendencies"),
            ("近期情绪基调", "emotional_baseline"),
            ("仍未解决", "open_loops"),
        )
        return "\n".join(
            f"[{label}]\n{str(state.get(key) or '').strip()}"
            for label, key in sections
            if str(state.get(key) or "").strip()
        )

    def get_semantic_state(self, scope_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM semantic_states WHERE scope_id=?", (str(scope_id),),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["source_episode_ids"] = _json_list(data.pop("source_episode_ids_json", "[]"))
        return data

    def clear_semantic_state(self, scope_id: str) -> bool:
        """Clear only the current materialized state; version history remains available for audit."""
        with self._lock:
            conn = self._connect()
            cur = conn.execute("DELETE FROM semantic_states WHERE scope_id=?", (str(scope_id),))
            conn.commit()
            return bool(cur.rowcount)

    def upsert_semantic_state(
        self,
        scope_id: str,
        state: dict[str, Any],
        *,
        reason: str,
        source_batch_id: str = "",
        source_episode_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        scope_id = str(scope_id or "").strip()
        if not scope_id:
            raise ValueError("scope_id is required")
        clean = {
            key: "\n".join(line.rstrip() for line in str(state.get(key) or "").strip().splitlines()).strip()
            for key in (
                "relationship_position", "commitments_boundaries", "behavior_tendencies",
                "emotional_baseline", "open_loops",
            )
        }
        rendered = self._render_semantic_state(clean)
        if not rendered:
            raise ValueError("semantic state is empty")
        episode_ids = [str(value) for value in (source_episode_ids or []) if str(value).strip()]
        now = time.time()
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT version FROM semantic_states WHERE scope_id=?", (scope_id,)).fetchone()
            version = int(row["version"] or 0) + 1 if row else 1
            snapshot = {
                "scope_id": scope_id,
                "version": version,
                **clean,
                "rendered_text": rendered,
                "source_batch_id": source_batch_id,
                "source_episode_ids": episode_ids,
                "updated_ts": now,
            }
            conn.execute(
                """INSERT INTO semantic_states
                   (scope_id,version,relationship_position,commitments_boundaries,behavior_tendencies,
                    emotional_baseline,open_loops,rendered_text,source_batch_id,source_episode_ids_json,updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope_id) DO UPDATE SET
                    version=excluded.version,relationship_position=excluded.relationship_position,
                    commitments_boundaries=excluded.commitments_boundaries,
                    behavior_tendencies=excluded.behavior_tendencies,
                    emotional_baseline=excluded.emotional_baseline,open_loops=excluded.open_loops,
                    rendered_text=excluded.rendered_text,source_batch_id=excluded.source_batch_id,
                    source_episode_ids_json=excluded.source_episode_ids_json,updated_ts=excluded.updated_ts""",
                (
                    scope_id, version, clean["relationship_position"], clean["commitments_boundaries"],
                    clean["behavior_tendencies"], clean["emotional_baseline"], clean["open_loops"],
                    rendered, source_batch_id or None, json.dumps(episode_ids, ensure_ascii=False), now,
                ),
            )
            conn.execute(
                """INSERT OR REPLACE INTO semantic_state_versions
                   (scope_id,version,snapshot_json,reason,source_batch_id,created_ts) VALUES (?,?,?,?,?,?)""",
                (scope_id, version, json.dumps(snapshot, ensure_ascii=False), str(reason or ""), source_batch_id or None, now),
            )
            conn.commit()
        return snapshot

    def semantic_state_history(self, scope_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connect().execute(
                """SELECT version,snapshot_json,reason,source_batch_id,created_ts
                   FROM semantic_state_versions WHERE scope_id=? ORDER BY version DESC LIMIT ?""",
                (str(scope_id), max(1, min(500, int(limit)))),
            ).fetchall()
        out = []
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"] or "{}")
            except json.JSONDecodeError:
                snapshot = {}
            out.append({
                "version": int(row["version"] or 0), "state": snapshot,
                "reason": row["reason"] or "", "source_batch_id": row["source_batch_id"] or "",
                "created_ts": float(row["created_ts"] or 0),
            })
        return out

    def enqueue_state_update(self, batch_id: str, scope_id: str, episode_ids: list[str]) -> None:
        if not batch_id:
            return
        now = time.time()
        with self._lock:
            conn = self._connect()
            conn.execute(
                """INSERT INTO semantic_state_queue
                   (batch_id,scope_id,episode_ids_json,status,attempts,last_error,created_ts,updated_ts)
                   VALUES (?,?,?,'pending',0,'',?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET scope_id=excluded.scope_id,
                    episode_ids_json=excluded.episode_ids_json,status='pending',last_error='',updated_ts=excluded.updated_ts
                    WHERE semantic_state_queue.status IN ('pending','failed')""",
                (batch_id, str(scope_id), json.dumps(episode_ids, ensure_ascii=False), now, now),
            )
            conn.commit()

    def pending_state_updates(
        self,
        limit: int = 10,
        scope_id: str = "",
    ) -> list[dict[str, Any]]:
        scope = str(scope_id or "").strip()
        where = "status IN ('pending','failed')"
        params: list[Any] = []
        if scope:
            where += " AND scope_id=?"
            params.append(scope)
        params.append(max(1, min(100, int(limit))))
        with self._lock:
            rows = self._connect().execute(
                f"""SELECT batch_id,scope_id,episode_ids_json,status,attempts,last_error,created_ts,updated_ts
                    FROM semantic_state_queue WHERE {where}
                    ORDER BY created_ts LIMIT ?""",
                tuple(params),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["episode_ids"] = _json_list(item.pop("episode_ids_json", "[]"))
            out.append(item)
        return out

    def pending_state_update_count(self, scope_id: str = "") -> int:
        scope = str(scope_id or "").strip()
        where = "status IN ('pending','failed')"
        params: tuple[Any, ...] = ()
        if scope:
            where += " AND scope_id=?"
            params = (scope,)
        with self._lock:
            row = self._connect().execute(
                f"SELECT COUNT(*) AS n FROM semantic_state_queue WHERE {where}",
                params,
            ).fetchone()
        return int(row["n"] or 0) if row else 0

    def semantic_state_queue_summary(self, scope_id: str = "") -> dict[str, Any]:
        scope = str(scope_id or "").strip()
        where = "1=1"
        params: list[Any] = []
        if scope:
            where += " AND scope_id=?"
            params.append(scope)
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                f"""SELECT status,COUNT(*) AS n FROM semantic_state_queue
                    WHERE {where} GROUP BY status""",
                tuple(params),
            ).fetchall()
            reasons = conn.execute(
                f"""SELECT last_error,COUNT(*) AS n FROM semantic_state_queue
                    WHERE {where} AND status='superseded' AND last_error<>''
                    GROUP BY last_error ORDER BY n DESC LIMIT 12""",
                tuple(params),
            ).fetchall()
        counts = {str(row["status"] or "unknown"): int(row["n"] or 0) for row in rows}
        return {
            "counts": counts,
            "suppressed_reasons": {
                str(row["last_error"] or "unknown"): int(row["n"] or 0)
                for row in reasons
            },
            "pending": counts.get("pending", 0) + counts.get("failed", 0),
            "total": sum(counts.values()),
        }

    def mark_state_update(self, batch_id: str, status: str, error: str = "") -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """UPDATE semantic_state_queue SET status=?,attempts=attempts+1,last_error=?,updated_ts=?
                   WHERE batch_id=?""",
                (str(status), str(error or "")[:1000], time.time(), str(batch_id)),
            )
            conn.commit()

    def mark_state_updates(self, batch_ids: list[str], status: str, error: str = "") -> int:
        values = list(dict.fromkeys(str(value) for value in batch_ids if str(value)))
        if not values:
            return 0
        placeholders = ",".join("?" for _ in values)
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                f"""UPDATE semantic_state_queue
                    SET status=?,attempts=attempts+1,last_error=?,updated_ts=?
                    WHERE batch_id IN ({placeholders})""",
                (str(status), str(error or "")[:1000], time.time(), *values),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def supersede_pending_state_updates(
        self,
        reason: str = "full_rebuild",
        scope_id: str = "",
    ) -> int:
        scope = str(scope_id or "").strip()
        where = "status IN ('pending','failed')"
        params: list[Any] = [str(reason or "full_rebuild")[:1000], time.time()]
        if scope:
            where += " AND scope_id=?"
            params.append(scope)
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                f"""UPDATE semantic_state_queue SET status='superseded',last_error=?,updated_ts=?
                    WHERE {where}""",
                tuple(params),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def episodes_for_source_turn(self, batch_id: str, turn_index: int) -> list[dict[str, Any]]:
        """Map one archived source turn back to exact or batch-level episodes."""
        if not batch_id:
            return []
        conn = self._connect()
        exact_rows = conn.execute(
            """SELECT e.episode_id,e.memo_name,e.occurred_at,e.event_ts,e.time_basis,e.memory_type,
                      e.importance,e.scene_anchor,e.retrieval_key,e.state_change,e.long_effect,
                      e.trigger_hint,e.entities_json,e.unresolved_json,e.evidence_quality,e.card_text
               FROM episode_turn_links l JOIN episodes e ON e.episode_id=l.episode_id
               WHERE e.active=1 AND l.batch_id=? AND l.turn_index=?
               GROUP BY e.episode_id ORDER BY e.id""",
            (str(batch_id), int(turn_index)),
        ).fetchall()
        rows = exact_rows or conn.execute(
            """SELECT episode_id,memo_name,occurred_at,event_ts,time_basis,memory_type,
                      importance,scene_anchor,retrieval_key,state_change,long_effect,
                      trigger_hint,entities_json,unresolved_json,evidence_quality,card_text
               FROM episodes WHERE active=1 AND source_batch_id=? ORDER BY id""",
            (str(batch_id),),
        ).fetchall()
        source_row = conn.execute(
            "SELECT content FROM source_turns WHERE batch_id=? AND turn_index=?",
            (str(batch_id), int(turn_index)),
        ).fetchone()
        source_terms = _terms(source_row["content"] if source_row else "")
        out = []
        for row in rows:
            item = dict(row)
            item["entities"] = _json_list(item.pop("entities_json", "[]"))
            item["unresolved"] = _json_list(item.pop("unresolved_json", "[]"))
            item["exact_turn_link"] = bool(exact_rows)
            card_terms = _terms(item.get("card_text") or "")
            item["batch_link_score"] = (
                1.0 if exact_rows else len(source_terms & card_terms) / max(1, len(source_terms))
            )
            out.append(item)
        if exact_rows or len(out) <= 1:
            return out
        out.sort(
            key=lambda item: (
                float(item.get("batch_link_score") or 0.0), int(item.get("importance") or 0),
            ),
            reverse=True,
        )
        best = float(out[0].get("batch_link_score") or 0.0)
        return [
            item for item in out[:2]
            if float(item.get("batch_link_score") or 0.0) >= max(0.0, best - 0.12)
        ]
