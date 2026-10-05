"""Durable, local-only recovery records for failed plugin LLM requests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any


TASK_OPTIONS = (
    ("memory_generation", "原文取证与日记"),
    ("semantic_state", "滚动状态"),
    ("profile", "画像"),
    ("creative", "梦境与主动内容"),
    ("xinchao_live", "心潮即时评估"),
    ("xinchao_post", "心潮聊天后评估"),
    ("xinchao_other", "心潮其他任务"),
    ("time_insight", "时间洞察"),
    ("query_plan", "检索意图规划"),
)
TASK_FAMILIES = frozenset(key for key, _ in TASK_OPTIONS)


class LLMCompensationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS failed_llm_requests (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, label TEXT NOT NULL,
                source_batch_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, primary_error TEXT NOT NULL,
                fallback_error TEXT NOT NULL, payload BLOB NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, result_text TEXT NOT NULL DEFAULT '',
                progress_stage TEXT NOT NULL DEFAULT '',
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                progress_percent REAL NOT NULL DEFAULT 0,
                progress_message TEXT NOT NULL DEFAULT '',
                started_ts REAL NOT NULL DEFAULT 0,
                finished_ts REAL NOT NULL DEFAULT 0,
                checkpoint_payload BLOB,
                created_ts REAL NOT NULL, updated_ts REAL NOT NULL
            )""")
            columns = {
                str(row[1]) for row in db.execute(
                    "PRAGMA table_info(failed_llm_requests)"
                ).fetchall()
            }
            migrations = {
                "progress_stage": "TEXT NOT NULL DEFAULT ''",
                "progress_current": "INTEGER NOT NULL DEFAULT 0",
                "progress_total": "INTEGER NOT NULL DEFAULT 0",
                "progress_percent": "REAL NOT NULL DEFAULT 0",
                "progress_message": "TEXT NOT NULL DEFAULT ''",
                "started_ts": "REAL NOT NULL DEFAULT 0",
                "finished_ts": "REAL NOT NULL DEFAULT 0",
                "checkpoint_payload": "BLOB",
            }
            for column, ddl in migrations.items():
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE failed_llm_requests ADD COLUMN {column} {ddl}"
                    )
            db.execute("CREATE INDEX IF NOT EXISTS failed_llm_status ON failed_llm_requests(status, created_ts)")
            # A coroutine cannot survive an Astr/plugin process reload. Preserve
            # the request and expose the interruption instead of leaving a
            # permanently running row in the WebUI.
            now = time.time()
            db.execute("""UPDATE failed_llm_requests
                SET status='failed', progress_stage='interrupted',
                    progress_message='插件重载中断了后台补偿，可重新提交',
                    finished_ts=?, updated_ts=? WHERE status='running'""",
                (now, now))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def enqueue(self, *, task: str, label: str, source_batch_id: str,
                primary_error: str, fallback_error: str, request: dict[str, Any]) -> str:
        raw = json.dumps(request, ensure_ascii=False, default=str).encode("utf-8")
        record_id = "llm_" + hashlib.sha256(
            (task + "\0" + label + "\0" + source_batch_id).encode("utf-8") + raw
        ).hexdigest()[:24]
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO failed_llm_requests
                (id,task,label,source_batch_id,status,primary_error,fallback_error,payload,created_ts,updated_ts)
                VALUES (?,?,?,?,'pending',?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status='pending',
                    primary_error=excluded.primary_error,
                    fallback_error=excluded.fallback_error,
                    progress_stage='',progress_current=0,progress_total=0,
                    progress_percent=0,progress_message='',finished_ts=0,
                    updated_ts=excluded.updated_ts""",
                (record_id, task, label, source_batch_id, primary_error[:500],
                 fallback_error[:500], sqlite3.Binary(zlib.compress(raw)), now, now))
        return record_id

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("""SELECT id,task,label,source_batch_id,status,
                primary_error,fallback_error,attempts,result_text,
                progress_stage,progress_current,progress_total,progress_percent,
                progress_message,started_ts,finished_ts,created_ts,updated_ts
                FROM failed_llm_requests ORDER BY created_ts DESC LIMIT ?""",
                (max(1, min(500, int(limit))),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            result_text = str(item.pop("result_text", "") or "")
            item["has_result"] = bool(result_text)
            item["result_preview"] = (
                result_text[:8000]
                if str(item.get("status") or "") == "model_recovered"
                else ""
            )
            out.append(item)
        return out

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM failed_llm_requests WHERE id=?", (record_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["request"] = json.loads(zlib.decompress(result.pop("payload")).decode("utf-8"))
        return result

    def update(self, record_id: str, status: str, *, result_text: str = "",
               error: str = "", increment_attempt: bool = True) -> None:
        if status not in {
            "pending", "running", "restored", "model_recovered", "failed", "dismissed",
        }:
            raise ValueError("invalid recovery status")
        now = time.time()
        finished_ts = now if status in {
            "restored", "model_recovered", "failed", "dismissed",
        } else 0.0
        with self._lock, self._connect() as db:
            db.execute("""UPDATE failed_llm_requests SET status=?,attempts=attempts+?,
                result_text=?,fallback_error=CASE WHEN ?='' THEN fallback_error ELSE ? END,
                finished_ts=CASE WHEN ?>0 THEN ? ELSE finished_ts END,
                updated_ts=? WHERE id=?""",
                (status, 1 if increment_attempt else 0, result_text[:100000],
                 error, error[:500], finished_ts, finished_ts, now, record_id))

    def start(self, record_id: str, *, message: str = "正在准备补偿") -> bool:
        """Atomically claim one request; rows from the same batch are exclusive."""
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT source_batch_id,status FROM failed_llm_requests WHERE id=?",
                (record_id,),
            ).fetchone()
            if row is None or str(row["status"]) in {"restored", "dismissed", "running"}:
                return False
            batch_id = str(row["source_batch_id"] or "")
            if batch_id:
                running = db.execute(
                    "SELECT 1 FROM failed_llm_requests WHERE source_batch_id=? "
                    "AND status='running' AND id!=? LIMIT 1",
                    (batch_id, record_id),
                ).fetchone()
                if running is not None:
                    return False
            cursor = db.execute("""UPDATE failed_llm_requests
                SET status='running',attempts=attempts+1,
                    progress_stage='queued',progress_current=0,progress_total=0,
                    progress_percent=0,progress_message=?,started_ts=?,finished_ts=0,
                    updated_ts=? WHERE id=? AND status IN ('pending','failed','model_recovered')""",
                (message[:500], now, now, record_id))
            return cursor.rowcount == 1

    def progress(
        self,
        record_id: str,
        *,
        stage: str,
        current: int = 0,
        total: int = 0,
        percent: float | None = None,
        message: str = "",
    ) -> None:
        total_value = max(0, int(total or 0))
        current_value = max(0, int(current or 0))
        if percent is None:
            percent_value = (
                100.0 * min(current_value, total_value) / total_value
                if total_value else 0.0
            )
        else:
            percent_value = max(0.0, min(100.0, float(percent)))
        with self._lock, self._connect() as db:
            db.execute("""UPDATE failed_llm_requests
                SET progress_stage=?,progress_current=?,progress_total=?,
                    progress_percent=?,progress_message=?,updated_ts=?
                WHERE id=? AND status='running'""",
                (str(stage)[:80], current_value, total_value, percent_value,
                 str(message)[:500], time.time(), record_id))

    def checkpoint(self, record_id: str, payload: dict[str, Any]) -> None:
        """Persist resumable intermediate work without marking it publishable."""
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE failed_llm_requests SET checkpoint_payload=?,updated_ts=? "
                "WHERE id=?",
                (sqlite3.Binary(zlib.compress(raw)), time.time(), record_id),
            )

    def load_checkpoint(self, record_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT checkpoint_payload FROM failed_llm_requests WHERE id=?",
                (record_id,),
            ).fetchone()
        if row is None or not row["checkpoint_payload"]:
            return {}
        try:
            value = json.loads(
                zlib.decompress(row["checkpoint_payload"]).decode("utf-8")
            )
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def clear_checkpoint(self, record_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE failed_llm_requests SET checkpoint_payload=NULL,updated_ts=? "
                "WHERE id=?",
                (time.time(), record_id),
            )

    def finish(
        self,
        record_id: str,
        status: str,
        *,
        result_text: str = "",
        error: str = "",
        message: str = "",
    ) -> None:
        if status not in {"restored", "model_recovered", "failed"}:
            raise ValueError("invalid terminal recovery status")
        now = time.time()
        percent = 100.0 if status in {"restored", "model_recovered"} else 0.0
        stage = "completed" if status in {"restored", "model_recovered"} else "failed"
        with self._lock, self._connect() as db:
            db.execute("""UPDATE failed_llm_requests SET status=?,result_text=?,
                fallback_error=CASE WHEN ?='' THEN fallback_error ELSE ? END,
                progress_stage=?,progress_percent=?,progress_message=?,
                finished_ts=?,updated_ts=? WHERE id=?""",
                (status, result_text[:100000], error, error[:500], stage, percent,
                 str(message or error)[:500], now, now, record_id))

    def resolve_batch(self, batch_id: str) -> None:
        if not batch_id:
            return
        with self._lock, self._connect() as db:
            now = time.time()
            db.execute("""UPDATE failed_llm_requests SET status='restored',
                progress_stage='completed',progress_percent=100,
                progress_message='同一原文批次已完成补偿',checkpoint_payload=NULL,
                finished_ts=?,updated_ts=?
                WHERE source_batch_id=? AND status IN ('pending','running','failed','model_recovered')""",
                (now, now, batch_id))
