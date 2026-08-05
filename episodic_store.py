"""Persistent episodic memory and source-evidence store.

Memos remains the human-readable source of active diary truth. This database
stores lossless source turns and rebuildable machine views keyed by memo name.
"""
from __future__ import annotations

import hashlib
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


logger = logging.getLogger(__name__)


def _serialize_f32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_f32(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _terms(text: str) -> set[str]:
    source = " ".join(str(text or "").lower().split())
    if not source:
        return set()
    out = set(re.findall(r"[a-z0-9_/-]{2,}|[\u4e00-\u9fff]{2,8}", source))
    try:
        import jieba
        out.update(word.strip().lower() for word in jieba.lcut(source) if len(word.strip()) >= 2)
    except Exception:
        out.update(source[index:index + 2] for index in range(max(0, len(source) - 1)))
    return {item for item in out if item}


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so tokens containing %/_ match literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class EpisodicStore:
    """Owns raw conversation evidence and derived episode cards."""

    SCHEMA_VERSION = 3

    def __init__(self, db_path: str, emb_dim: Optional[int], emb_model_id: Optional[str]):
        self.db_path = db_path
        self.emb_dim = int(emb_dim) if emb_dim else None
        self.emb_model_id = str(emb_model_id or "unknown")
        self._conn: sqlite3.Connection | None = None
        self._vec_ok = False
        self._lock = threading.RLock()

    async def init(self) -> None:
        self._connect()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
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
        self._init_schema(conn)
        self._ensure_embedding_contract(conn)
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
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
                UNIQUE(batch_id, turn_index),
                FOREIGN KEY(batch_id) REFERENCES source_batches(batch_id) ON DELETE CASCADE
            )"""
        )
        source_turn_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(source_turns)").fetchall()
        }
        if "embedding" not in source_turn_columns:
            conn.execute("ALTER TABLE source_turns ADD COLUMN embedding BLOB")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS source_turn_terms (
                turn_id INTEGER NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY(turn_id, term),
                FOREIGN KEY(turn_id) REFERENCES source_turns(id) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id TEXT NOT NULL UNIQUE,
                memo_name TEXT NOT NULL UNIQUE,
                source_batch_id TEXT,
                source_kind TEXT DEFAULT 'legacy',
                legacy INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                occurred_at TEXT DEFAULT '',
                event_ts REAL DEFAULT 0,
                time_basis TEXT DEFAULT 'unknown',
                memory_type TEXT DEFAULT 'plot_fact',
                importance INTEGER DEFAULT 3,
                scene_anchor TEXT DEFAULT '',
                retrieval_key TEXT DEFAULT '',
                state_change TEXT DEFAULT '',
                long_effect TEXT DEFAULT '',
                trigger_hint TEXT DEFAULT '',
                entities_json TEXT DEFAULT '[]',
                affect_before TEXT DEFAULT '',
                affect_after TEXT DEFAULT '',
                unresolved_json TEXT DEFAULT '[]',
                card_text TEXT NOT NULL,
                diary_content_hash TEXT DEFAULT '',
                evidence_quality TEXT DEFAULT 'diary_derived',
                source_updated_ts REAL DEFAULT 0,
                embedding BLOB,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL,
                FOREIGN KEY(source_batch_id) REFERENCES source_batches(batch_id) ON DELETE SET NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS episode_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id TEXT NOT NULL,
                evidence_index INTEGER NOT NULL,
                kind TEXT DEFAULT 'event',
                actor TEXT DEFAULT '',
                detail TEXT NOT NULL,
                quote_text TEXT DEFAULT '',
                turn_indexes_json TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                grounded INTEGER NOT NULL DEFAULT 0,
                UNIQUE(episode_id, evidence_index),
                FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS episode_turn_links (
                episode_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                evidence_index INTEGER NOT NULL,
                PRIMARY KEY(episode_id, turn_index, evidence_index),
                FOREIGN KEY(episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE,
                FOREIGN KEY(batch_id) REFERENCES source_batches(batch_id) ON DELETE CASCADE
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_active ON episodes(active, event_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_memo ON episodes(memo_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_turn_batch ON source_turns(batch_id, turn_index)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source_turn_term ON source_turn_terms(term, turn_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_episode ON episode_evidence(episode_id, evidence_index)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_turn_link ON episode_turn_links(batch_id, turn_index)")
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recall_eval_cases (
                case_id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                expected_memos_json TEXT NOT NULL DEFAULT '[]',
                note TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                source TEXT DEFAULT 'manual',
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recall_eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL NOT NULL,
                mode TEXT NOT NULL,
                case_count INTEGER NOT NULL,
                metrics_json TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '[]'
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_versions_scope ON semantic_state_versions(scope_id, version DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_queue_status ON semantic_state_queue(status, updated_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_cases_enabled ON recall_eval_cases(enabled, updated_ts DESC)")
        legacy_links = conn.execute(
            """SELECT e.episode_id,e.source_batch_id,v.evidence_index,v.turn_indexes_json
               FROM episodes e JOIN episode_evidence v ON v.episode_id=e.episode_id
               WHERE e.source_batch_id IS NOT NULL AND e.source_batch_id!=''"""
        ).fetchall()
        for row in legacy_links:
            for value in _json_list(row["turn_indexes_json"]):
                try:
                    turn_index = int(value)
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO episode_turn_links
                       (episode_id,batch_id,turn_index,evidence_index) VALUES (?,?,?,?)""",
                    (
                        row["episode_id"], row["source_batch_id"], turn_index,
                        int(row["evidence_index"] or 0),
                    ),
                )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(self.SCHEMA_VERSION),),
        )
        conn.commit()

    def _ensure_embedding_contract(self, conn: sqlite3.Connection) -> None:
        old_dim_row = conn.execute("SELECT value FROM meta WHERE key='emb_dim'").fetchone()
        old_model_row = conn.execute("SELECT value FROM meta WHERE key='emb_model_id'").fetchone()
        old_dim = int(old_dim_row["value"] or 0) if old_dim_row else 0
        old_model = str(old_model_row["value"] or "") if old_model_row else ""
        changed = bool(
            (old_dim and self.emb_dim and old_dim != self.emb_dim)
            or (old_model and self.emb_model_id not in {"", "unknown"} and old_model != self.emb_model_id)
        )
        if changed:
            conn.execute("UPDATE episodes SET embedding=NULL")
            conn.execute("UPDATE source_turns SET embedding=NULL")
            try:
                conn.execute("DROP TABLE IF EXISTS vec_episode_cards")
                conn.execute("DROP TABLE IF EXISTS vec_source_turns")
            except Exception:
                pass
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('emb_dim',?)", (str(self.emb_dim or ""),))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('emb_model_id',?)", (self.emb_model_id,))
        if self._vec_ok and self.emb_dim:
            try:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_episode_cards "
                    f"USING vec0(embedding float[{self.emb_dim}] distance_metric=cosine)"
                )
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_source_turns "
                    f"USING vec0(embedding float[{self.emb_dim}] distance_metric=cosine)"
                )
            except Exception as exc:
                logger.warning("[memos-memory][episode] vec episode table failed: %s", exc)
                self._vec_ok = False
        conn.commit()

    async def ensure_dim(self, dim: int, model_id: str) -> None:
        with self._lock:
            self.emb_dim = int(dim)
            self.emb_model_id = str(model_id or "unknown")
            self._ensure_embedding_contract(self._connect())

    @staticmethod
    def episode_id_for_memo(memo_name: str) -> str:
        return "ep_" + hashlib.sha256(str(memo_name).encode("utf-8")).hexdigest()[:24]

    def archive_batch(self, session_id: str, messages: list[dict[str, Any]], source_kind: str) -> str:
        normalized = []
        for index, message in enumerate(messages or []):
            content = str(message.get("content") or "").strip()
            if not content:
                continue
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
            conn = self._connect()
            conn.execute(
                """INSERT OR IGNORE INTO source_batches
                   (batch_id,session_id,source_kind,content_hash,message_count,first_event_ts,last_event_ts,status,created_ts,updated_ts)
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
                    (
                        batch_id, row["turn_index"], row["role"], row["content"],
                        row["event_ts"], row["event_timezone"], content_hash,
                    ),
                )
                saved = conn.execute(
                    "SELECT id FROM source_turns WHERE batch_id=? AND turn_index=?",
                    (batch_id, row["turn_index"]),
                ).fetchone()
                if saved:
                    conn.executemany(
                        "INSERT OR IGNORE INTO source_turn_terms(turn_id,term) VALUES (?,?)",
                        [(int(saved["id"]), term) for term in sorted(_terms(row["content"]))[:120]],
                    )
            conn.commit()
        return batch_id

    def mark_batch(self, batch_id: str, status: str) -> None:
        if not batch_id:
            return
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE source_batches SET status=?,updated_ts=? WHERE batch_id=?",
                (str(status), time.time(), batch_id),
            )
            conn.commit()

    def source_turns(self, batch_id: str) -> list[dict[str, Any]]:
        if not batch_id:
            return []
        rows = self._connect().execute(
            """SELECT turn_index,role,content,event_ts,event_timezone
               FROM source_turns WHERE batch_id=? ORDER BY turn_index""",
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def source_turn_embedding_rows(
        self,
        *,
        batch_id: str = "",
        after_id: int = 0,
        limit: int = 64,
        missing_only: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = ["id>?"]
        params: list[Any] = [max(0, int(after_id))]
        if batch_id:
            clauses.append("batch_id=?")
            params.append(str(batch_id))
        if missing_only:
            clauses.append("embedding IS NULL")
        params.append(max(1, min(500, int(limit))))
        rows = self._connect().execute(
            f"""SELECT id,batch_id,turn_index,role,content,event_ts,event_timezone
                 FROM source_turns WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_source_turn_embeddings(self, rows: list[tuple[int, list[float]]]) -> int:
        if not rows:
            return 0
        with self._lock:
            conn = self._connect()
            updated = 0
            try:
                conn.execute("BEGIN")
                for row_id, vector in rows:
                    blob = _serialize_f32(vector)
                    cur = conn.execute(
                        "UPDATE source_turns SET embedding=? WHERE id=?",
                        (blob, int(row_id)),
                    )
                    if not cur.rowcount:
                        continue
                    updated += 1
                    content_row = conn.execute(
                        "SELECT content FROM source_turns WHERE id=?", (int(row_id),)
                    ).fetchone()
                    if content_row:
                        conn.executemany(
                            "INSERT OR IGNORE INTO source_turn_terms(turn_id,term) VALUES (?,?)",
                            [
                                (int(row_id), term)
                                for term in sorted(_terms(content_row["content"]))[:120]
                            ],
                        )
                    if self._vec_ok:
                        conn.execute("DELETE FROM vec_source_turns WHERE rowid=?", (int(row_id),))
                        conn.execute(
                            "INSERT INTO vec_source_turns(rowid,embedding) VALUES (?,?)",
                            (int(row_id), blob),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return updated

    def source_turn_vector_count(self) -> int:
        row = self._connect().execute(
            "SELECT COUNT(*) AS n FROM source_turns WHERE embedding IS NOT NULL"
        ).fetchone()
        return int(row["n"] or 0) if row else 0

    def upsert_episode(
        self,
        *,
        memo_name: str,
        episode: dict[str, Any],
        card_text: str,
        embedding: list[float] | None,
        source_batch_id: str = "",
        source_kind: str = "legacy",
        legacy: bool = True,
        evidence_quality: str = "diary_derived",
        diary_content_hash: str = "",
        source_updated_ts: float = 0.0,
    ) -> str:
        memo_name = str(memo_name or "").strip()
        if not memo_name:
            raise ValueError("memo_name is required")
        episode_id = str(episode.get("episode_id") or self.episode_id_for_memo(memo_name))
        entities = episode.get("entities") if isinstance(episode.get("entities"), list) else []
        unresolved = episode.get("unresolved") if isinstance(episode.get("unresolved"), list) else []
        raw_evidence = episode.get("evidence") if isinstance(episode.get("evidence"), list) else []
        evidence = []
        seen_evidence: set[tuple[Any, ...]] = set()
        for item in raw_evidence:
            if not isinstance(item, dict):
                continue
            indexes = item.get("turn_indexes") if isinstance(item.get("turn_indexes"), list) else []
            signature = (
                str(item.get("kind") or "event").strip(),
                str(item.get("actor") or "").strip(),
                " ".join(str(item.get("detail") or item.get("fact") or "").split()),
                " ".join(str(item.get("quote") or item.get("quote_text") or "").split()),
                tuple(indexes),
            )
            if signature in seen_evidence:
                continue
            seen_evidence.add(signature)
            evidence.append(item)
        now = time.time()
        with self._lock:
            conn = self._connect()
            conn.execute(
                """INSERT INTO episodes
                   (episode_id,memo_name,source_batch_id,source_kind,legacy,active,occurred_at,event_ts,time_basis,
                    memory_type,importance,scene_anchor,retrieval_key,state_change,long_effect,trigger_hint,
                    entities_json,affect_before,affect_after,unresolved_json,card_text,diary_content_hash,
                    evidence_quality,source_updated_ts,embedding,created_ts,updated_ts)
                   VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(memo_name) DO UPDATE SET
                    episode_id=excluded.episode_id,source_batch_id=excluded.source_batch_id,
                    source_kind=excluded.source_kind,legacy=excluded.legacy,active=1,
                    occurred_at=excluded.occurred_at,event_ts=excluded.event_ts,time_basis=excluded.time_basis,
                    memory_type=excluded.memory_type,importance=excluded.importance,
                    scene_anchor=excluded.scene_anchor,retrieval_key=excluded.retrieval_key,
                    state_change=excluded.state_change,long_effect=excluded.long_effect,
                    trigger_hint=excluded.trigger_hint,entities_json=excluded.entities_json,
                    affect_before=excluded.affect_before,affect_after=excluded.affect_after,
                    unresolved_json=excluded.unresolved_json,card_text=excluded.card_text,
                    diary_content_hash=excluded.diary_content_hash,evidence_quality=excluded.evidence_quality,
                    source_updated_ts=excluded.source_updated_ts,embedding=excluded.embedding,updated_ts=excluded.updated_ts""",
                (
                    episode_id, memo_name, source_batch_id or None, source_kind, 1 if legacy else 0,
                    str(episode.get("occurred_at") or ""), float(episode.get("event_ts") or 0),
                    str(episode.get("time_basis") or "unknown"),
                    str(episode.get("memory_type") or "plot_fact"),
                    max(1, min(5, int(episode.get("importance") or 3))),
                    str(episode.get("scene_anchor") or ""), str(episode.get("retrieval_key") or ""),
                    str(episode.get("state_change") or ""), str(episode.get("long_effect") or ""),
                    str(episode.get("trigger_hint") or ""),
                    json.dumps(entities, ensure_ascii=False), str(episode.get("affect_before") or ""),
                    str(episode.get("affect_after") or ""), json.dumps(unresolved, ensure_ascii=False),
                    str(card_text or ""), str(diary_content_hash or ""), str(evidence_quality),
                    float(source_updated_ts or 0), _serialize_f32(embedding) if embedding else None, now, now,
                ),
            )
            row = conn.execute("SELECT id FROM episodes WHERE memo_name=?", (memo_name,)).fetchone()
            row_id = int(row["id"])
            conn.execute("DELETE FROM episode_evidence WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM episode_turn_links WHERE episode_id=?", (episode_id,))
            for index, item in enumerate(evidence):
                if not isinstance(item, dict):
                    continue
                detail = str(item.get("detail") or item.get("fact") or "").strip()
                quote = str(item.get("quote") or item.get("quote_text") or "").strip()
                if not detail and not quote:
                    continue
                conn.execute(
                    """INSERT INTO episode_evidence
                       (episode_id,evidence_index,kind,actor,detail,quote_text,turn_indexes_json,confidence,grounded)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        episode_id, index, str(item.get("kind") or "event"), str(item.get("actor") or ""),
                        detail or quote, quote,
                        json.dumps(item.get("turn_indexes") if isinstance(item.get("turn_indexes"), list) else []),
                        max(0.0, min(1.0, float(item.get("confidence") or 0.5))),
                        1 if item.get("grounded") else 0,
                    ),
                )
                if source_batch_id:
                    turn_indexes = (
                        item.get("turn_indexes") if isinstance(item.get("turn_indexes"), list) else []
                    )
                    for value in turn_indexes:
                        try:
                            turn_index = int(value)
                        except (TypeError, ValueError):
                            continue
                        conn.execute(
                            """INSERT OR IGNORE INTO episode_turn_links
                               (episode_id,batch_id,turn_index,evidence_index) VALUES (?,?,?,?)""",
                            (episode_id, source_batch_id, turn_index, index),
                        )
            if self._vec_ok:
                try:
                    conn.execute("DELETE FROM vec_episode_cards WHERE rowid=?", (row_id,))
                    if embedding:
                        conn.execute(
                            "INSERT INTO vec_episode_cards(rowid,embedding) VALUES (?,?)",
                            (row_id, _serialize_f32(embedding)),
                        )
                except Exception as exc:
                    logger.debug("[memos-memory][episode] card vector upsert failed: %s", exc)
            conn.commit()
        return episode_id

    def delete_by_memo_name(self, memo_name: str) -> int:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT id FROM episodes WHERE memo_name=?", (memo_name,)).fetchone()
            if not row:
                return 0
            row_id = int(row["id"])
            conn.execute("DELETE FROM episodes WHERE id=?", (row_id,))
            if self._vec_ok:
                try:
                    conn.execute("DELETE FROM vec_episode_cards WHERE rowid=?", (row_id,))
                except Exception:
                    pass
            conn.commit()
            return 1

    def memo_names(self) -> set[str]:
        rows = self._connect().execute("SELECT memo_name FROM episodes WHERE active=1").fetchall()
        return {str(row["memo_name"]) for row in rows}

    def get_episode(self, memo_name: str) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM episodes WHERE memo_name=? AND active=1", (memo_name,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["entities"] = _json_list(data.pop("entities_json", "[]"))
        data["unresolved"] = _json_list(data.pop("unresolved_json", "[]"))
        data["embedding_ready"] = bool(data.get("embedding"))
        data.pop("embedding", None)
        return data

    def list_episodes(
        self,
        *,
        query: str = "",
        quality: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return active episode cards without exposing embedding blobs."""
        clauses = ["active=1"]
        params: list[Any] = []
        quality = str(quality or "").strip()
        if quality:
            clauses.append("evidence_quality=?")
            params.append(quality)
        rows = self._connect().execute(
            f"""SELECT episode_id,memo_name,source_batch_id,source_kind,legacy,
                       occurred_at,event_ts,time_basis,memory_type,importance,
                       scene_anchor,retrieval_key,state_change,long_effect,trigger_hint,
                       entities_json,affect_before,affect_after,unresolved_json,card_text,
                       diary_content_hash,evidence_quality,source_updated_ts,created_ts,updated_ts
                FROM episodes WHERE {' AND '.join(clauses)}
                ORDER BY event_ts DESC, updated_ts DESC LIMIT ? OFFSET ?""",
            (*params, max(1, min(1000, int(limit))), max(0, int(offset))),
        ).fetchall()
        query_terms = _terms(query)
        result = []
        for row in rows:
            item = dict(row)
            if query_terms:
                haystack = " ".join(
                    str(item.get(key) or "").lower()
                    for key in (
                        "memo_name", "occurred_at", "memory_type", "scene_anchor",
                        "retrieval_key", "state_change", "long_effect", "trigger_hint", "card_text",
                    )
                )
                if not any(term in haystack for term in query_terms):
                    continue
            item["entities"] = _json_list(item.pop("entities_json", "[]"))
            item["unresolved"] = _json_list(item.pop("unresolved_json", "[]"))
            result.append(item)
        return result

    def episode_detail(self, memo_name: str) -> dict[str, Any] | None:
        episode = self.get_episode(memo_name)
        if not episode:
            return None
        episode["evidence"] = self.evidence_for_memo(memo_name, "", limit=100)
        episode["source_turns"] = self.source_turns(str(episode.get("source_batch_id") or ""))
        return episode

    def episodes_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        if not batch_id:
            return []
        with self._lock:
            rows = self._connect().execute(
                "SELECT memo_name FROM episodes WHERE active=1 AND source_batch_id=? ORDER BY event_ts,id",
                (str(batch_id),),
            ).fetchall()
        out = []
        for row in rows:
            detail = self.episode_detail(str(row["memo_name"]))
            if detail:
                out.append(detail)
        return out

    def batch_status(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            """SELECT batch_id,session_id,source_kind,message_count,first_event_ts,last_event_ts,
                      status,created_ts,updated_ts
               FROM source_batches ORDER BY updated_ts DESC LIMIT ?""",
            (max(1, min(500, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

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
                    episode_ids_json=excluded.episode_ids_json,status='pending',last_error='',updated_ts=excluded.updated_ts""",
                (batch_id, str(scope_id), json.dumps(episode_ids, ensure_ascii=False), now, now),
            )
            conn.commit()

    def pending_state_updates(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connect().execute(
                """SELECT batch_id,scope_id,episode_ids_json,status,attempts,last_error,created_ts,updated_ts
                   FROM semantic_state_queue WHERE status IN ('pending','failed')
                   ORDER BY created_ts LIMIT ?""",
                (max(1, min(100, int(limit))),),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["episode_ids"] = _json_list(item.pop("episode_ids_json", "[]"))
            out.append(item)
        return out

    def mark_state_update(self, batch_id: str, status: str, error: str = "") -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """UPDATE semantic_state_queue SET status=?,attempts=attempts+1,last_error=?,updated_ts=?
                   WHERE batch_id=?""",
                (str(status), str(error or "")[:1000], time.time(), str(batch_id)),
            )
            conn.commit()

    def supersede_pending_state_updates(self, reason: str = "full_rebuild") -> int:
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """UPDATE semantic_state_queue SET status='superseded',last_error=?,updated_ts=?
                   WHERE status IN ('pending','failed')""",
                (str(reason or "full_rebuild")[:1000], time.time()),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def upsert_eval_case(
        self,
        query: str,
        expected_memos: list[str],
        *,
        case_id: str = "",
        note: str = "",
        source: str = "manual",
        enabled: bool = True,
    ) -> dict[str, Any]:
        query = " ".join(str(query or "").split()).strip()
        expected = list(dict.fromkeys(str(value).strip() for value in expected_memos if str(value).strip()))
        if not query or not expected:
            raise ValueError("query and expected_memos are required")
        case_id = str(case_id or "").strip() or "case_" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:20]
        now = time.time()
        with self._lock:
            conn = self._connect()
            existing = conn.execute(
                "SELECT created_ts,expected_memos_json FROM recall_eval_cases WHERE case_id=?", (case_id,)
            ).fetchone()
            if existing:
                expected = list(dict.fromkeys(_json_list(existing["expected_memos_json"]) + expected))
            created_ts = float(existing["created_ts"] or now) if existing else now
            conn.execute(
                """INSERT OR REPLACE INTO recall_eval_cases
                   (case_id,query,expected_memos_json,note,enabled,source,created_ts,updated_ts)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (case_id, query, json.dumps(expected, ensure_ascii=False), str(note or ""), 1 if enabled else 0,
                 str(source or "manual"), created_ts, now),
            )
            conn.commit()
        return {"case_id": case_id, "query": query, "expected_memos": expected, "enabled": bool(enabled)}

    def list_eval_cases(self, enabled_only: bool = False, limit: int = 300) -> list[dict[str, Any]]:
        sql = "SELECT * FROM recall_eval_cases"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY updated_ts DESC LIMIT ?"
        with self._lock:
            rows = self._connect().execute(sql, (max(1, min(2000, int(limit))),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["expected_memos"] = _json_list(item.pop("expected_memos_json", "[]"))
            item["enabled"] = bool(item.get("enabled"))
            out.append(item)
        return out

    def delete_eval_case(self, case_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            cur = conn.execute("DELETE FROM recall_eval_cases WHERE case_id=?", (str(case_id),))
            conn.commit()
            return bool(cur.rowcount)

    def record_eval_run(self, mode: str, metrics: dict[str, Any], details: list[dict[str, Any]]) -> int:
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """INSERT INTO recall_eval_runs(created_ts,mode,case_count,metrics_json,details_json)
                   VALUES (?,?,?,?,?)""",
                (time.time(), str(mode), len(details), json.dumps(metrics, ensure_ascii=False),
                 json.dumps(details, ensure_ascii=False)),
            )
            conn.commit()
            return int(cur.lastrowid)

    def evidence_for_memo(self, memo_name: str, query: str = "", limit: int = 3) -> list[dict[str, Any]]:
        episode = self.get_episode(memo_name)
        if not episode:
            return []
        rows = self._connect().execute(
            """SELECT kind,actor,detail,quote_text,turn_indexes_json,confidence,grounded
               FROM episode_evidence WHERE episode_id=? ORDER BY evidence_index""",
            (episode["episode_id"],),
        ).fetchall()
        query_terms = _terms(query)
        scored = []
        for index, row in enumerate(rows):
            data = dict(row)
            text = " ".join([data.get("actor") or "", data.get("detail") or "", data.get("quote_text") or ""])
            item_terms = _terms(text)
            overlap = len(query_terms & item_terms) / max(1, len(query_terms)) if query_terms else 0.0
            score = overlap * 0.75 + float(data.get("confidence") or 0.0) * 0.20 + (0.05 if data.get("grounded") else 0.0)
            data["turn_indexes"] = _json_list(data.pop("turn_indexes_json", "[]"))
            data["match_score"] = round(score, 4)
            scored.append((score, -index, data))
        scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored[:max(0, int(limit))]]

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

    def search_source_turns(
        self,
        query_vec: list[float],
        query_text: str,
        limit: int = 24,
    ) -> list[dict[str, Any]]:
        """Search first-hand archived turns with semantic and lexical evidence."""
        limit = max(1, min(200, int(limit)))
        with self._lock:
            conn = self._connect()
            candidates: dict[int, dict[str, Any]] = {}
            if self._vec_ok and query_vec:
                try:
                    rows = conn.execute(
                        """SELECT s.*,v.distance AS distance
                           FROM vec_source_turns v JOIN source_turns s ON s.id=v.rowid
                           WHERE v.embedding MATCH ? AND k=? ORDER BY v.distance""",
                        (_serialize_f32(query_vec), max(limit * 3, 36)),
                    ).fetchall()
                    for row in rows:
                        item = dict(row)
                        item["semantic"] = 1.0 - float(item.pop("distance") or 0.0)
                        candidates[int(item["id"])] = item
                except Exception as exc:
                    logger.debug("[memos-memory][episode] source vector search failed: %s", exc)
            if not candidates and query_vec:
                rows = conn.execute(
                    "SELECT * FROM source_turns WHERE embedding IS NOT NULL"
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["semantic"] = _cosine(query_vec, _deserialize_f32(item.get("embedding")))
                    candidates[int(item["id"])] = item

            query_terms = _terms(query_text)
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
                        item["semantic"] = _cosine(
                            query_vec, _deserialize_f32(item.get("embedding"))
                        )
                        candidates[row_id] = item

            out = []
            for item in candidates.values():
                content = str(item.get("content") or "")
                content_terms = _terms(content)
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
            out.sort(
                key=lambda item: (float(item.get("score") or 0.0), float(item.get("relevance") or 0.0)),
                reverse=True,
            )
            return out[:limit]

    def search_cards(self, query_vec: list[float], query_text: str, limit: int = 18) -> list[dict[str, Any]]:
        with self._lock:
            return self._search_cards_locked(query_vec, query_text, limit)

    def _search_cards_locked(self, query_vec: list[float], query_text: str, limit: int = 18) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        conn = self._connect()
        candidates: dict[int, dict[str, Any]] = {}
        if self._vec_ok and query_vec:
            try:
                rows = conn.execute(
                    """SELECT e.*,v.distance AS distance
                       FROM vec_episode_cards v JOIN episodes e ON e.id=v.rowid
                       WHERE v.embedding MATCH ? AND k=? AND e.active=1 ORDER BY v.distance""",
                    (_serialize_f32(query_vec), max(limit * 3, 30)),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["semantic"] = 1.0 - float(item.pop("distance") or 0.0)
                    candidates[int(item["id"])] = item
            except Exception as exc:
                logger.debug("[memos-memory][episode] vector card search failed: %s", exc)
        if not candidates:
            rows = conn.execute("SELECT * FROM episodes WHERE active=1 AND embedding IS NOT NULL").fetchall()
            for row in rows:
                item = dict(row)
                item["semantic"] = _cosine(query_vec, _deserialize_f32(item.get("embedding")))
                candidates[int(item["id"])] = item

        query_terms = _terms(query_text)
        # Exact lexical evidence may rescue a card outside the ANN window.
        # 4.5: filter inside SQLite instead of pulling every episode row into
        # Python. Same predicate as before (substring hit on lowered card_text),
        # so recall is identical; only rows that actually match come back.
        if query_terms:
            terms = sorted(query_terms, key=len, reverse=True)[:64]
            like_clause = " OR ".join("LOWER(card_text) LIKE ? ESCAPE '\\'" for _ in terms)
            params = [f"%{_escape_like(term)}%" for term in terms]
            rows = conn.execute(
                f"SELECT * FROM episodes WHERE active=1 AND ({like_clause})",
                params,
            ).fetchall()
            for row in rows:
                item = dict(row)
                if int(item["id"]) not in candidates:
                    item["semantic"] = _cosine(query_vec, _deserialize_f32(item.get("embedding")))
                    candidates[int(item["id"])] = item

        out = []
        for item in candidates.values():
            card_terms = _terms(item.get("card_text") or "")
            overlap = len(query_terms & card_terms) / max(1, len(query_terms)) if query_terms else 0.0
            exact = sum(1 for term in query_terms if term in str(item.get("card_text") or "").lower())
            exact_norm = exact / max(1, len(query_terms)) if query_terms else 0.0
            semantic = max(-1.0, min(1.0, float(item.get("semantic") or 0.0)))
            importance = max(1, min(5, int(item.get("importance") or 3)))
            score = semantic * 0.72 + overlap * 0.16 + exact_norm * 0.08 + ((importance - 1) / 4) * 0.04
            item["lexical"] = round(max(overlap, exact_norm), 4)
            item["relevance"] = round(max(0.0, semantic * 0.82 + max(overlap, exact_norm) * 0.18), 6)
            item["score"] = round(score, 6)
            item["entities"] = _json_list(item.pop("entities_json", "[]"))
            item["unresolved"] = _json_list(item.pop("unresolved_json", "[]"))
            item.pop("embedding", None)
            out.append(item)
        out.sort(key=lambda item: (float(item.get("score") or 0.0), float(item.get("relevance") or 0.0)), reverse=True)
        return out[:limit]

    def stats(self) -> dict[str, Any]:
        conn = self._connect()
        episodes = int(conn.execute("SELECT COUNT(*) AS n FROM episodes WHERE active=1").fetchone()["n"])
        grounded = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1 AND evidence_quality='source_grounded'"
        ).fetchone()["n"])
        legacy = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1 AND evidence_quality='diary_derived'"
        ).fetchone()["n"])
        evidence = int(conn.execute("SELECT COUNT(*) AS n FROM episode_evidence").fetchone()["n"])
        batches = int(conn.execute("SELECT COUNT(*) AS n FROM source_batches").fetchone()["n"])
        turns = int(conn.execute("SELECT COUNT(*) AS n FROM source_turns").fetchone()["n"])
        source_turn_vectors = int(conn.execute(
            "SELECT COUNT(*) AS n FROM source_turns WHERE embedding IS NOT NULL"
        ).fetchone()["n"])
        missing_embeddings = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1 AND embedding IS NULL"
        ).fetchone()["n"])
        state_count = int(conn.execute("SELECT COUNT(*) AS n FROM semantic_states").fetchone()["n"])
        state_versions = int(conn.execute("SELECT COUNT(*) AS n FROM semantic_state_versions").fetchone()["n"])
        pending_state = int(conn.execute(
            "SELECT COUNT(*) AS n FROM semantic_state_queue WHERE status IN ('pending','failed')"
        ).fetchone()["n"])
        eval_cases = int(conn.execute("SELECT COUNT(*) AS n FROM recall_eval_cases WHERE enabled=1").fetchone()["n"])
        return {
            "episodes": episodes,
            "source_grounded": grounded,
            "diary_derived": legacy,
            "evidence": evidence,
            "source_batches": batches,
            "source_turns": turns,
            "source_turn_vectors": source_turn_vectors,
            "missing_embeddings": missing_embeddings,
            "semantic_states": state_count,
            "semantic_state_versions": state_versions,
            "pending_state_updates": pending_state,
            "recall_eval_cases": eval_cases,
            "vector_backend": "sqlite-vec" if self._vec_ok else "python-cosine",
            "db_path": self.db_path,
        }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
