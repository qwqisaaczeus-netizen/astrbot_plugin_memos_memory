"""Persistent long-diary rewrite preview workflow (4.6.0-test2 / 8.2).

Unlike test1's preview-wide commit, test2 confirms each generated diary under
its own SQLite SAVEPOINT. One failed memo write therefore cannot leave another
successful diary without its rollback mapping, and retry only touches failed
or missing items.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable

TERMINAL = {"confirmed", "discarded"}


class PreviewStore:
    def __init__(self, get_conn: Callable[[], sqlite3.Connection],
                 lock: threading.RLock, keep: int = 20):
        self._get_conn = get_conn
        self._lock = lock
        self.keep = max(1, int(keep or 20))

    def init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS diary_rewrite_previews (
                preview_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                old_memo_name TEXT DEFAULT '',
                old_card_text TEXT DEFAULT '',
                source_batch_id TEXT DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL,
                confirmed_ts REAL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS diary_preview_items (
                preview_id TEXT NOT NULL,
                item_index INTEGER NOT NULL,
                old_memo_name TEXT DEFAULT '',
                new_memo_name TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT DEFAULT '',
                rollback_id INTEGER DEFAULT 0,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL,
                PRIMARY KEY(preview_id,item_index),
                FOREIGN KEY(preview_id) REFERENCES diary_rewrite_previews(preview_id) ON DELETE CASCADE
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_preview_status ON diary_rewrite_previews(status,updated_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_preview_item_status ON diary_preview_items(preview_id,status,item_index)")

    @staticmethod
    def _decode(row: sqlite3.Row | None, items: list[sqlite3.Row] | None = None) -> dict[str, Any] | None:
        if not row:
            return None
        data = dict(row)
        try:
            data["payload"] = json.loads(data.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            data["payload"] = {}
        data["items"] = [dict(item) for item in (items or [])]
        data["new_diaries_count"] = len(data["payload"].get("new_diaries") or [])
        return data

    def create(self, preview: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        preview_id = str(preview.get("preview_id") or f"preview_{uuid.uuid4().hex[:24]}")
        payload = dict(preview.get("payload") or preview)
        # Preserve explicit outer identity fields, but don't recursively store payload.
        payload.pop("payload", None)
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR REPLACE INTO diary_rewrite_previews
                   (preview_id,episode_id,old_memo_name,old_card_text,source_batch_id,
                    payload_json,status,created_ts,updated_ts,confirmed_ts)
                   VALUES (?,?,?,?,?,?,?,COALESCE((SELECT created_ts FROM diary_rewrite_previews WHERE preview_id=?),?),?,NULL)""",
                (preview_id, str(preview.get("episode_id") or payload.get("episode_id") or ""),
                 str(preview.get("old_memo_name") or payload.get("old_memo_name") or ""),
                 str(preview.get("old_card_text") or payload.get("old_card_text") or ""),
                 str(preview.get("source_batch_id") or payload.get("source_batch_id") or ""),
                 json.dumps(payload, ensure_ascii=False), "pending", preview_id, now, now),
            )
            conn.commit()
        self.prune()
        return self.get(preview_id) or {"preview_id": preview_id}

    def get(self, preview_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM diary_rewrite_previews WHERE preview_id=?", (str(preview_id),)).fetchone()
        items = conn.execute(
            "SELECT * FROM diary_preview_items WHERE preview_id=? ORDER BY item_index", (str(preview_id),)
        ).fetchall() if row else []
        return self._decode(row, items)

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            "SELECT * FROM diary_rewrite_previews ORDER BY updated_ts DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        ).fetchall()
        return [self.get(str(row["preview_id"])) for row in rows if row]

    def update_status(self, preview_id: str, status: str, confirmed: bool = False) -> bool:
        with self._lock:
            conn = self._get_conn()
            now = time.time()
            cur = conn.execute(
                """UPDATE diary_rewrite_previews SET status=?,updated_ts=?,
                   confirmed_ts=CASE WHEN ? THEN ? ELSE confirmed_ts END WHERE preview_id=?""",
                (str(status), now, 1 if confirmed else 0, now, str(preview_id)),
            )
            conn.commit()
            return bool(cur.rowcount)

    def discard(self, preview_id: str) -> bool:
        preview = self.get(preview_id)
        if not preview or preview.get("status") == "confirmed":
            return False
        return self.update_status(preview_id, "discarded")

    def record_inplace_success(
        self, preview_id: str, memo_name: str, rollback_id: int,
    ) -> bool:
        """Mark a one-item in-place repair as confirmed and rollback-addressable."""
        with self._lock:
            conn = self._get_conn()
            preview = self.get(preview_id)
            if not preview or preview.get("status") == "discarded":
                return False
            now = time.time()
            old_name = str(preview.get("old_memo_name") or memo_name)
            conn.execute(
                """INSERT INTO diary_preview_items
                   (preview_id,item_index,old_memo_name,new_memo_name,status,error,
                    rollback_id,created_ts,updated_ts)
                   VALUES (?,0,?,?,?,'',?,?,?)
                   ON CONFLICT(preview_id,item_index) DO UPDATE SET
                    old_memo_name=excluded.old_memo_name,
                    new_memo_name=excluded.new_memo_name,status='succeeded',error='',
                    rollback_id=excluded.rollback_id,updated_ts=excluded.updated_ts""",
                (
                    str(preview_id), old_name, str(memo_name), "succeeded",
                    int(rollback_id), now, now,
                ),
            )
            conn.execute(
                """UPDATE diary_rewrite_previews
                   SET status='confirmed',updated_ts=?,confirmed_ts=?
                   WHERE preview_id=?""",
                (now, now, str(preview_id)),
            )
            conn.commit()
            return True

    def confirm(
        self,
        preview_id: str,
        apply_item: Callable[[dict[str, Any], int], str],
        record_rollback: Callable[[str, str, list[str], str], int],
        compensate_item: Callable[[str, int], None] | None = None,
    ) -> dict[str, Any]:
        """Confirm each generated diary under one SAVEPOINT.

        Callbacks run while the SAVEPOINT is active. Integrators should make
        their memo write callback compensatable/idempotent. Retrying skips rows
        already marked succeeded and retries only failed/missing rows.
        """
        with self._lock:
            conn = self._get_conn()
            preview = self.get(preview_id)
            if not preview:
                return {"ok": False, "reason": "missing", "preview_id": preview_id}
            if preview.get("status") == "discarded":
                return {"ok": False, "reason": "discarded", "preview_id": preview_id}
            payload = preview.get("payload") or {}
            diaries = payload.get("new_diaries") if isinstance(payload.get("new_diaries"), list) else []
            if not diaries:
                return {"ok": False, "reason": "empty", "preview_id": preview_id}
            previous = {int(row["item_index"]): row for row in preview.get("items") or []}
            succeeded = 0
            failed = 0
            skipped = 0
            results: list[dict[str, Any]] = []
            old_name = str(preview.get("old_memo_name") or payload.get("old_memo_name") or "")
            episode_id = str(preview.get("episode_id") or payload.get("episode_id") or "")
            for index, raw_item in enumerate(diaries):
                item = raw_item if isinstance(raw_item, dict) else {"content": str(raw_item)}
                old_row = previous.get(index)
                if old_row and old_row.get("status") == "succeeded":
                    succeeded += 1
                    skipped += 1
                    results.append({"index": index, "status": "succeeded",
                                    "new_memo_name": old_row.get("new_memo_name"), "skipped": True})
                    continue
                savepoint = f"preview_item_{index}"
                conn.execute(f"SAVEPOINT {savepoint}")
                new_name = ""
                try:
                    new_name = str(apply_item(item, index) or "").strip()
                    if not new_name:
                        raise ValueError("apply_item returned empty memo name")
                    rollback_id = int(record_rollback(
                        old_name, episode_id, [new_name], f"preview={preview_id};item={index}"
                    ) or 0)
                    now = time.time()
                    conn.execute(
                        """INSERT INTO diary_preview_items
                           (preview_id,item_index,old_memo_name,new_memo_name,status,error,
                            rollback_id,created_ts,updated_ts)
                           VALUES (?,?,?,?,?,'',?,?,?)
                           ON CONFLICT(preview_id,item_index) DO UPDATE SET
                            old_memo_name=excluded.old_memo_name,new_memo_name=excluded.new_memo_name,
                            status='succeeded',error='',rollback_id=excluded.rollback_id,
                            updated_ts=excluded.updated_ts""",
                        (preview_id, index, old_name, new_name, "succeeded", rollback_id, now, now),
                    )
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    succeeded += 1
                    results.append({"index": index, "status": "succeeded",
                                    "new_memo_name": new_name, "rollback_id": rollback_id})
                except Exception as exc:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    # SQLite work has been rolled back. If apply_item already
                    # created an external Memos row, compensate it immediately
                    # so no orphan diary survives without rollback metadata.
                    compensation_error = ""
                    orphan_name = ""
                    if new_name and compensate_item is not None:
                        try:
                            compensate_item(new_name, index)
                        except Exception as cleanup_exc:
                            compensation_error = f"; compensation_failed={cleanup_exc}"
                            orphan_name = new_name
                    elif new_name:
                        orphan_name = new_name
                    now = time.time()
                    error_text = (str(exc) + compensation_error)[:500]
                    conn.execute(
                        """INSERT INTO diary_preview_items
                           (preview_id,item_index,old_memo_name,new_memo_name,status,error,created_ts,updated_ts)
                           VALUES (?,?,?,?,'failed',?,?,?)
                           ON CONFLICT(preview_id,item_index) DO UPDATE SET
                            new_memo_name=excluded.new_memo_name,status='failed',
                            error=excluded.error,updated_ts=excluded.updated_ts""",
                        (preview_id, index, old_name, orphan_name, error_text, now, now),
                    )
                    failed += 1
                    results.append({"index": index, "status": "failed", "error": str(exc)[:500]})
            final_status = "confirmed" if succeeded == len(diaries) and failed == 0 else "partial"
            now = time.time()
            conn.execute(
                """UPDATE diary_rewrite_previews SET status=?,updated_ts=?,
                   confirmed_ts=CASE WHEN ?='confirmed' THEN ? ELSE confirmed_ts END
                   WHERE preview_id=?""",
                (final_status, now, final_status, now, preview_id),
            )
            conn.commit()
        return {"ok": failed == 0 and succeeded == len(diaries), "preview_id": preview_id,
                "status": final_status, "succeeded": succeeded, "failed": failed,
                "skipped": skipped, "results": results}

    def rollback_targets(self, preview_id: str) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            """SELECT item_index,new_memo_name,rollback_id FROM diary_preview_items
               WHERE preview_id=? AND status='succeeded' ORDER BY item_index""",
            (str(preview_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def prune(self) -> int:
        """Never prune pending/partial work; keep newest terminal rows only."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT preview_id FROM diary_rewrite_previews
                   WHERE status IN ('confirmed','discarded') ORDER BY updated_ts DESC"""
            ).fetchall()
            stale = rows[self.keep:]
            for row in stale:
                conn.execute("DELETE FROM diary_rewrite_previews WHERE preview_id=?", (row["preview_id"],))
            conn.commit()
            return len(stale)
