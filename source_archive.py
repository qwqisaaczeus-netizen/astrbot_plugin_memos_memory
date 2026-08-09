"""SourceArchive Repository (4.6.0-test2 / 3.1, 8.3.C, 8.3.D).

Owns the lossless turn tables only: source_batches / source_turns /
source_turn_chunks / source_turn_terms. No episodes, no rollbacks, no
semantic-state machinery. Vector tables are borrowed from VectorGeneration
so this class never mints `vec_*` table names itself.

All writes append/replace derived embeddings; the original text and the
chunk text are never overwritten by anything in this Repository.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .store_utils import cosine, deserialize_f32, extract_terms, json_list, serialize_f32
from .vector_generation import VectorGeneration

logger = logging.getLogger(__name__)


class SourceArchive:
    """Repository: complete raw conversation + per-chunk vectors."""

    SCHEMA_VERSION = 5

    def __init__(
        self,
        db_path: str,
        gen: VectorGeneration,
        get_conn: Callable[[], sqlite3.Connection],
        lock: threading.RLock,
    ):
        self.db_path = str(db_path)
        self._gen = gen
        self._get_conn = get_conn
        self._lock = lock

    # ---- schema --------------------------------------------------------

    def init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS source_batches (
                batch_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                first_event_ts REAL DEFAULT 0,
                last_event_ts REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'archived',
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS source_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                event_ts REAL DEFAULT 0,
                event_timezone TEXT DEFAULT '',
                content_hash TEXT NOT NULL,
                embedding BLOB,
                embedding_generation TEXT DEFAULT '',
                UNIQUE(batch_id, turn_index),
                FOREIGN KEY(batch_id) REFERENCES source_batches(batch_id) ON DELETE CASCADE
            )"""
        )
        turn_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(source_turns)").fetchall()}
        if "embedding_generation" not in turn_cols:
            conn.execute("ALTER TABLE source_turns ADD COLUMN embedding_generation TEXT DEFAULT ''")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS source_turn_terms (
                turn_id INTEGER NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY(turn_id, term),
                FOREIGN KEY(turn_id) REFERENCES source_turns(id) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS source_turn_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding BLOB,
                embedding_generation TEXT DEFAULT '',
                UNIQUE(turn_id, chunk_index),
                FOREIGN KEY(turn_id) REFERENCES source_turns(id) ON DELETE CASCADE
            )"""
        )
        # Rebuildable vectors are versioned independently from source/card rows.
        # This preserves active + pending + one rollback generation even when
        # sqlite-vec is unavailable and one row cannot hold two dimensions.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS embedding_versions (
                kind TEXT NOT NULL,
                row_id INTEGER NOT NULL,
                generation TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_ts REAL NOT NULL,
                PRIMARY KEY(kind,row_id,generation)
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_embedding_versions_gen ON embedding_versions(generation,kind,row_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_turn_batch ON source_turns(batch_id, turn_index)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source_turn_term ON source_turn_terms(term, turn_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_turn ON source_turn_chunks(turn_id, chunk_index)")

    # ---- archive -------------------------------------------------------

    def archive_batch(self, session_id: str, messages: list[dict[str, Any]], source_kind: str) -> str:
        normalized = []
        for index, message in enumerate(messages or []):
            raw_content = message.get("content")
            content = raw_content if isinstance(raw_content, str) else str(raw_content or "")
            # Lossless means no strip/normalization: preserve leading/trailing
            # whitespace, newlines and even an explicitly empty archived turn.
            try:
                event_ts = float(message.get("event_ts") or message.get("recorded_ts") or message.get("created_ts") or 0)
            except (TypeError, ValueError):
                event_ts = 0.0
            normalized.append({
                "turn_index": index,
                "role": str(message.get("role") or ""),
                "content": content,
                "event_ts": event_ts,
                "event_timezone": str(message.get("event_timezone") or message.get("timezone") or ""),
            })
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256((str(session_id) + "\n" + payload).encode("utf-8")).hexdigest()
        batch_id = "batch_" + digest[:24]
        now = time.time()
        event_times = [float(row["event_ts"]) for row in normalized if float(row["event_ts"]) > 0]
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR IGNORE INTO source_batches
                   (batch_id,session_id,source_kind,content_hash,message_count,
                    first_event_ts,last_event_ts,status,created_ts,updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id, str(session_id), str(source_kind or "auto"), digest, len(normalized),
                    min(event_times) if event_times else 0.0,
                    max(event_times) if event_times else 0.0,
                    "archived", now, now,
                ),
            )
            for row in normalized:
                content_hash = hashlib.sha256(row["content"].encode("utf-8")).hexdigest()
                conn.execute(
                    """INSERT OR IGNORE INTO source_turns
                       (batch_id,turn_index,role,content,event_ts,event_timezone,content_hash)
                       VALUES (?,?,?,?,?,?,?)""",
                    (batch_id, row["turn_index"], row["role"], row["content"],
                     row["event_ts"], row["event_timezone"], content_hash),
                )
                saved = conn.execute(
                    "SELECT id FROM source_turns WHERE batch_id=? AND turn_index=?",
                    (batch_id, row["turn_index"]),
                ).fetchone()
                if saved:
                    conn.executemany(
                        "INSERT OR IGNORE INTO source_turn_terms(turn_id,term) VALUES (?,?)",
                        [(int(saved["id"]), term) for term in sorted(extract_terms(row["content"]))[:120]],
                    )
            conn.commit()
        return batch_id

    def mark_batch(self, batch_id: str, status: str) -> None:
        if not batch_id:
            return
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE source_batches SET status=?,updated_ts=? WHERE batch_id=?",
                (str(status), time.time(), batch_id),
            )
            conn.commit()

    # ---- reads ---------------------------------------------------------

    def source_turns(self, batch_id: str) -> list[dict[str, Any]]:
        if not batch_id:
            return []
        rows = self._get_conn().execute(
            """SELECT turn_index,role,content,event_ts,event_timezone
               FROM source_turns WHERE batch_id=? ORDER BY turn_index""",
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def turn_chunks_for_turn(self, turn_id: int) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            "SELECT id,chunk_index,chunk_text,embedding_generation FROM source_turn_chunks "
            "WHERE turn_id=? ORDER BY chunk_index",
            (int(turn_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_turn_chunks(self, turn_id: int, chunks: list[str]) -> int:
        if not chunks:
            return 0
        added = 0
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN")
                for index, text in enumerate(chunks):
                    text = " ".join(str(text or "").split()).strip()
                    if not text:
                        continue
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO source_turn_chunks
                           (turn_id,chunk_index,chunk_text) VALUES (?,?,?)""",
                        (int(turn_id), int(index), text),
                    )
                    added += cur.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return added

    # ---- embedding rows ------------------------------------------------

    def turn_embedding_rows(self, *, batch_id: str = "", after_id: int = 0,
                            limit: int = 64, missing_only: bool = True,
                            target_generation: str = "") -> list[dict[str, Any]]:
        clauses = ["id>?"]
        params: list[Any] = [max(0, int(after_id))]
        if batch_id:
            clauses.append("batch_id=?")
            params.append(str(batch_id))
        if missing_only and target_generation:
            clauses.append("NOT EXISTS (SELECT 1 FROM embedding_versions ev WHERE ev.kind='source' AND ev.row_id=source_turns.id AND ev.generation=?)")
            params.append(str(target_generation))
        elif missing_only:
            clauses.append("embedding IS NULL")
        params.append(max(1, min(500, int(limit))))
        rows = self._get_conn().execute(
            f"""SELECT id,batch_id,turn_index,role,content,event_ts,event_timezone
                FROM source_turns WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def chunk_embedding_rows(self, *, after_id: int = 0,
                              limit: int = 64, missing_only: bool = True,
                              target_generation: str = "") -> list[dict[str, Any]]:
        clauses = ["id>?"]
        params: list[Any] = [max(0, int(after_id))]
        if missing_only and target_generation:
            clauses.append("NOT EXISTS (SELECT 1 FROM embedding_versions ev WHERE ev.kind='chunk' AND ev.row_id=source_turn_chunks.id AND ev.generation=?)")
            params.append(str(target_generation))
        elif missing_only:
            clauses.append("embedding IS NULL")
        params.append(max(1, min(500, int(limit))))
        rows = self._get_conn().execute(
            f"""SELECT id,turn_id,chunk_index,chunk_text
                FROM source_turn_chunks WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_turn_embeddings(self, rows: list[tuple[int, list[float]]], runtime_gen: str = "") -> int:
        """`runtime_gen` is the generation id the *current* embedding should be
        tagged with (the runtime model/dim of the active request). The caller
        obtains it from `gen.runtime_generation(model, dim)`; passing "" tags
        rows with whatever the active generation currently is. This keeps the
        Repository from having to know the model name."""
        if not rows:
            return 0
        with self._lock:
            conn = self._get_conn()
            gen = runtime_gen or self._gen.active_generation()
            target_active = self._gen.active_generation()
            target_table = self._gen.table_name_for("source", gen) if self._gen._vec_ok and gen else ""
            updated = 0
            try:
                conn.execute("BEGIN")
                for row_id, vector in rows:
                    blob = serialize_f32(vector)
                    exists = conn.execute("SELECT 1 FROM source_turns WHERE id=?", (int(row_id),)).fetchone()
                    if not exists:
                        continue
                    conn.execute(
                        """INSERT OR REPLACE INTO embedding_versions
                           (kind,row_id,generation,embedding,created_ts) VALUES ('source',?,?,?,?)""",
                        (int(row_id), gen, blob, time.time()),
                    )
                    if gen == target_active:
                        conn.execute(
                            "UPDATE source_turns SET embedding=?,embedding_generation=? WHERE id=?",
                            (blob, gen, int(row_id)),
                        )
                    updated += 1
                    content_row = conn.execute(
                        "SELECT content FROM source_turns WHERE id=?", (int(row_id),)
                    ).fetchone()
                    if content_row:
                        conn.executemany(
                            "INSERT OR IGNORE INTO source_turn_terms(turn_id,term) VALUES (?,?)",
                            [(int(row_id), term) for term in sorted(extract_terms(content_row["content"]))[:120]],
                        )
                    if target_table:
                        conn.execute(f"DELETE FROM {target_table} WHERE rowid=?", (int(row_id),))
                        conn.execute(f"INSERT INTO {target_table}(rowid,embedding) VALUES (?,?)",
                                     (int(row_id), blob))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return updated

    def replace_chunk_embeddings(self, rows: list[tuple[int, list[float]]], runtime_gen: str = "") -> int:
        if not rows:
            return 0
        with self._lock:
            conn = self._get_conn()
            gen = runtime_gen or self._gen.active_generation()
            target_active = self._gen.active_generation()
            target_table = self._gen.table_name_for("chunk", gen) if self._gen._vec_ok and gen else ""
            updated = 0
            try:
                conn.execute("BEGIN")
                for chunk_id, vector in rows:
                    blob = serialize_f32(vector)
                    exists = conn.execute("SELECT 1 FROM source_turn_chunks WHERE id=?", (int(chunk_id),)).fetchone()
                    if not exists:
                        continue
                    conn.execute(
                        """INSERT OR REPLACE INTO embedding_versions
                           (kind,row_id,generation,embedding,created_ts) VALUES ('chunk',?,?,?,?)""",
                        (int(chunk_id), gen, blob, time.time()),
                    )
                    if gen == target_active:
                        conn.execute(
                            "UPDATE source_turn_chunks SET embedding=?,embedding_generation=? WHERE id=?",
                            (blob, gen, int(chunk_id)),
                        )
                    updated += 1
                    if target_table:
                        conn.execute(f"DELETE FROM {target_table} WHERE rowid=?", (int(chunk_id),))
                        conn.execute(f"INSERT INTO {target_table}(rowid,embedding) VALUES (?,?)",
                                     (int(chunk_id), blob))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return updated

    # ---- search --------------------------------------------------------

    def search_strict_source(self, query_vec: list[float], query_text: str,
                             limit: int = 24) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            conn = self._get_conn()
            active_gen = self._gen.active_generation()
            candidates: dict[int, dict[str, Any]] = {}
            if self._gen._vec_ok and query_vec:
                # whole-turn vectors (active generation)
                tbl = self._gen.table_name("source")
                if tbl:
                    try:
                        rows = conn.execute(
                            f"""SELECT s.*,v.distance AS distance
                                FROM {tbl} v JOIN source_turns s ON s.id=v.rowid
                                WHERE v.embedding MATCH ? AND k=? ORDER BY v.distance""",
                            (serialize_f32(query_vec), max(limit * 3, 36)),
                        ).fetchall()
                        for row in rows:
                            item = dict(row)
                            item["semantic"] = 1.0 - float(item.pop("distance") or 0.0)
                            candidates[int(item["id"])] = item
                    except Exception as exc:
                        logger.debug("[memory][archive] source vector search failed: %s", exc)
                # chunk vectors: chunk hit rolls up to its owning turn
                chunk_tbl = self._gen.table_name("chunk")
                if chunk_tbl:
                    try:
                        rows = conn.execute(
                            f"""SELECT s.*,c.chunk_index AS chunk_index,v.distance AS distance
                                FROM {chunk_tbl} v
                                JOIN source_turn_chunks c ON c.id=v.rowid
                                JOIN source_turns s ON s.id=c.turn_id
                                WHERE v.embedding MATCH ? AND k=? ORDER BY v.distance""",
                            (serialize_f32(query_vec), max(limit * 3, 36)),
                        ).fetchall()
                        for row in rows:
                            item = dict(row)
                            semantic = 1.0 - float(item.pop("distance") or 0.0)
                            existing = candidates.get(int(item["id"]))
                            if existing is None or semantic > float(existing.get("semantic") or 0.0):
                                item["semantic"] = semantic
                                item["chunk_hit"] = True
                                candidates[int(item["id"])] = item
                    except Exception as exc:
                        logger.debug("[memory][archive] source chunk vector search failed: %s", exc)
            if not candidates and query_vec:
                if active_gen:
                    rows = conn.execute(
                        """SELECT s.*,ev.embedding AS version_embedding
                           FROM embedding_versions ev JOIN source_turns s ON s.id=ev.row_id
                           WHERE ev.kind='source' AND ev.generation=?""",
                        (active_gen,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT s.*,s.embedding AS version_embedding FROM source_turns s WHERE s.embedding IS NOT NULL"
                    ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["semantic"] = cosine(
                        query_vec, deserialize_f32(item.pop("version_embedding", None))
                    )
                    candidates[int(item["id"])] = item
                # Python fallback also searches child chunks and rolls the best
                # child match up to its owning full source turn.
                if active_gen:
                    chunk_rows = conn.execute(
                        """SELECT s.*,c.chunk_index,ev.embedding AS version_embedding
                           FROM embedding_versions ev
                           JOIN source_turn_chunks c ON c.id=ev.row_id
                           JOIN source_turns s ON s.id=c.turn_id
                           WHERE ev.kind='chunk' AND ev.generation=?""",
                        (active_gen,),
                    ).fetchall()
                else:
                    chunk_rows = conn.execute(
                        """SELECT s.*,c.chunk_index,c.embedding AS version_embedding
                           FROM source_turn_chunks c JOIN source_turns s ON s.id=c.turn_id
                           WHERE c.embedding IS NOT NULL"""
                    ).fetchall()
                for row in chunk_rows:
                    item = dict(row)
                    semantic = cosine(
                        query_vec, deserialize_f32(item.pop("version_embedding", None))
                    )
                    existing = candidates.get(int(item["id"]))
                    if existing is None or semantic > float(existing.get("semantic") or 0.0):
                        item["semantic"] = semantic
                        item["chunk_hit"] = True
                        candidates[int(item["id"])] = item

            query_terms = extract_terms(query_text)
            if query_terms:
                terms = sorted(query_terms)[:32]
                placeholders = ",".join("?" for _ in terms)
                lexical_rows = conn.execute(
                    f"""SELECT s.*,COUNT(*) AS term_hits
                        FROM source_turn_terms t JOIN source_turns s ON s.id=t.turn_id
                        WHERE t.term IN ({placeholders})
                        GROUP BY s.id ORDER BY term_hits DESC,s.id DESC LIMIT ?""",
                    (*terms, max(limit * 4, 48)),
                ).fetchall()
                for row in lexical_rows:
                    row_id = int(row["id"])
                    if row_id not in candidates:
                        item = dict(row)
                        item["semantic"] = cosine(
                            query_vec, deserialize_f32(item.get("embedding"))
                        ) if query_vec else 0.0
                        candidates[row_id] = item

            out = []
            for item in candidates.values():
                content = str(item.get("content") or "")
                content_terms = extract_terms(content)
                overlap = len(query_terms & content_terms) / max(1, len(query_terms)) if query_terms else 0.0
                exact = sum(1 for term in query_terms if term in content.lower())
                exact_norm = exact / max(1, len(query_terms)) if query_terms else 0.0
                semantic = max(-1.0, min(1.0, float(item.get("semantic") or 0.0)))
                lexical = max(overlap, exact_norm)
                item["lexical"] = round(lexical, 4)
                item["relevance"] = round(max(0.0, semantic * 0.78 + lexical * 0.22), 6)
                item["score"] = round(semantic * 0.68 + overlap * 0.20 + exact_norm * 0.12, 6)
                item.pop("embedding", None)
                out.append(item)
            out.sort(key=lambda i: (float(i.get("score") or 0.0), float(i.get("relevance") or 0.0)), reverse=True)
            return out[:limit]

    # ---- stats --------------------------------------------------------

    def counts(self) -> dict[str, int]:
        conn = self._get_conn()
        return {
            "batches": int(conn.execute("SELECT COUNT(*) AS n FROM source_batches").fetchone()["n"]),
            "turns": int(conn.execute("SELECT COUNT(*) AS n FROM source_turns").fetchone()["n"]),
            "turn_vectors": int(conn.execute(
                "SELECT COUNT(*) AS n FROM source_turns WHERE embedding IS NOT NULL").fetchone()["n"]),
            "chunks": int(conn.execute("SELECT COUNT(*) AS n FROM source_turn_chunks").fetchone()["n"]),
            "chunk_vectors": int(conn.execute(
                "SELECT COUNT(*) AS n FROM source_turn_chunks WHERE embedding IS NOT NULL").fetchone()["n"]),
            "turn_terms": int(conn.execute("SELECT COUNT(*) AS n FROM source_turn_terms").fetchone()["n"]),
        }