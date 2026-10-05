"""Old Episode inventory, scope assignment, and resumable Shadow migration.

Scans existing episodes and classifies them by source traceability grade.
Writes results to thread_migration_state. Supports pause/resume and
idempotent re-runs. Never modifies any source data.
"""
from __future__ import annotations

import logging
import json
import time
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

_SCAN_BATCH_SIZE = 100


class ThreadMigration:
    """Inventory existing Episodes without modifying source memory rows."""

    def __init__(self, get_conn: Callable[[], Any], lock: threading.RLock,
                 thread_store: Any):
        self._get_conn = get_conn
        self._lock = lock
        self._ts = thread_store  # ThreadStore instance

    # ----------------------------------------------------------------------- #
    #  public interface                                                         #
    # ----------------------------------------------------------------------- #

    def scan_status(self) -> dict[str, Any]:
        conn = self._get_conn()
        grades = self._ts.source_grade_counts()
        checkpoint = self._ts._get(conn, "scan_checkpoint_episode_id", "")
        completed = self._ts._get(conn, "scan_completed", "0") == "1"
        started_ts = float(self._ts._get(conn, "scan_started_ts", "0") or 0)
        finished_ts = float(self._ts._get(conn, "scan_finished_ts", "0") or 0)
        return {
            "completed": completed,
            "grades": grades,
            "checkpoint_episode_id": checkpoint,
            "started_ts": started_ts,
            "finished_ts": finished_ts,
        }

    def run_scan(self, *, read_only: bool = True, scope_id: str = "",
                 enqueue_existing: bool = False, batch_size: int = _SCAN_BATCH_SIZE,
                 stop_event: threading.Event | None = None,
                 max_batches: int | None = None, restart: bool = False) -> dict[str, Any]:
        """Resume a durable keyset scan; bounded slices never claim completion.

        restart=True inventories from the beginning without clearing manual data.
        A completed scan is idempotent; enqueue handles later source revisions.
        """
        from .thread_candidates import ThreadCandidates
        signature = json.dumps([scope_id, read_only, enqueue_existing])
        key = "scan_state:" + signature
        with self._lock:
            conn = self._get_conn()
            try:
                state = json.loads(self._ts._get(conn, key, "{}"))
                if not isinstance(state, dict):
                    state = {}
            except (TypeError, ValueError):
                state = {}
            if restart:
                state = {}
        if state.get('completed'):
            return {**state, "processed_this_call": 0}
        last_id = str(state.get('checkpoint_episode_id') or '')
        scanned = int(state.get('scanned') or 0)
        grades = state.get('grades') or dict.fromkeys(('A', 'B', 'C', 'D'), 0)
        processed = batches = 0
        completed = False
        candidates = ThreadCandidates(self._get_conn)
        while max_batches is None or batches < max(1, int(max_batches)):
            if (stop_event and stop_event.is_set()) or self._ts.is_paused():
                break
            with self._lock:
                conn = self._get_conn()
                rows = conn.execute("SELECT episode_id,source_batch_id,evidence_quality FROM episodes WHERE active=1 AND episode_id>? ORDER BY episode_id LIMIT ?",
                                    (last_id, max(1, min(500, int(batch_size))))).fetchall()
            if not rows:
                completed = True
                break
            for row in rows:
                eid = str(row['episode_id'])
                # Enqueue owns a short transaction and is safe to replay if interrupted.
                if enqueue_existing and scope_id:
                    self._ts.enqueue(scope_id, eid)
                with self._lock:
                    conn = self._get_conn()
                    try:
                        grade = self._classify_episode(conn, row)
                        if not read_only:
                            self._ts._set(conn, 'grade:' + eid, grade)
                        if scope_id and not enqueue_existing:
                            conn.execute("INSERT OR IGNORE INTO thread_episode_scopes VALUES(?,?,?,?,?,?)", (eid, scope_id, 'migration', .9, time.time(), time.time()))
                        candidates.refresh_episode(eid, commit=False)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                grades[grade] = int(grades.get(grade, 0)) + 1
                scanned += 1
                processed += 1
                last_id = eid
            batches += 1
            with self._lock:
                conn = self._get_conn()
                state = {"scanned": scanned, "grades": grades, "checkpoint_episode_id": last_id, "completed": False}
                self._ts._set(conn, key, json.dumps(state))
                self._ts._set(conn, 'scan_checkpoint_episode_id', last_id)
                self._ts._set(conn, 'scan_scanned_count', str(scanned))
                conn.commit()
        state = {"scanned": scanned, "grades": grades, "checkpoint_episode_id": last_id, "completed": completed}
        with self._lock:
            conn = self._get_conn()
            self._ts._set(conn, key, json.dumps(state))
            self._ts._set(conn, 'scan_completed', '1' if completed else '0')
            if completed:
                self._ts._set(conn, 'scan_finished_ts', str(time.time()))
            conn.commit()
        return {**state, "processed_this_call": processed}

    # ----------------------------------------------------------------------- #
    #  classification                                                           #
    # ----------------------------------------------------------------------- #

    @staticmethod
    def _classify_episode(conn: Any, row: Any) -> str:
        episode_id = str(row["episode_id"])
        source_batch_id = str(row["source_batch_id"] or "")
        evidence_quality = str(row["evidence_quality"] or "")

        # Grade A: has verified source turn links
        try:
            has_link = conn.execute(
                """SELECT 1 FROM episode_turn_links tl
                   JOIN source_turns st ON st.batch_id=tl.batch_id
                                       AND st.turn_index=tl.turn_index
                   WHERE tl.episode_id=? LIMIT 1""",
                (episode_id,),
            ).fetchone()
            if has_link:
                return "A"
        except Exception:
            pass

        # Grade B: has source batch but no verified turn links
        if source_batch_id:
            try:
                batch_exists = conn.execute(
                    "SELECT 1 FROM source_batches WHERE batch_id=? LIMIT 1",
                    (source_batch_id,),
                ).fetchone()
                if batch_exists:
                    return "B"
            except Exception:
                pass

        # Grade D: date unparseable or corrupted quality flag
        if evidence_quality not in ("diary_derived", "source_grounded",
                                    "mixed_user_edited", ""):
            return "D"

        # Grade C: diary-derived, no source
        return "C"
