"""
sqlite-vec 向量存储层(v1.0)。
- 分块嵌:每篇日记按句切 chunk,逐块存向量;检索命中 chunk -> 回 memos 取整篇
- 记录 emb 模型 id + 维度(meta 表),换模型能检测并重灌
- sqlite-vec 不可用时自动回退到纯 Python 余弦
- memos 是原文真相源,本库可随时 clear_all + reindex 重建

对外 API(与 main.py 对齐):
    await init()
    get_stored_meta() -> dict|None
    await ensure_dim(dim, model_id)
    await insert_chunks(memo_name, chunks, embeddings, ts_text, tags, importance, source_session, manual, memory_type, long_effect, trigger_hint, occurred_at, event_ts, time_basis, source_created_ts, source_updated_ts)
    await search_topk(query_vec, top_k, min_similarity) -> list[dict]
    await clear_all()
    close()
"""
from __future__ import annotations

import logging
import json
import math
import re
import sqlite3
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _serialize_f32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_f32(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)



# ---------- BM25 (pure stdlib) ----------
import re as _re
import time
import math as _math

class _BM25:
    """Okapi BM25 for Chinese text using character bigram tokenization."""
    def __init__(self, k1=1.5, b=0.75):
        self.k1 = k1
        self.b = b
        self._docs = []
        self._df = {}
        self._avgdl = 1.0
        self._n = 0

    @staticmethod
    def _tokenize(text, use_jieba=False):
        """Tokenize text for BM25. use_jieba=True uses jieba.lcut (better Chinese)."""
        if use_jieba:
            try:
                import jieba
                words = jieba.lcut(text)
                return [w.strip() for w in words if len(w.strip()) >= 1]
            except ImportError:
                pass
        # Fallback: character bigram + unigram
        text = _re.sub(r"[\s\n]+", " ", text.strip())
        tokens = []
        i = 0
        while i < len(text):
            if ord(text[i]) > 127:
                if i + 1 < len(text) and ord(text[i+1]) > 127:
                    tokens.append(text[i:i+2])
                tokens.append(text[i])
                i += 1
            else:
                m = _re.match(r"[a-zA-Z0-9]+", text[i:])
                if m:
                    tokens.append(m.group().lower())
                    i += m.end()
                else:
                    i += 1
        return tokens

    def build(self, docs, use_jieba=False):
        self._use_jieba = use_jieba
        self._docs = [self._tokenize(d, use_jieba) for d in docs]
        self._n = len(self._docs)
        total_len = 0
        self._df.clear()
        for toks in self._docs:
            total_len += len(toks)
            for t in set(toks):
                self._df[t] = self._df.get(t, 0) + 1
        self._avgdl = total_len / max(self._n, 1)

    def score(self, query, doc_idx):
        q_toks = self._tokenize(query, getattr(self, "_use_jieba", False))
        d_toks = self._docs[doc_idx]
        dl = len(d_toks)
        tf_map = {}
        for t in d_toks:
            tf_map[t] = tf_map.get(t, 0) + 1
        s = 0.0
        for qt in q_toks:
            tf = tf_map.get(qt, 0)
            df = self._df.get(qt, 0)
            idf = _math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)
            num = tf * (self.k1 + 1)
            denom = tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
            s += idf * num / denom
        return s

    def to_dict(self):
        """Serialize BM25 index for persistence."""
        return {
            "k1": self.k1, "b": self.b, "n": self._n, "use_jieba": getattr(self, "_use_jieba", False),
            "avgdl": self._avgdl,
            "docs": self._docs,
            "df": self._df,
        }

    @classmethod
    def from_dict(cls, d):
        """Deserialize BM25 index from dict."""
        obj = cls(k1=d["k1"], b=d["b"])
        obj._n = d["n"]
        obj._use_jieba = d.get("use_jieba", False)
        obj._avgdl = d["avgdl"]
        obj._docs = d["docs"]
        obj._df = d["df"]
        return obj

class VectorStore:
    def __init__(self, db_path: str, emb_dim: Optional[int], emb_model_id: Optional[str]):
        self.db_path = db_path
        self.emb_dim = emb_dim
        self.emb_model_id = emb_model_id or "unknown"
        self._conn: Optional[sqlite3.Connection] = None
        self._vec_ok = False
        self._bm25 = None
        self._bm25_ids = []
        self._bm25_use_jieba = False

    # ---------- 生命周期 ----------
    async def init(self) -> None:
        self._connect()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL mode: allows concurrent reads during writes, prevents corruption
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Integrity check on first connect
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result and result[0] != "ok":
                logger.warning("[memos-mem] DB integrity check failed: %s, rebuilding", result[0])
                conn.close()
                Path(self.db_path).unlink(missing_ok=True)
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        # 尝试加载 sqlite-vec
        try:
            conn.enable_load_extension(True)
            import sqlite_vec  # type: ignore
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._vec_ok = True
            logger.info("[memos-mem] sqlite-vec 已加载")
        except Exception as exc:
            self._vec_ok = False
            logger.warning("[memos-mem] sqlite-vec 不可用(%s),回退纯 Python 余弦", exc)
        self._conn = conn
        self._init_schema(conn)
        self._init_extra_tables()
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memo_name TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                ts_text TEXT,
                tags TEXT,
                importance INTEGER DEFAULT 3,
                source_session TEXT,
                manual INTEGER DEFAULT 0,
                memory_type TEXT DEFAULT 'plot_fact',
                long_effect TEXT DEFAULT '',
                trigger_hint TEXT DEFAULT '',
                created_ts REAL,
                occurred_at TEXT DEFAULT '',
                event_ts REAL DEFAULT 0,
                time_basis TEXT DEFAULT 'unknown',
                source_created_ts REAL DEFAULT 0,
                source_updated_ts REAL DEFAULT 0,
                indexed_ts REAL DEFAULT 0,
                passage_index INTEGER DEFAULT 0,
                char_start INTEGER DEFAULT 0,
                char_end INTEGER DEFAULT 0,
                content_hash TEXT DEFAULT '',
                scene_anchor TEXT DEFAULT '',
                retrieval_key TEXT DEFAULT '',
                state_change TEXT DEFAULT '',
                entities TEXT DEFAULT '',
                embedding BLOB
            )"""
        )
        self._ensure_column(conn, "chunks", "memory_type", "TEXT DEFAULT 'plot_fact'")
        self._ensure_column(conn, "chunks", "long_effect", "TEXT DEFAULT ''")
        self._ensure_column(conn, "chunks", "trigger_hint", "TEXT DEFAULT ''")
        self._ensure_column(conn, "chunks", "occurred_at", "TEXT DEFAULT ''")
        self._ensure_column(conn, "chunks", "event_ts", "REAL DEFAULT 0")
        self._ensure_column(conn, "chunks", "time_basis", "TEXT DEFAULT 'unknown'")
        self._ensure_column(conn, "chunks", "source_created_ts", "REAL DEFAULT 0")
        self._ensure_column(conn, "chunks", "source_updated_ts", "REAL DEFAULT 0")
        self._ensure_column(conn, "chunks", "indexed_ts", "REAL DEFAULT 0")
        self._ensure_column(conn, "chunks", "passage_index", "INTEGER DEFAULT 0")
        self._ensure_column(conn, "chunks", "char_start", "INTEGER DEFAULT 0")
        self._ensure_column(conn, "chunks", "char_end", "INTEGER DEFAULT 0")
        self._ensure_column(conn, "chunks", "content_hash", "TEXT DEFAULT ''")
        self._ensure_column(conn, "chunks", "scene_anchor", "TEXT DEFAULT ''")
        self._ensure_column(conn, "chunks", "retrieval_key", "TEXT DEFAULT ''")
        self._ensure_column(conn, "chunks", "state_change", "TEXT DEFAULT ''")
        self._ensure_column(conn, "chunks", "entities", "TEXT DEFAULT ''")
        conn.execute("UPDATE chunks SET indexed_ts=created_ts WHERE COALESCE(indexed_ts,0)=0 AND COALESCE(created_ts,0)>0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memo_name ON chunks(memo_name)")
        # 未压缩对话缓冲(进程重启不丢)。session_id 是 umo 或类似标识;
        # seq 是插入顺序,触发压缩时按 (session_id, seq) 取并整批删除。
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                seq INTEGER NOT NULL,
                created_ts REAL NOT NULL
            )"""
        )
        # Per-message event time was added in v3.2.3. Existing rows keep their
        # original created_ts as a lossless fallback, so upgrades do not rewrite
        # or discard a pending conversation buffer.
        self._ensure_column(conn, "pending_messages", "event_ts", "REAL DEFAULT 0")
        self._ensure_column(conn, "pending_messages", "event_timezone", "TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_messages(session_id, id)")
        if self._vec_ok and self.emb_dim:
            self._create_vec_table(conn, self.emb_dim)
        conn.commit()
        # 写/校验模型标记
        self._write_marker_if_absent(conn)

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _create_vec_table(self, conn: sqlite3.Connection, dim: int) -> None:
        try:
            # distance_metric=cosine: 让 v.distance 是余弦距离(0~2),score=1-dist 落在有意义区间。
            # 不指定则默认 L2 距离,距离常 >1,score=1-dist 会变负被 min_similarity 过滤掉 -> 检索永远空。
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{dim}] distance_metric=cosine)"
            )
        except Exception as exc:
            logger.warning("[memos-mem] vec0 建表失败: %s", exc)
            self._vec_ok = False

    def _write_marker_if_absent(self, conn: sqlite3.Connection) -> None:
        cur = conn.execute("SELECT value FROM meta WHERE key='emb_model_id'")
        row = cur.fetchone()
        if row is None:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('emb_model_id',?)", (self.emb_model_id,))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('emb_dim',?)", (str(self.emb_dim or ""),))
            conn.commit()

    # ---------- meta ----------
    def get_stored_meta(self) -> Optional[dict[str, Any]]:
        conn = self._connect()
        cur = conn.execute("SELECT key, value FROM meta")
        rows = cur.fetchall()
        if not rows:
            return None
        return {r["key"]: r["value"] for r in rows}

    def get_meta_value(self, key: str, default: str = "") -> str:
        row = self._connect().execute(
            "SELECT value FROM meta WHERE key=?", (str(key),)
        ).fetchone()
        return str(row["value"] if row else default)

    def set_meta_value(self, key: str, value: str) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            (str(key), str(value)),
        )
        conn.commit()

    def passage_embedding_rows(self, after_id: int = 0, limit: int = 48) -> list[dict[str, Any]]:
        """Return stable source rows for a resumable local-passage migration."""
        rows = self._connect().execute(
            """SELECT id,memo_name,chunk_text FROM chunks
               WHERE id>? ORDER BY id LIMIT ?""",
            (max(0, int(after_id)), max(1, min(256, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_chunk_embeddings(self, rows: list[tuple[int, list[float]]]) -> int:
        """Atomically replace passage vectors in both SQLite and sqlite-vec."""
        if not rows:
            return 0
        conn = self._connect()
        changed = 0
        try:
            for row_id, vector in rows:
                if not vector:
                    continue
                blob = _serialize_f32(vector)
                conn.execute("UPDATE chunks SET embedding=?,indexed_ts=? WHERE id=?", (blob, time.time(), int(row_id)))
                if self._vec_ok:
                    conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (int(row_id),))
                    conn.execute(
                        "INSERT INTO vec_chunks(rowid,embedding) VALUES(?,?)",
                        (int(row_id), blob),
                    )
                changed += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self.invalidate_bm25()
        return changed

    async def ensure_dim(self, dim: int, model_id: Optional[str] = None) -> None:
        """首次 embed 后推断出维度时补建向量表 + 写 meta。"""
        self.emb_dim = dim
        if model_id:
            self.emb_model_id = model_id
        conn = self._connect()
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('emb_dim',?)", (str(dim),))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('emb_model_id',?)", (self.emb_model_id,))
        conn.commit()
        if self._vec_ok:
            self._create_vec_table(conn, dim)
            conn.commit()

    # ---------- 分块(供 main.py 复用) ----------
    @staticmethod
    def chunk_text(text: str, max_chars: int = 60) -> list[str]:
        sentences = re.split(r"(?<=[。！？!?\n])\s*", text)
        chunks: list[str] = []
        cur = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if cur and len(cur) + len(s) > max_chars:
                chunks.append(cur)
                cur = s
            else:
                cur = (cur + " " + s).strip() if cur else s
        if cur:
            chunks.append(cur)
        return chunks or [text]

    # ---------- 写入 ----------
    async def insert_chunks(
        self,
        memo_name: str,
        chunks: list[str],
        embeddings: list[list[float]],
        ts_text: str = "",
        tags: Optional[list[str]] = None,
        importance: int = 3,
        source_session: str = "",
        manual: int = 0,
        memory_type: str = "plot_fact",
        long_effect: str = "",
        trigger_hint: str = "",
        occurred_at: str = "",
        event_ts: float = 0.0,
        time_basis: str = "unknown",
        source_created_ts: float = 0.0,
        source_updated_ts: float = 0.0,
        passages: Optional[list[dict[str, Any]]] = None,
        content_hash: str = "",
        scene_anchor: str = "",
        retrieval_key: str = "",
        state_change: str = "",
        entities: Optional[list[str]] = None,
    ) -> int:
        import time as _t
        conn = self._connect()
        tags_str = ",".join(tags or [])
        now = _t.time()
        legacy_created_ts = float(source_created_ts or event_ts or now)
        n = 0
        passage_rows = passages or []
        entities_text = ",".join(str(x).strip() for x in (entities or []) if str(x).strip())
        for passage_index, (text, vec) in enumerate(zip(chunks, embeddings)):
            if not text or not vec:
                continue
            passage = passage_rows[passage_index] if passage_index < len(passage_rows) else {}
            stored_text = str(passage.get("text") or text)
            char_start = max(0, int(passage.get("char_start") or 0))
            char_end = max(char_start, int(passage.get("char_end") or (char_start + len(stored_text))))
            # 首块时若还没建 vec 表(dim 未知),补建
            if self._vec_ok and self.emb_dim is None:
                self.emb_dim = len(vec)
                self._create_vec_table(conn, self.emb_dim)
            cur = conn.execute(
                """INSERT INTO chunks
                   (memo_name, chunk_text, ts_text, tags, importance, source_session, manual,
                    memory_type, long_effect, trigger_hint, created_ts,
                    occurred_at, event_ts, time_basis, source_created_ts, source_updated_ts,
                    indexed_ts, passage_index, char_start, char_end, content_hash,
                    scene_anchor, retrieval_key, state_change, entities, embedding)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    memo_name, stored_text, ts_text, tags_str, importance, source_session, manual,
                    memory_type or "plot_fact", long_effect or "", trigger_hint or "",
                    legacy_created_ts, occurred_at or "", float(event_ts or 0.0), time_basis or "unknown",
                    float(source_created_ts or 0.0), float(source_updated_ts or 0.0), now,
                    int(passage.get("passage_index") if passage.get("passage_index") is not None else passage_index),
                    char_start, char_end, content_hash or "", scene_anchor or "",
                    retrieval_key or "", state_change or "", entities_text,
                    _serialize_f32(vec),
                ),
            )
            rowid = cur.lastrowid
            if self._vec_ok:
                try:
                    conn.execute(
                        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?,?)",
                        (rowid, _serialize_f32(vec)),
                    )
                except Exception as exc:
                    logger.debug("[memos-mem] vec 插入失败: %s", exc)
            n += 1
        conn.commit()
        self.invalidate_bm25()
        self.invalidate_month_index()
        return n

    def invalidate_bm25(self) -> None:
        """Drop in-memory/persisted BM25 cache after index-changing writes."""
        self._bm25 = None
        self._bm25_ids = []
        try:
            conn = self._connect()
            conn.execute("DELETE FROM meta WHERE key='bm25_index'")
            conn.commit()
        except Exception:
            pass

    def invalidate_month_index(self) -> None:
        """Mark the derived month router stale without touching diary source data."""
        try:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('month_index_dirty','1')"
            )
            conn.commit()
        except Exception:
            pass

    # ---------- 对话缓冲(防重启丢失) ----------
    async def buffer_append(self, session_id: str, msgs: list[dict[str, str]]) -> None:
        """将对话缓冲追加进 sqlite。AstrBot 重启后能拿回。"""
        if not session_id or not msgs:
            return
        import time as _t
        conn = self._connect()
        # 计算该 session 当前最大 seq, 继续往下排
        cur = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM pending_messages WHERE session_id=?",
            (session_id,),
        )
        row = cur.fetchone()
        next_seq = (int(row["m"]) if row else -1) + 1
        now = _t.time()
        rows = []
        for i, m in enumerate(msgs):
            if not m.get("content"):
                continue
            try:
                event_ts = float(m.get("event_ts") or m.get("recorded_ts") or now)
            except (TypeError, ValueError):
                event_ts = now
            rows.append((
                session_id,
                m["role"],
                m["content"],
                next_seq + i,
                now,
                event_ts,
                str(m.get("event_timezone") or m.get("timezone") or ""),
            ))
        if not rows:
            return
        conn.executemany(
            """INSERT INTO pending_messages
               (session_id, role, content, seq, created_ts, event_ts, event_timezone)
               VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()

    @staticmethod
    def _buffer_row_dict(row) -> dict[str, Any]:
        event_ts = float(row["event_ts"] or row["created_ts"] or 0.0)
        return {
            "role": row["role"],
            "content": row["content"],
            "event_ts": event_ts,
            "recorded_ts": event_ts,
            "created_ts": float(row["created_ts"] or 0.0),
            "event_timezone": row["event_timezone"] or "",
        }

    async def buffer_take(self, session_id: str, last_seq: int = -1) -> list[dict[str, Any]]:
        """读取该 session 自 last_seq+1 起的所有缓冲消息(不删除)。
        返回 [{"role","content"}, ...], 按 seq 升序。
        """
        conn = self._connect()
        cur = conn.execute(
            """SELECT role, content, created_ts, event_ts, event_timezone
               FROM pending_messages WHERE session_id=? AND seq>? ORDER BY seq ASC""",
            (session_id, last_seq),
        )
        return [self._buffer_row_dict(r) for r in cur.fetchall()]

    async def buffer_snapshot(self, session_id: str, max_messages: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Return an immutable pending-message snapshot and its exact upper seq."""
        conn = self._connect()
        sql = """SELECT role, content, seq, created_ts, event_ts, event_timezone
                 FROM pending_messages WHERE session_id=? ORDER BY seq ASC"""
        params: tuple[Any, ...] = (session_id,)
        if max_messages > 0:
            sql += " LIMIT ?"
            params = (session_id, int(max_messages))
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return [], -1
        return ([self._buffer_row_dict(r) for r in rows], int(rows[-1]["seq"]))

    async def buffer_drop(self, session_id: str, up_to_seq: int) -> int:
        """删除该 session 已处理完的缓冲消息(<= up_to_seq),返回删除数。
        压缩成功后调用,把已写库的那批清除,避免重复压缩。
        """
        conn = self._connect()
        cur = conn.execute(
            "DELETE FROM pending_messages WHERE session_id=? AND seq<=?",
            (session_id, up_to_seq),
        )
        conn.commit()
        return cur.rowcount

    async def buffer_max_seq(self, session_id: str) -> int:
        """取该 session 当前最大 seq;重启用"""
        conn = self._connect()
        cur = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM pending_messages WHERE session_id=?",
            (session_id,),
        )
        row = cur.fetchone()
        return int(row["m"]) if row else -1

    # ---------- 检索 ----------
    async def search_topk(
        self,
        query_vec: list[float],
        top_k: int = 2,
        min_similarity: float = 0.52,
        *,
        w_relevance: float = 0.72,
        bm25_weight: float = 0.3,
        bm25_query: str = "",
        rerank_fn=None,
        w_importance: float = 0.23,
        w_recency: float = 0.03,
        pin_boost: float = 0.18,
        time_boost: float = 0.0,
        current_month_day: str = "",
        candidate_memo_names: set[str] | None = None,
        feedback_query_vec: list[float] | None = None,
        feedback_query_text: str = "",
    ) -> list[dict[str, Any]]:
        """按 memo 去重的 top_k 个 chunk(每篇日记只返回最相关的一块)。

        排序不是纯 cosine,而是综合分,专为长期 RP 日记设计:
            final = w_relevance * relevance          # 语义相关度(cosine, 硬门槛)
                  + w_importance * importance_norm   # 剧情重要性(importance 1-5 -> 0-1)
                  + w_recency  * recency_norm        # 新近度(仅极小 tie-break, 老记忆不降权)
                  + pin_boost  (若 manual=1)         # 手动钉的记忆额外加成

        关键设计:relevance 是硬门槛(< min_similarity 直接淘汰),保证不相关的绝不因
        importance 高而被召回;但通过门槛后,重要剧情(初遇/承诺/里程碑)排在日常琐事前。
        时间不做衰减 —— RP 场景里初遇/定情这类珍贵记忆往往最老,衰减会把它们埋掉。
        recency 只在综合分接近时让新记忆略微优先,权重极小。

        返回 [{"memo_name","chunk_text","ts_text","tags","importance","score","relevance"}]
        """
        # Gather with lower threshold; real filter happens after BM25 blend
        cands = self._gather_candidates(
            query_vec, top_k, min(min_similarity, 0.4), candidate_memo_names
        )
        if not cands and not (bm25_weight > 0 and bm25_query):
            return []

        # BM25 hybrid: blend vector relevance with keyword relevance
        if bm25_weight > 0 and bm25_query:
            self._ensure_bm25()
            if self._bm25:
                # Pre-compute max BM25 per memo_name (best chunk score)
                memo_bm25_max = {}
                for i, mn in enumerate(self._bm25_ids):
                    if candidate_memo_names is not None and mn not in candidate_memo_names:
                        continue
                    bs = self._bm25.score(bm25_query, i)
                    if mn not in memo_bm25_max or bs > memo_bm25_max[mn]:
                        memo_bm25_max[mn] = bs
                # Find global max for normalization
                all_bm25 = list(memo_bm25_max.values())
                bs_max = max(all_bm25) if all_bm25 else 1.0
                # BM25 is an independent retrieval signal, not merely a bonus for
                # candidates already admitted by cosine. Rescue exact names and
                # phrases that embeddings often miss, then let the common ranker
                # and optional reranker decide whether they survive.
                existing_memos = {str(c.get("memo_name") or "") for c in cands}
                query_tokens = set(self._bm25._tokenize(
                    bm25_query, getattr(self._bm25, "_use_jieba", False)
                ))
                lexical_ranked = sorted(memo_bm25_max.items(), key=lambda item: item[1], reverse=True)
                conn = self._connect()
                for mn, raw_score in lexical_ranked[:max(top_k * 2, 30)]:
                    if raw_score <= 0 or mn in existing_memos:
                        continue
                    rows = conn.execute(
                        """SELECT id AS chunk_id,memo_name,chunk_text,ts_text,tags,importance,manual,
                                  memory_type,long_effect,trigger_hint,created_ts,occurred_at,event_ts,time_basis,
                                  source_created_ts,source_updated_ts,passage_index,char_start,char_end,content_hash,
                                  scene_anchor,retrieval_key,state_change,entities
                           FROM chunks WHERE memo_name=? ORDER BY passage_index,rowid""",
                        (mn,),
                    ).fetchall()
                    if not rows:
                        continue
                    def lexical_overlap(row):
                        tokens = set(self._bm25._tokenize(
                            str(row["chunk_text"] or ""), getattr(self._bm25, "_use_jieba", False)
                        ))
                        return len(query_tokens & tokens)
                    row = max(rows, key=lexical_overlap)
                    cands.append({
                        "chunk_id": int(row["chunk_id"] or 0), "memo_name": row["memo_name"],
                        "chunk_text": row["chunk_text"] or "", "ts_text": row["ts_text"] or "",
                        "tags": row["tags"] or "", "importance": row["importance"],
                        "manual": row["manual"], "memory_type": row["memory_type"],
                        "long_effect": row["long_effect"], "trigger_hint": row["trigger_hint"],
                        "created_ts": row["created_ts"], "occurred_at": row["occurred_at"],
                        "event_ts": row["event_ts"], "time_basis": row["time_basis"],
                        "source_created_ts": row["source_created_ts"],
                        "source_updated_ts": row["source_updated_ts"],
                        "passage_index": int(row["passage_index"] or 0),
                        "char_start": int(row["char_start"] or 0), "char_end": int(row["char_end"] or 0),
                        "content_hash": row["content_hash"] or "", "scene_anchor": row["scene_anchor"] or "",
                        "retrieval_key": row["retrieval_key"] or "", "state_change": row["state_change"] or "",
                        "entities": row["entities"] or "", "relevance": 0.0,
                        "_lexical_rescue": True,
                    })
                    existing_memos.add(mn)
                blend_log = []
                for c in cands:
                    mn = c["memo_name"]
                    bm25_raw = memo_bm25_max.get(mn, 0.0)
                    bm25_norm = bm25_raw / bs_max if bs_max > 0 else 0.0
                    old_rel = c["relevance"]
                    c["semantic_relevance"] = old_rel
                    blended = (1.0 - bm25_weight) * old_rel + bm25_weight * bm25_norm
                    if c.get("_lexical_rescue") and bm25_norm >= 0.34:
                        blended = max(blended, 0.42 + bm25_norm * 0.38)
                    c["relevance"] = blended
                    c["bm25_relevance"] = bm25_norm
                    if bm25_raw > 0:
                        blend_log.append({"memo": mn[:20], "vec": round(old_rel, 3), "bm25": round(bm25_norm, 3), "blended": round(c["relevance"], 3)})
                if blend_log:
                    logger.info("[memos-mem] BM25 blend: %s", blend_log[:3])
        if not cands:
            return []

        # Query-scoped WebUI feedback is a bounded prior. Legacy global boosts
        # and the retired anchor table remain stored for rollback, but are not
        # allowed to bias current retrieval. 4.5: fetched in one batched query
        # for the whole candidate pool instead of one SELECT per candidate.
        feedback_map = self.feedback_effects_for_query(
            [c["memo_name"] for c in cands],
            feedback_query_vec or query_vec,
            feedback_query_text or bm25_query,
        )
        for c in cands:
            feedback = feedback_map.get(c["memo_name"]) or {}
            c["future_boost"] = float(feedback.get("boost") or 0.0)
            c["feedback_matches"] = int(feedback.get("matches") or 0)
            c["feedback_actions"] = feedback.get("actions") or []

        # Recency follows the remembered event, never the local reindex time.
        ts_list = [c.get("event_ts") or c.get("source_created_ts") or c.get("created_ts") for c in cands]
        ts_list = [float(ts) for ts in ts_list if ts]
        ts_min = min(ts_list) if ts_list else 0.0
        ts_max = max(ts_list) if ts_list else 1.0
        ts_span = (ts_max - ts_min) or 1.0

        for c in cands:
            rel = c["relevance"]
            imp = max(1, min(5, int(c.get("importance") or 3)))
            imp_norm = (imp - 1) / 4.0  # 1->0, 5->1
            memory_ts = c.get("event_ts") or c.get("source_created_ts") or c.get("created_ts") or ts_min
            rec_norm = ((float(memory_ts) - ts_min) / ts_span) if ts_span else 0.0
            relevance_part = w_relevance * rel
            importance_part = w_importance * imp_norm
            recency_part = w_recency * rec_norm
            manual_part = 0.0
            tier_part = 0.0
            feedback_part = float(c.get("future_boost", 0.0) or 0.0)
            time_part = 0.0
            final = relevance_part + importance_part + recency_part
            if int(c.get("manual") or 0) == 1:
                manual_part = pin_boost
                final += manual_part
            # v1.8: importance=5 boost (key plot memories always surface)
            if imp >= 5:
                tier_part = 0.15
                final += tier_part
            # v1.8: user feedback boost
            final += feedback_part
            # v1.5.1: 时间感知加权 — 当前对话月日与日记月日匹配时加分
            if time_boost > 0 and current_month_day:
                md = c.get("ts_text", "")
                if md and current_month_day in md:
                    time_part = time_boost
                    final += time_part
            c["score"] = final
            c["score_parts"] = {
                "relevance": round(relevance_part, 6),
                "importance": round(importance_part, 6),
                "recency": round(recency_part, 6),
                "manual": round(manual_part, 6),
                "tier5": round(tier_part, 6),
                "feedback": round(feedback_part, 6),
                "time": round(time_part, 6),
            }
            c["diagnostics"] = {
                "importance_norm": round(imp_norm, 4),
                "recency_norm": round(rec_norm, 4),
                "manual": int(c.get("manual") or 0),
                "feedback_boost": round(feedback_part, 4),
                "feedback_matches": int(c.get("feedback_matches") or 0),
                "feedback_actions": c.get("feedback_actions") or [],
                "time_matched": bool(time_part),
            }

        # Re-rank BEFORE min_similarity filter (so rerank can rescue borderline candidates)
        if rerank_fn is not None:
            try:
                cands.sort(key=lambda x: x["score"], reverse=True)
                rerank_pool = cands[:max(top_k * 10, 20)]
                reranked = await rerank_fn(bm25_query, rerank_pool)
                if reranked:
                    rescue_gate = max(0.55, float(min_similarity or 0.0))
                    for c in reranked:
                        rerank_score = float(c.get("_rerank_score") or 0.0)
                        c["_rerank_boost"] = rerank_score >= rescue_gate
                    pool_set = set(id(c) for c in rerank_pool)
                    rest = [c for c in cands if id(c) not in pool_set]
                    cands = reranked + rest
                    logger.info("[memos-mem] rerank: %d reordered + %d rest, top3: %s",
                        len(reranked), len(rest),
                        [{"memo": c["memo_name"][:20], "score": round(c["score"], 3)} for c in reranked[:3]])
            except Exception as e:
                logger.warning("[memos-mem] rerank failed, using coarse ranking: %s", e)

        # Post-BM25 + post-rerank filter
        if min_similarity > 0:
            before_filter = len(cands)
            filtered = [c for c in cands if c["relevance"] >= min_similarity or c.get("_rerank_boost")]
            if len(filtered) != before_filter:
                logger.info("[memos-mem] filter: %d -> %d (threshold=%.2f)", before_filter, len(filtered), min_similarity)
            cands = filtered
            if not cands:
                return []

        # 按综合分降序,memo 去重取 top_k
        cands.sort(key=lambda x: x["score"], reverse=True)
        out: list[dict] = []
        seen: set[str] = set()
        for c in cands:
            mn = c["memo_name"]
            if mn in seen:
                continue
            seen.add(mn)
            out.append({
                "memo_name": mn, "chunk_text": c["chunk_text"],
                "ts_text": c["ts_text"], "tags": c["tags"],
                "importance": c["importance"], "score": c["score"],
                "relevance": c["relevance"],
                "memory_type": c.get("memory_type") or "plot_fact",
                "long_effect": c.get("long_effect") or "",
                "trigger_hint": c.get("trigger_hint") or "",
                "created_ts": c.get("created_ts", 0),
                "occurred_at": c.get("occurred_at") or "",
                "event_ts": float(c.get("event_ts") or 0.0),
                "time_basis": c.get("time_basis") or "unknown",
                "source_created_ts": float(c.get("source_created_ts") or 0.0),
                "source_updated_ts": float(c.get("source_updated_ts") or 0.0),
                "manual": int(c.get("manual") or 0),
                "feedback_boost": float(c.get("future_boost") or 0.0),
                "_rerank_score": float(c.get("_rerank_score") or 0.0),
                "chunk_id": int(c.get("chunk_id") or 0),
                "passage_index": int(c.get("passage_index") or 0),
                "char_start": int(c.get("char_start") or 0),
                "char_end": int(c.get("char_end") or 0),
                "content_hash": c.get("content_hash") or "",
                "scene_anchor": c.get("scene_anchor") or "",
                "retrieval_key": c.get("retrieval_key") or "",
                "state_change": c.get("state_change") or "",
                "entities": c.get("entities") or "",
                "score_parts": c.get("score_parts") or {},
                "diagnostics": c.get("diagnostics") or {},
                "semantic_relevance": float(c.get("semantic_relevance") or c.get("relevance") or 0.0),
                "bm25_relevance": float(c.get("bm25_relevance") or 0.0),
                "lexical_rescue": bool(c.get("_lexical_rescue")),
            })
            if len(out) >= top_k:
                break
        return out

    def search_temporal_keys(self, keys: list[str], limit: int = 20) -> list[dict[str, Any]]:
        """Retrieve concrete date matches without another embedding query."""
        clean = list(dict.fromkeys(" ".join(str(key or "").split()).strip() for key in keys))
        clean = [key for key in clean if key][:12]
        if not clean:
            return []
        conn = self._connect()
        clauses = []
        params: list[Any] = []
        for key in clean:
            clauses.append("(occurred_at LIKE ? OR ts_text LIKE ?)")
            params.extend((f"%{key}%", f"%{key}%"))
        rows = conn.execute(
            """SELECT id AS chunk_id,memo_name,chunk_text,ts_text,tags,importance,manual,
                      memory_type,long_effect,trigger_hint,created_ts,occurred_at,event_ts,time_basis,
                      source_created_ts,source_updated_ts,passage_index,char_start,char_end,content_hash,
                      scene_anchor,retrieval_key,state_change,entities
               FROM chunks WHERE """ + " OR ".join(clauses),
            params,
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            haystack = f"{row['occurred_at'] or ''} {row['ts_text'] or ''}"
            matches = [key for key in clean if key in haystack]
            if not matches:
                continue
            specificity = max(min(1.0, len(key) / 10.0) for key in matches)
            relevance = min(0.96, 0.64 + specificity * 0.24 + min(0.08, len(matches) * 0.02))
            item = {
                "memo_name": row["memo_name"], "chunk_text": row["chunk_text"] or "",
                "ts_text": row["ts_text"] or "", "tags": row["tags"] or "",
                "importance": int(row["importance"] or 3), "manual": int(row["manual"] or 0),
                "memory_type": row["memory_type"] or "plot_fact", "long_effect": row["long_effect"] or "",
                "trigger_hint": row["trigger_hint"] or "", "created_ts": float(row["created_ts"] or 0),
                "occurred_at": row["occurred_at"] or "", "event_ts": float(row["event_ts"] or 0),
                "time_basis": row["time_basis"] or "unknown",
                "source_created_ts": float(row["source_created_ts"] or 0),
                "source_updated_ts": float(row["source_updated_ts"] or 0),
                "chunk_id": int(row["chunk_id"] or 0), "passage_index": int(row["passage_index"] or 0),
                "char_start": int(row["char_start"] or 0), "char_end": int(row["char_end"] or 0),
                "content_hash": row["content_hash"] or "", "scene_anchor": row["scene_anchor"] or "",
                "retrieval_key": row["retrieval_key"] or "", "state_change": row["state_change"] or "",
                "entities": row["entities"] or "", "relevance": relevance, "score": relevance,
                "_temporal_rescue": True, "_temporal_keys": matches,
                "_matched_passages": [{
                    "key": int(row["chunk_id"] or 0), "chunk_id": int(row["chunk_id"] or 0),
                    "passage_index": int(row["passage_index"] or 0),
                    "char_start": int(row["char_start"] or 0), "char_end": int(row["char_end"] or 0),
                    "text": row["chunk_text"] or "", "route": "temporal", "relevance": relevance,
                }],
            }
            existing = grouped.get(str(row["memo_name"]))
            if existing is None or relevance > float(existing.get("relevance") or 0):
                grouped[str(row["memo_name"])] = item
        out = sorted(
            grouped.values(),
            key=lambda item: (float(item.get("relevance") or 0), float(item.get("event_ts") or 0)),
            reverse=True,
        )
        return out[:max(1, min(100, int(limit)))]

    def close(self):
        """Close the database connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _init_extra_tables(self):
        """v1.8+: Create tables for graph, keywords, summaries, feedback, anchors."""
        conn = self._connect()
        conn.execute("""CREATE TABLE IF NOT EXISTS memo_graph (
            memo_a TEXT NOT NULL,
            memo_b TEXT NOT NULL,
            similarity REAL,
            shared_tags TEXT,
            created_ts REAL,
            PRIMARY KEY (memo_a, memo_b)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS memo_similarity_edges (
            memo_a TEXT NOT NULL,
            memo_b TEXT NOT NULL,
            similarity REAL,
            source TEXT DEFAULT 'embedding',
            shared_tags TEXT DEFAULT '',
            updated_ts REAL,
            PRIMARY KEY (memo_a, memo_b)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS memo_similarity_clusters (
            memo_name TEXT PRIMARY KEY,
            cluster_id TEXT NOT NULL,
            representative TEXT DEFAULT '',
            cluster_size INTEGER DEFAULT 1,
            max_similarity REAL DEFAULT 1,
            reason TEXT DEFAULT '',
            updated_ts REAL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_similarity_cluster_id ON memo_similarity_clusters(cluster_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_similarity_edges_a ON memo_similarity_edges(memo_a)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_similarity_edges_b ON memo_similarity_edges(memo_b)")
        conn.execute("""CREATE TABLE IF NOT EXISTS memo_keywords (
            memo_name TEXT NOT NULL,
            keyword TEXT NOT NULL,
            weight REAL DEFAULT 0,
            PRIMARY KEY (memo_name, keyword)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS memo_summaries (
            year_month TEXT PRIMARY KEY,
            summary TEXT,
            embedding BLOB,
            created_ts REAL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS memo_feedback (
            memo_name TEXT PRIMARY KEY,
            boost REAL DEFAULT 0,
            reason TEXT,
            updated_ts REAL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS recall_feedback_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            memo_name TEXT NOT NULL,
            query_text TEXT DEFAULT '',
            query_fingerprint TEXT DEFAULT '',
            query_embedding BLOB,
            action TEXT NOT NULL,
            effect REAL DEFAULT 0,
            reason TEXT DEFAULT '',
            source TEXT DEFAULT 'webui',
            created_ts REAL,
            updated_ts REAL,
            UNIQUE(request_id, memo_name)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_feedback_memo ON recall_feedback_events(memo_name, updated_ts DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_anchors (
            memo_name TEXT NOT NULL,
            anchor_type TEXT NOT NULL,
            strength INTEGER DEFAULT 3,
            note TEXT DEFAULT '',
            created_ts REAL,
            updated_ts REAL,
            PRIMARY KEY (memo_name, anchor_type)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS memo_month_index (
            year_month TEXT PRIMARY KEY,
            memo_count INTEGER DEFAULT 0,
            day_count INTEGER DEFAULT 0,
            first_event_ts REAL DEFAULT 0,
            last_event_ts REAL DEFAULT 0,
            cues_json TEXT DEFAULT '[]',
            tags_json TEXT DEFAULT '[]',
            entities_json TEXT DEFAULT '[]',
            memory_types_json TEXT DEFAULT '{}',
            memo_names_json TEXT DEFAULT '[]',
            updated_ts REAL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS memo_month_routes (
            year_month TEXT NOT NULL,
            route_id INTEGER NOT NULL,
            cue_text TEXT DEFAULT '',
            source_memos_json TEXT DEFAULT '[]',
            embedding BLOB,
            updated_ts REAL,
            PRIMARY KEY (year_month, route_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_month_routes_month ON memo_month_routes(year_month)")
        conn.commit()

    def _ensure_bm25(self):
        """Build or load BM25 index (memo-level, persisted)."""
        if self._bm25 is not None:
            return
        import json as _json
        conn = self._connect()
        # Check if persisted index is still valid
        chunk_count = conn.execute("SELECT count(*) as c FROM chunks").fetchone()["c"]
        if chunk_count == 0:
            return
        stored = conn.execute("SELECT value FROM meta WHERE key='bm25_index'").fetchone()
        if stored:
            try:
                d = _json.loads(stored["value"])
                max_rid = conn.execute("SELECT COALESCE(MAX(rowid),0) as m FROM chunks").fetchone()["m"]
                if d.get("chunk_count") == chunk_count and d.get("max_rowid") == max_rid:
                    self._bm25 = _BM25.from_dict(d["bm25"])
                    self._bm25_ids = d["ids"]
                    logger.info("[memos-mem] BM25 index loaded from cache (%d docs)", len(self._bm25_ids))
                    return
            except Exception:
                pass
        # Build memo-level BM25: concatenate all chunks per memo
        rows = conn.execute("SELECT memo_name, chunk_text FROM chunks ORDER BY rowid").fetchall()
        memo_texts = {}
        for r in rows:
            mn = r["memo_name"]
            ct = (r["chunk_text"] or "").strip()
            if mn not in memo_texts:
                memo_texts[mn] = []
            memo_texts[mn].append(ct)
        self._bm25_ids = list(memo_texts.keys())
        # Concatenate chunks with space separator for BM25
        docs = [" ".join(memo_texts[mn]) for mn in self._bm25_ids]
        self._bm25 = _BM25()
        self._bm25.build(docs, use_jieba=self._bm25_use_jieba)
        # Persist to meta table
        try:
            max_rid = conn.execute("SELECT COALESCE(MAX(rowid),0) as m FROM chunks").fetchone()["m"]
            payload = _json.dumps({
                "chunk_count": chunk_count,
                "max_rowid": max_rid,
                "ids": self._bm25_ids,
                "bm25": self._bm25.to_dict(),
            }, ensure_ascii=False)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('bm25_index', ?)", (payload,))
            conn.commit()
            logger.info("[memos-mem] BM25 index built & persisted (%d memos)", len(self._bm25_ids))
        except Exception as e:
            logger.warning("[memos-mem] BM25 persist failed: %s", e)

    # ---------- v1.8: Graph operations ----------
    def graph_add_edge(self, memo_a: str, memo_b: str, similarity: float, shared_tags: str = ""):
        """Add an edge between two memos in the graph."""
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO memo_graph (memo_a, memo_b, similarity, shared_tags, created_ts) VALUES (?,?,?,?,?)",
            (memo_a, memo_b, similarity, shared_tags, time.time()))
        conn.commit()

    def graph_get_related(self, memo_name: str, limit: int = 10) -> list[dict]:
        """Get memos related to the given memo via graph edges."""
        conn = self._connect()
        rows = conn.execute(
            """SELECT memo_b AS related, similarity, shared_tags FROM memo_graph WHERE memo_a=?
               UNION ALL
               SELECT memo_a AS related, similarity, shared_tags FROM memo_graph WHERE memo_b=?
               ORDER BY similarity DESC LIMIT ?""",
            (memo_name, memo_name, limit)).fetchall()
        return [{"memo_name": r["related"], "similarity": r["similarity"], "shared_tags": r["shared_tags"]} for r in rows]

    def graph_get_clusters(self, threshold: float = 0.7) -> list[dict]:
        """Get graph organized into clusters for two-level visualization.
        Returns list of clusters, each with memos and internal edges."""
        conn = self._connect()
        # Get all memos with embeddings
        rows = conn.execute("SELECT DISTINCT memo_name, ts_text, importance, tags FROM chunks").fetchall()
        memo_list = []
        for r in rows:
            emb = self.get_memo_embedding(r["memo_name"])
            tags = self.get_memo_tags(r["memo_name"])
            memo_list.append({
                "memo_name": r["memo_name"],
                "ts_text": r["ts_text"] or "",
                "importance": int(r["importance"] or 3),
                "tags": tags,
                "embedding": emb,
            })

        # Simple agglomerative clustering by embedding similarity
        clusters = []
        assigned = set()
        for m in memo_list:
            if m["memo_name"] in assigned or m["embedding"] is None:
                continue
            cluster = {"memos": [m["memo_name"]], "tags": set(m["tags"]), "imp_sum": m["importance"]}
            assigned.add(m["memo_name"])
            for other in memo_list:
                if other["memo_name"] in assigned or other["embedding"] is None:
                    continue
                sim = _cosine(m["embedding"], other["embedding"])
                if sim >= threshold:
                    cluster["memos"].append(other["memo_name"])
                    cluster["tags"] |= other["tags"]
                    cluster["imp_sum"] += other["importance"]
                    assigned.add(other["memo_name"])
            # Get representative info
            cluster["size"] = len(cluster["memos"])
            cluster["avg_imp"] = round(cluster["imp_sum"] / max(cluster["size"], 1), 1)
            cluster["label"] = ", ".join(sorted(cluster["tags"]))[:30] if cluster["tags"] else f"Cluster ({cluster['size']})"
            cluster["tags"] = sorted(cluster["tags"])
            clusters.append(cluster)

        # Sort clusters by size descending
        clusters.sort(key=lambda c: -c["size"])
        return clusters

    def graph_get_all(self) -> list[dict]:
        """Get all graph edges for visualization."""
        conn = self._connect()
        rows = conn.execute("SELECT memo_a, memo_b, similarity, shared_tags FROM memo_graph ORDER BY similarity DESC").fetchall()
        return [{"memo_a": r["memo_a"], "memo_b": r["memo_b"], "similarity": r["similarity"], "shared_tags": r["shared_tags"]} for r in rows]

    def graph_rebuild(self, memo_embeddings: dict[str, list[float]], memo_tags: dict[str, set[str]], threshold: float = 0.8):
        """Rebuild the entire graph from scratch."""
        conn = self._connect()
        conn.execute("DELETE FROM memo_graph")
        memos = list(memo_embeddings.keys())
        edges = []
        for i in range(len(memos)):
            for j in range(i + 1, len(memos)):
                ma, mb = memos[i], memos[j]
                sim = _cosine(memo_embeddings[ma], memo_embeddings[mb])
                shared = memo_tags.get(ma, set()) & memo_tags.get(mb, set())
                if sim >= threshold or len(shared) >= 2:
                    edges.append((ma, mb, round(sim, 4), ",".join(sorted(shared))))
        for ma, mb, sim, tags in edges:
            conn.execute(
                "INSERT OR REPLACE INTO memo_graph (memo_a, memo_b, similarity, shared_tags, created_ts) VALUES (?,?,?,?,?)",
                (ma, mb, sim, tags, time.time()))
        conn.commit()

    # ---------- v2.2.6test2: recall similarity clusters ----------
    def similarity_clusters_store(self, assignments: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        """Replace the precomputed similarity-cluster index."""
        conn = self._connect()
        now = time.time()
        conn.execute("DELETE FROM memo_similarity_clusters")
        conn.execute("DELETE FROM memo_similarity_edges")
        for e in edges:
            a = str(e.get("memo_a") or "")
            b = str(e.get("memo_b") or "")
            if not a or not b or a == b:
                continue
            if a > b:
                a, b = b, a
            conn.execute(
                """INSERT OR REPLACE INTO memo_similarity_edges
                   (memo_a, memo_b, similarity, source, shared_tags, updated_ts)
                   VALUES (?,?,?,?,?,?)""",
                (
                    a,
                    b,
                    float(e.get("similarity") or 0.0),
                    str(e.get("source") or "embedding"),
                    str(e.get("shared_tags") or ""),
                    now,
                ),
            )
        for a in assignments:
            memo_name = str(a.get("memo_name") or "")
            if not memo_name:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO memo_similarity_clusters
                   (memo_name, cluster_id, representative, cluster_size, max_similarity, reason, updated_ts)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    memo_name,
                    str(a.get("cluster_id") or f"solo:{memo_name}"),
                    str(a.get("representative") or memo_name),
                    int(a.get("cluster_size") or 1),
                    float(a.get("max_similarity") or 1.0),
                    str(a.get("reason") or ""),
                    now,
                ),
            )
        conn.commit()

    def similarity_edges_upsert_for_memo(self, memo_name: str, edges: list[dict[str, Any]]) -> None:
        """Refresh similarity edges that involve one memo. Components are recomputed separately."""
        conn = self._connect()
        now = time.time()
        conn.execute("DELETE FROM memo_similarity_edges WHERE memo_a=? OR memo_b=?", (memo_name, memo_name))
        for e in edges:
            a = str(e.get("memo_a") or "")
            b = str(e.get("memo_b") or "")
            if not a or not b or a == b:
                continue
            if a > b:
                a, b = b, a
            conn.execute(
                """INSERT OR REPLACE INTO memo_similarity_edges
                   (memo_a, memo_b, similarity, source, shared_tags, updated_ts)
                   VALUES (?,?,?,?,?,?)""",
                (
                    a,
                    b,
                    float(e.get("similarity") or 0.0),
                    str(e.get("source") or "embedding"),
                    str(e.get("shared_tags") or ""),
                    now,
                ),
            )
        conn.commit()

    def similarity_edges_all(self, limit: int = 1000, min_similarity: float = 0.0) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """SELECT memo_a, memo_b, similarity, source, shared_tags
               FROM memo_similarity_edges
               WHERE similarity>=?
               ORDER BY similarity DESC LIMIT ?""",
            (float(min_similarity or 0.0), int(limit or 1000)),
        ).fetchall()
        return [
            {
                "memo_a": r["memo_a"], "memo_b": r["memo_b"],
                "similarity": float(r["similarity"] or 0.0),
                "source": r["source"] or "embedding",
                "shared_tags": r["shared_tags"] or "",
            }
            for r in rows
        ]

    def similarity_edges_for_memo(self, memo_name: str, limit: int = 20, min_similarity: float = 0.0) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """SELECT memo_b AS related, similarity, source, shared_tags
                 FROM memo_similarity_edges WHERE memo_a=? AND similarity>=?
               UNION ALL
               SELECT memo_a AS related, similarity, source, shared_tags
                 FROM memo_similarity_edges WHERE memo_b=? AND similarity>=?
               ORDER BY similarity DESC LIMIT ?""",
            (memo_name, float(min_similarity or 0.0), memo_name, float(min_similarity or 0.0), int(limit or 20)),
        ).fetchall()
        return [
            {
                "memo_name": r["related"],
                "similarity": float(r["similarity"] or 0.0),
                "source": r["source"] or "embedding",
                "shared_tags": r["shared_tags"] or "",
            }
            for r in rows
        ]

    def similarity_cluster_map(self, memo_names: list[str] | set[str] | None = None) -> dict[str, str]:
        conn = self._connect()
        if memo_names:
            names = [n for n in memo_names if n]
            if not names:
                return {}
            placeholders = ",".join("?" for _ in names)
            rows = conn.execute(
                f"SELECT memo_name, cluster_id FROM memo_similarity_clusters WHERE memo_name IN ({placeholders})",
                names,
            ).fetchall()
        else:
            rows = conn.execute("SELECT memo_name, cluster_id FROM memo_similarity_clusters").fetchall()
        return {r["memo_name"]: r["cluster_id"] for r in rows}

    def similarity_clusters_store_from_edges(self, min_similarity: float = 0.0) -> dict[str, int]:
        """Recompute connected components from the current similarity-edge table."""
        conn = self._connect()
        memos = sorted(self.all_memo_names())
        parent = {m: m for m in memos}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            if a not in parent or b not in parent:
                return
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        rows = conn.execute(
            """SELECT memo_a, memo_b, similarity, source, shared_tags
               FROM memo_similarity_edges WHERE similarity>=?""",
            (float(min_similarity or 0.0),),
        ).fetchall()
        for r in rows:
            union(r["memo_a"], r["memo_b"])
        groups: dict[str, list[str]] = {}
        for m in memos:
            groups.setdefault(find(m), []).append(m)
        meta_rows = conn.execute(
            """SELECT memo_name, MAX(importance) AS imp, MAX(created_ts) AS created_ts
               FROM chunks GROUP BY memo_name"""
        ).fetchall()
        meta = {r["memo_name"]: (int(r["imp"] or 3), float(r["created_ts"] or 0.0)) for r in meta_rows}
        edge_score: dict[str, float] = {}
        edge_reason: dict[str, str] = {}
        for r in rows:
            sim = float(r["similarity"] or 0.0)
            for m in (r["memo_a"], r["memo_b"]):
                if sim > edge_score.get(m, 0.0):
                    edge_score[m] = sim
                    edge_reason[m] = r["source"] or "embedding"
        assignments = []
        sorted_groups = sorted(groups.values(), key=lambda xs: (-len(xs), xs[0]))
        for idx, names in enumerate(sorted_groups, start=1):
            rep = sorted(names, key=lambda n: (meta.get(n, (3, 0.0))[0], meta.get(n, (3, 0.0))[1]), reverse=True)[0]
            cid = f"sim-{idx:04d}" if len(names) > 1 else f"solo:{names[0]}"
            for n in names:
                assignments.append({
                    "memo_name": n,
                    "cluster_id": cid,
                    "representative": rep,
                    "cluster_size": len(names),
                    "max_similarity": edge_score.get(n, 1.0 if len(names) == 1 else 0.0),
                    "reason": edge_reason.get(n, "singleton" if len(names) == 1 else "component"),
                })
        self.similarity_clusters_store(assignments, [dict(r) for r in rows])
        return {"memos": len(memos), "clusters": len(sorted_groups), "edges": len(rows)}

    def similarity_cluster_overview(self, limit: int = 80) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """SELECT cluster_id,
                      COUNT(*) AS size,
                      MAX(representative) AS representative,
                      AVG(max_similarity) AS avg_similarity,
                      MAX(updated_ts) AS updated_ts
               FROM memo_similarity_clusters
               GROUP BY cluster_id
               ORDER BY size DESC, avg_similarity DESC
               LIMIT ?""",
            (int(limit or 80),),
        ).fetchall()
        out = []
        for r in rows:
            cid = r["cluster_id"]
            memos = conn.execute(
                """SELECT c.memo_name, c.cluster_size, c.max_similarity, c.reason,
                          MAX(ch.ts_text) AS ts, MAX(ch.importance) AS imp,
                          MAX(ch.tags) AS tags, MAX(ch.memory_type) AS memory_type,
                          MIN(ch.chunk_text) AS preview, MAX(ch.created_ts) AS created_ts
                   FROM memo_similarity_clusters c
                   JOIN chunks ch ON ch.memo_name=c.memo_name
                   WHERE c.cluster_id=?
                   GROUP BY c.memo_name
                   ORDER BY c.max_similarity DESC, MAX(ch.importance) DESC, MAX(ch.created_ts) DESC
                   LIMIT 40""",
                (cid,),
            ).fetchall()
            tag_set = set()
            items = []
            for m in memos:
                for t in (m["tags"] or "").replace(",", " ").split():
                    if t.strip():
                        tag_set.add(t.strip().lstrip("#"))
                items.append({
                    "memo_name": m["memo_name"],
                    "ts": m["ts"] or "",
                    "imp": int(m["imp"] or 3),
                    "tags": m["tags"] or "",
                    "memory_type": m["memory_type"] or "plot_fact",
                    "preview": (m["preview"] or "")[:220],
                    "similarity": float(m["max_similarity"] or 0.0),
                    "reason": m["reason"] or "",
                    "created_ts": float(m["created_ts"] or 0.0),
                })
            out.append({
                "id": cid,
                "size": int(r["size"] or 0),
                "representative": r["representative"] or "",
                "avg_similarity": round(float(r["avg_similarity"] or 0.0), 4),
                "updated_ts": float(r["updated_ts"] or 0.0),
                "label": ", ".join(sorted(tag_set)[:3]) or cid,
                "tags": sorted(tag_set)[:10],
                "memos": items,
            })
        return out

    # ---------- v1.8: Keywords operations ----------
    def keywords_store(self, memo_name: str, keywords: list[tuple[str, float]]):
        """Store keywords for a memo. keywords = [(word, idf_weight), ...]"""
        conn = self._connect()
        conn.execute("DELETE FROM memo_keywords WHERE memo_name=?", (memo_name,))
        for word, weight in keywords:
            conn.execute("INSERT INTO memo_keywords (memo_name, keyword, weight) VALUES (?,?,?)",
                (memo_name, word, weight))
        conn.commit()

    def keywords_find_by_word(self, word: str) -> list[dict]:
        """Find memos that have this keyword."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT memo_name, weight FROM memo_keywords WHERE keyword=? ORDER BY weight DESC",
            (word,)).fetchall()
        return [{"memo_name": r["memo_name"], "weight": r["weight"]} for r in rows]

    def keywords_find_in_text(self, text: str, limit: int = 20, min_weight: float = 0.2, min_hits: int = 1) -> list[dict]:
        """Find stored keywords that appear in free-form text, grouped by memo."""
        text = (text or "").strip()
        if not text:
            return []
        conn = self._connect()
        rows = conn.execute(
            "SELECT memo_name, keyword, weight FROM memo_keywords ORDER BY weight DESC"
        ).fetchall()
        grouped: dict[str, dict] = {}
        for r in rows:
            keyword = str(r["keyword"] or "").strip()
            memo_name = str(r["memo_name"] or "")
            weight = float(r["weight"] or 0.0)
            if not keyword or weight < min_weight:
                continue
            if keyword in text:
                item = grouped.setdefault(memo_name, {"memo_name": memo_name, "keywords": [], "weight": 0.0})
                item["keywords"].append(keyword)
                item["weight"] += weight
        out = []
        for item in grouped.values():
            if len(item["keywords"]) >= min_hits:
                out.append({
                    "memo_name": item["memo_name"],
                    "keyword": ",".join(item["keywords"][:3]),
                    "weight": round(item["weight"], 3),
                    "hits": len(item["keywords"]),
                })
        out.sort(key=lambda x: (-x["hits"], -x["weight"]))
        return out[:limit]

    def keywords_get_all(self) -> dict[str, list[str]]:
        """Get all keywords grouped by memo_name."""
        conn = self._connect()
        rows = conn.execute("SELECT memo_name, keyword, weight FROM memo_keywords ORDER BY memo_name, weight DESC").fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["memo_name"], []).append(r["keyword"])
        return result

    # ---------- v1.8: Summary operations ----------
    def summary_store(self, year_month: str, summary: str, embedding: list[float] | None = None):
        """Store or update a monthly summary."""
        conn = self._connect()
        emb_blob = _serialize_f32(embedding) if embedding else None
        conn.execute(
            "INSERT OR REPLACE INTO memo_summaries (year_month, summary, embedding, created_ts) VALUES (?,?,?,?)",
            (year_month, summary, emb_blob, time.time()))
        conn.commit()

    def summary_get(self, year_month: str) -> dict | None:
        """Get a monthly summary."""
        conn = self._connect()
        row = conn.execute("SELECT year_month, summary, created_ts FROM memo_summaries WHERE year_month=?", (year_month,)).fetchone()
        if row:
            return {"year_month": row["year_month"], "summary": row["summary"], "created_ts": row["created_ts"]}
        return None

    def summary_list(self) -> list[dict]:
        """List all summaries."""
        conn = self._connect()
        rows = conn.execute("SELECT year_month, summary, created_ts FROM memo_summaries ORDER BY year_month DESC").fetchall()
        return [{"year_month": r["year_month"], "summary": r["summary"], "created_ts": r["created_ts"]} for r in rows]

    # ---------- v1.8: Feedback operations ----------
    def feedback_set(self, memo_name: str, boost_delta: float, reason: str = ""):
        """Update feedback boost for a memo."""
        conn = self._connect()
        existing = conn.execute("SELECT boost FROM memo_feedback WHERE memo_name=?", (memo_name,)).fetchone()
        if existing:
            new_boost = max(-0.5, min(0.5, existing["boost"] + boost_delta))
            conn.execute("UPDATE memo_feedback SET boost=?, reason=?, updated_ts=? WHERE memo_name=?",
                (new_boost, reason, time.time(), memo_name))
        else:
            conn.execute("INSERT INTO memo_feedback (memo_name, boost, reason, updated_ts) VALUES (?,?,?,?)",
                (memo_name, max(-0.5, min(0.5, boost_delta)), reason, time.time()))
        conn.commit()

    def feedback_get_boost(self, memo_name: str) -> float:
        """Get the feedback boost for a memo."""
        conn = self._connect()
        row = conn.execute("SELECT boost FROM memo_feedback WHERE memo_name=?", (memo_name,)).fetchone()
        return float(row["boost"]) if row else 0.0

    def feedback_get_all(self) -> list[dict]:
        """Get all feedback entries."""
        conn = self._connect()
        rows = conn.execute("SELECT memo_name, boost, reason, updated_ts FROM memo_feedback ORDER BY updated_ts DESC").fetchall()
        return [{"memo_name": r["memo_name"], "boost": r["boost"], "reason": r["reason"], "updated_ts": r["updated_ts"]} for r in rows]

    @staticmethod
    def _feedback_terms(text: str) -> set[str]:
        compact = re.sub(r"\s+", "", str(text or "").lower())
        if not compact:
            return set()
        terms = {compact} if len(compact) <= 12 else set()
        terms.update(compact[i:i + 2] for i in range(max(0, len(compact) - 1)))
        terms.update(
            token for token in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", str(text or "").lower())
            if len(token) >= 2
        )
        return terms

    @staticmethod
    def _query_fingerprint(text: str) -> str:
        import hashlib
        normalized = " ".join(str(text or "").strip().lower().split())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]

    def feedback_record(
        self,
        request_id: str,
        memo_name: str,
        query_text: str,
        action: str,
        effect: float,
        reason: str = "",
        source: str = "webui",
        query_embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Store one WebUI feedback decision in the context of its recall query."""
        request_id = str(request_id or "").strip() or f"web-{time.time_ns():x}"
        memo_name = str(memo_name or "").strip()
        query_text = " ".join(str(query_text or "").strip().split())[:1200]
        action = str(action or "").strip()
        if not memo_name or not query_text or not action:
            raise ValueError("memo_name, query_text and action are required")
        now = time.time()
        conn = self._connect()
        conn.execute(
            """INSERT INTO recall_feedback_events
               (request_id, memo_name, query_text, query_fingerprint, query_embedding,
                action, effect, reason, source, created_ts, updated_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(request_id, memo_name) DO UPDATE SET
                 query_text=excluded.query_text,
                 query_fingerprint=excluded.query_fingerprint,
                 query_embedding=COALESCE(excluded.query_embedding, recall_feedback_events.query_embedding),
                 action=excluded.action,
                 effect=excluded.effect,
                 reason=excluded.reason,
                 source=excluded.source,
                 updated_ts=excluded.updated_ts""",
            (
                request_id, memo_name, query_text, self._query_fingerprint(query_text),
                _serialize_f32(query_embedding) if query_embedding else None,
                action, max(-0.25, min(0.20, float(effect or 0.0))),
                str(reason or "")[:300], str(source or "webui")[:40], now, now,
            ),
        )
        conn.commit()
        row = conn.execute(
            """SELECT id, request_id, memo_name, query_text, action, effect, reason,
                      source, created_ts, updated_ts
               FROM recall_feedback_events WHERE request_id=? AND memo_name=?""",
            (request_id, memo_name),
        ).fetchone()
        return dict(row) if row else {}

    def feedback_event_delete(self, event_id: int) -> int:
        conn = self._connect()
        cur = conn.execute("DELETE FROM recall_feedback_events WHERE id=?", (int(event_id),))
        conn.commit()
        return int(cur.rowcount)

    def feedback_event_list(self, limit: int = 200) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            """SELECT id, request_id, memo_name, query_text, action, effect, reason,
                      source, created_ts, updated_ts
               FROM recall_feedback_events ORDER BY updated_ts DESC LIMIT ?""",
            (max(1, min(1000, int(limit or 200))),),
        ).fetchall()
        return [dict(row) for row in rows]

    def feedback_effect_for_query(
        self,
        memo_name: str,
        query_vec: list[float] | None,
        query_text: str,
    ) -> dict[str, Any]:
        """Return a bounded prior from feedback on semantically similar past queries."""
        return self.feedback_effects_for_query([memo_name], query_vec, query_text).get(
            memo_name, {"boost": 0.0, "matches": 0, "actions": []}
        )

    def feedback_effects_for_query(
        self,
        memo_names: list[str],
        query_vec: list[float] | None,
        query_text: str,
    ) -> dict[str, dict[str, Any]]:
        """Batched feedback prior: one SQL round-trip for the whole candidate set.

        4.5: replaces the per-candidate query in search_topk. With oversampled
        candidate pools (hundreds of chunks) the old path issued one SELECT per
        candidate; this keeps the identical scoring math but fetches all rows
        with a single IN(...) query and groups them per memo (limit 80 each,
        newest first, matching the old per-memo LIMIT 80).
        """
        names = [str(n) for n in dict.fromkeys(memo_names) if n]
        result: dict[str, dict[str, Any]] = {
            name: {"boost": 0.0, "matches": 0, "actions": []} for name in names
        }
        if not names:
            return result
        conn = self._connect()
        rows_by_memo: dict[str, list] = {}
        chunk_size = 400  # stay well under SQLite's bound-parameter limit
        for i in range(0, len(names), chunk_size):
            part = names[i:i + chunk_size]
            placeholders = ",".join("?" * len(part))
            rows = conn.execute(
                f"""SELECT memo_name, query_text, query_embedding, action, effect, updated_ts
                    FROM recall_feedback_events WHERE memo_name IN ({placeholders})
                    ORDER BY updated_ts DESC""",
                part,
            ).fetchall()
            for row in rows:
                bucket = rows_by_memo.setdefault(str(row["memo_name"]), [])
                if len(bucket) < 80:
                    bucket.append(row)
        if not rows_by_memo:
            return result
        current_terms = self._feedback_terms(query_text)
        current_fp = self._query_fingerprint(query_text)
        for name, rows in rows_by_memo.items():
            weighted = 0.0
            matches = 0
            actions: list[str] = []
            for row in rows:
                affinity = 0.0
                blob = row["query_embedding"]
                if query_vec and blob:
                    try:
                        affinity = max(affinity, _cosine(query_vec, _deserialize_f32(blob)))
                    except Exception:
                        pass
                old_terms = self._feedback_terms(row["query_text"] or "")
                if current_terms and old_terms:
                    lexical = len(current_terms & old_terms) / max(1, len(current_terms | old_terms))
                    if current_fp == self._query_fingerprint(row["query_text"] or ""):
                        lexical = 1.0
                    affinity = max(affinity, lexical)
                if affinity < 0.48:
                    continue
                # A feedback decision is local to its query neighborhood. Even many
                # clicks cannot turn one diary into a global always-recall item.
                scale = 0.35 + 0.65 * min(1.0, (affinity - 0.48) / 0.52)
                weighted += float(row["effect"] or 0.0) * scale
                matches += 1
                action = str(row["action"] or "")
                if action and action not in actions:
                    actions.append(action)
            result[name] = {
                "boost": round(max(-0.22, min(0.18, weighted)), 6),
                "matches": matches,
                "actions": actions[:6],
            }
        return result

    # ---------- v3.2.5: lossless month routing index ----------
    @staticmethod
    def _memory_date_parts(item: dict[str, Any]) -> tuple[str, int]:
        for value in (item.get("occurred_at"), item.get("ts_text")):
            match = re.search(r"((?:19|20)\d{2})[-/年](\d{1,2})(?:[-/月](\d{1,2}))?", str(value or ""))
            if match:
                year = int(match.group(1))
                month = int(match.group(2))
                day = int(match.group(3) or 0)
                if 1 <= month <= 12:
                    return f"{year:04d}-{month:02d}", day if 1 <= day <= 31 else 0
        raw_ts = item.get("event_ts") or item.get("source_created_ts") or item.get("created_ts")
        try:
            dt = datetime.fromtimestamp(float(raw_ts))
            return dt.strftime("%Y-%m"), int(dt.day)
        except Exception:
            return "", 0

    @staticmethod
    def _month_cue_values(value: Any, *, phrases: bool = False) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        values: list[str] = []
        if phrases:
            clean = " ".join(text.split()).strip(" ,，。；;#")
            if len(clean) >= 2:
                values.append(clean[:80])
        for token in re.split(r"[,，、;；|/#\s]+", text):
            token = token.strip(" .。:：()（）[]【】\"'“”")
            if 2 <= len(token) <= 30 and token not in values:
                values.append(token)
        return values

    @staticmethod
    def _average_vectors(vectors: list[list[float]]) -> list[float] | None:
        if not vectors:
            return None
        dim = len(vectors[0])
        same = [vec for vec in vectors if len(vec) == dim]
        if not same:
            return None
        return [sum(vec[i] for vec in same) / len(same) for i in range(dim)]

    def _month_route_clusters(self, items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        vector_items = [item for item in items if item.get("embedding")]
        if not vector_items:
            return []
        vector_items.sort(
            key=lambda item: (-int(item.get("importance") or 3), float(item.get("event_ts") or 0), item["memo_name"])
        )
        cluster_count = min(6, max(1, int(round(math.sqrt(len(vector_items))))))
        seeds = [vector_items[0]]
        while len(seeds) < cluster_count:
            remaining = [item for item in vector_items if item not in seeds]
            if not remaining:
                break
            seeds.append(min(
                remaining,
                key=lambda item: max(_cosine(item["embedding"], seed["embedding"]) for seed in seeds),
            ))
        centroids = [list(item["embedding"]) for item in seeds]
        clusters: list[list[dict[str, Any]]] = [[] for _ in centroids]
        for _ in range(4):
            clusters = [[] for _ in centroids]
            for item in vector_items:
                idx = max(range(len(centroids)), key=lambda i: _cosine(item["embedding"], centroids[i]))
                clusters[idx].append(item)
            for idx, members in enumerate(clusters):
                avg = self._average_vectors([member["embedding"] for member in members])
                if avg:
                    centroids[idx] = avg
        return [members for members in clusters if members]

    def month_index_rebuild(self) -> dict[str, Any]:
        """Rebuild a derived, multi-vector month router from existing diary chunks."""
        conn = self._connect()
        rows = conn.execute(
            """SELECT memo_name, chunk_text, ts_text, tags, importance, memory_type,
                      long_effect, trigger_hint, occurred_at, event_ts, source_created_ts,
                      created_ts, scene_anchor, retrieval_key, state_change, entities, embedding
               FROM chunks ORDER BY memo_name, passage_index, id"""
        ).fetchall()
        memos: dict[str, dict[str, Any]] = {}
        for row in rows:
            memo_name = str(row["memo_name"] or "")
            if not memo_name:
                continue
            item = memos.setdefault(memo_name, {
                "memo_name": memo_name,
                "chunks": [], "vectors": [], "tags": set(), "entities": set(),
                "importance": 1, "memory_type": "plot_fact", "long_effect": "",
                "trigger_hint": "", "scene_anchor": "", "retrieval_key": "",
                "state_change": "", "ts_text": "", "occurred_at": "",
                "event_ts": 0.0, "source_created_ts": 0.0, "created_ts": 0.0,
            })
            text = str(row["chunk_text"] or "").strip()
            if text:
                item["chunks"].append(text)
            if row["embedding"]:
                try:
                    item["vectors"].append(_deserialize_f32(row["embedding"]))
                except Exception:
                    pass
            item["tags"].update(self._month_cue_values(row["tags"] or ""))
            item["entities"].update(self._month_cue_values(row["entities"] or ""))
            item["importance"] = max(item["importance"], int(row["importance"] or 3))
            for key in (
                "memory_type", "long_effect", "trigger_hint", "scene_anchor", "retrieval_key",
                "state_change", "ts_text", "occurred_at",
            ):
                if row[key] and not item.get(key):
                    item[key] = row[key]
            for key in ("event_ts", "source_created_ts", "created_ts"):
                item[key] = max(float(item.get(key) or 0), float(row[key] or 0))
        months: dict[str, list[dict[str, Any]]] = {}
        for item in memos.values():
            item["embedding"] = self._average_vectors(item.pop("vectors"))
            year_month, day = self._memory_date_parts(item)
            if not year_month:
                continue
            item["year_month"] = year_month
            item["day"] = day
            months.setdefault(year_month, []).append(item)

        now = time.time()
        conn.execute("DELETE FROM memo_month_index")
        conn.execute("DELETE FROM memo_month_routes")
        route_count = 0
        for year_month, items in sorted(months.items()):
            tag_counts: dict[str, int] = {}
            entity_counts: dict[str, int] = {}
            type_counts: dict[str, int] = {}
            cue_counts: dict[str, int] = {}
            for item in items:
                for tag in item["tags"]:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    cue_counts[tag] = cue_counts.get(tag, 0) + 2
                for entity in item["entities"]:
                    entity_counts[entity] = entity_counts.get(entity, 0) + 1
                    cue_counts[entity] = cue_counts.get(entity, 0) + 2
                memory_type = str(item.get("memory_type") or "plot_fact")
                type_counts[memory_type] = type_counts.get(memory_type, 0) + 1
                for key in ("retrieval_key", "scene_anchor", "state_change", "trigger_hint", "long_effect"):
                    for cue in self._month_cue_values(item.get(key), phrases=True):
                        cue_counts[cue] = cue_counts.get(cue, 0) + 1
            top = lambda data, limit: [
                {"value": value, "count": count}
                for value, count in sorted(data.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
            ]
            event_values = [
                float(item.get("event_ts") or item.get("source_created_ts") or item.get("created_ts") or 0)
                for item in items
            ]
            event_values = [value for value in event_values if value > 0]
            conn.execute(
                """INSERT INTO memo_month_index
                   (year_month, memo_count, day_count, first_event_ts, last_event_ts,
                    cues_json, tags_json, entities_json, memory_types_json,
                    memo_names_json, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    year_month, len(items), len({item["day"] for item in items if item["day"]}),
                    min(event_values) if event_values else 0.0,
                    max(event_values) if event_values else 0.0,
                    json.dumps(top(cue_counts, 36), ensure_ascii=False),
                    json.dumps(top(tag_counts, 20), ensure_ascii=False),
                    json.dumps(top(entity_counts, 20), ensure_ascii=False),
                    json.dumps(type_counts, ensure_ascii=False, sort_keys=True),
                    json.dumps([item["memo_name"] for item in items], ensure_ascii=False), now,
                ),
            )
            for route_id, members in enumerate(self._month_route_clusters(items)):
                centroid = self._average_vectors([member["embedding"] for member in members if member.get("embedding")])
                if not centroid:
                    continue
                cue_parts: list[str] = [year_month]
                for member in members:
                    cue_parts.extend(sorted(member["tags"]))
                    cue_parts.extend(sorted(member["entities"]))
                    cue_parts.extend(str(member.get(key) or "") for key in (
                        "retrieval_key", "scene_anchor", "state_change", "trigger_hint", "long_effect",
                    ))
                    cue_parts.append(" ".join(member.get("chunks", [])[:2])[:240])
                cue_text = " ".join(part.strip() for part in cue_parts if str(part or "").strip())[:5000]
                conn.execute(
                    """INSERT INTO memo_month_routes
                       (year_month, route_id, cue_text, source_memos_json, embedding, updated_ts)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        year_month, route_id, cue_text,
                        json.dumps([member["memo_name"] for member in members], ensure_ascii=False),
                        _serialize_f32(centroid), now,
                    ),
                )
                route_count += 1
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('month_index_dirty','0')")
        conn.commit()
        return {"months": len(months), "memos": len(memos), "routes": route_count, "updated_ts": now}

    def month_index_ensure(self) -> dict[str, Any]:
        conn = self._connect()
        marker = conn.execute("SELECT value FROM meta WHERE key='month_index_dirty'").fetchone()
        count = int(conn.execute("SELECT COUNT(*) AS n FROM memo_month_index").fetchone()["n"] or 0)
        memo_count = self.distinct_memo_count()
        if (marker is None or str(marker["value"]) != "0" or (memo_count > 0 and count == 0)):
            return self.month_index_rebuild()
        return {"months": count, "memos": memo_count, "rebuilt": False}

    @staticmethod
    def _json_value(raw: Any, default: Any) -> Any:
        try:
            return json.loads(raw) if raw else default
        except Exception:
            return default

    def month_index_overview(self) -> list[dict[str, Any]]:
        self.month_index_ensure()
        rows = self._connect().execute(
            """SELECT year_month, memo_count, day_count, first_event_ts, last_event_ts,
                      cues_json, tags_json, entities_json, memory_types_json, updated_ts
               FROM memo_month_index ORDER BY year_month DESC"""
        ).fetchall()
        return [{
            "year_month": row["year_month"],
            "memo_count": int(row["memo_count"] or 0),
            "day_count": int(row["day_count"] or 0),
            "first_event_ts": float(row["first_event_ts"] or 0),
            "last_event_ts": float(row["last_event_ts"] or 0),
            "cues": self._json_value(row["cues_json"], []),
            "tags": self._json_value(row["tags_json"], []),
            "entities": self._json_value(row["entities_json"], []),
            "memory_types": self._json_value(row["memory_types_json"], {}),
            "updated_ts": float(row["updated_ts"] or 0),
        } for row in rows]

    def month_memo_names(self, year_months: list[str]) -> set[str]:
        self.month_index_ensure()
        months = [str(value) for value in year_months if re.fullmatch(r"(?:19|20)\d{2}-\d{2}", str(value or ""))]
        if not months:
            return set()
        marks = ",".join("?" for _ in months)
        rows = self._connect().execute(
            f"SELECT memo_names_json FROM memo_month_index WHERE year_month IN ({marks})", months
        ).fetchall()
        names: set[str] = set()
        for row in rows:
            names.update(str(value) for value in self._json_value(row["memo_names_json"], []) if value)
        return names

    def month_route_search(
        self,
        query_vec: list[float],
        query_text: str,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        self.month_index_ensure()
        query_terms = self._feedback_terms(query_text)
        explicit_months: set[str] = set()
        for match in re.finditer(r"((?:19|20)\d{2})[-/年](\d{1,2})月?", str(query_text or "")):
            month = int(match.group(2))
            if 1 <= month <= 12:
                explicit_months.add(f"{int(match.group(1)):04d}-{month:02d}")
        rows = self._connect().execute(
            """SELECT r.year_month, r.route_id, r.cue_text, r.source_memos_json,
                      r.embedding, i.memo_count, i.day_count
               FROM memo_month_routes r JOIN memo_month_index i ON i.year_month=r.year_month"""
        ).fetchall()
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                semantic = max(0.0, _cosine(query_vec, _deserialize_f32(row["embedding"]))) if row["embedding"] else 0.0
            except Exception:
                semantic = 0.0
            cue_terms = self._feedback_terms(row["cue_text"] or "")
            lexical = len(query_terms & cue_terms) / max(1, min(len(query_terms), 12)) if query_terms else 0.0
            exact = row["year_month"] in explicit_months
            score = min(1.0, semantic * 0.82 + min(1.0, lexical) * 0.18 + (0.35 if exact else 0.0))
            item = merged.setdefault(row["year_month"], {
                "year_month": row["year_month"], "score": 0.0, "semantic": 0.0,
                "lexical": 0.0, "exact": exact, "memo_count": int(row["memo_count"] or 0),
                "day_count": int(row["day_count"] or 0), "route_ids": [], "source_memos": [],
            })
            if score > item["score"]:
                item.update({"score": score, "semantic": semantic, "lexical": lexical})
            item["route_ids"].append(int(row["route_id"] or 0))
            for memo_name in self._json_value(row["source_memos_json"], []):
                if memo_name not in item["source_memos"]:
                    item["source_memos"].append(memo_name)
        values = [item for item in merged.values() if item["exact"] or item["score"] >= 0.42]
        values.sort(key=lambda item: (bool(item["exact"]), float(item["score"])), reverse=True)
        return values[:max(1, min(6, int(limit or 2)))]

    def month_calendar(self, year_month: str) -> dict[str, Any]:
        self.month_index_ensure()
        if not re.fullmatch(r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])", str(year_month or "")):
            return {"year_month": year_month, "days": [], "memo_count": 0}
        days: dict[int, list[dict[str, Any]]] = {}
        for item in self.list_memories(limit=100000):
            item_month, day = self._memory_date_parts(item)
            if item_month != year_month or day <= 0:
                continue
            days.setdefault(day, []).append(item)
        for values in days.values():
            values.sort(key=lambda item: float(item.get("event_ts") or item.get("source_created_ts") or 0))
        overview = next((item for item in self.month_index_overview() if item["year_month"] == year_month), None)
        return {
            "year_month": year_month,
            "memo_count": sum(len(values) for values in days.values()),
            "day_count": len(days),
            "days": [{"day": day, "count": len(days.get(day, [])), "memos": days.get(day, [])} for day in sorted(days)],
            "index": overview or {},
        }

    # ---------- v1.11: Anchor operations ----------
    def anchor_set(self, memo_name: str, anchor_type: str, strength: int = 3, note: str = ""):
        """Create/update a typed long-term anchor for one memo."""
        conn = self._connect()
        self._init_extra_tables()
        anchor_type = (anchor_type or "").strip()
        strength = max(1, min(5, int(strength or 3)))
        now = time.time()
        conn.execute(
            """INSERT INTO memory_anchors(memo_name, anchor_type, strength, note, created_ts, updated_ts)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(memo_name, anchor_type)
               DO UPDATE SET strength=excluded.strength, note=excluded.note, updated_ts=excluded.updated_ts""",
            (memo_name, anchor_type, strength, note or "", now, now),
        )
        conn.commit()

    def anchor_delete(self, memo_name: str, anchor_type: str | None = None) -> int:
        """Delete one anchor type or all anchors for a memo."""
        conn = self._connect()
        self._init_extra_tables()
        if anchor_type:
            cur = conn.execute(
                "DELETE FROM memory_anchors WHERE memo_name=? AND anchor_type=?",
                (memo_name, anchor_type),
            )
        else:
            cur = conn.execute("DELETE FROM memory_anchors WHERE memo_name=?", (memo_name,))
        conn.commit()
        return int(cur.rowcount)

    def anchor_list(self, memo_name: str | None = None) -> list[dict]:
        """List typed anchors, newest first."""
        conn = self._connect()
        self._init_extra_tables()
        if memo_name:
            rows = conn.execute(
                """SELECT memo_name, anchor_type, strength, note, created_ts, updated_ts
                   FROM memory_anchors WHERE memo_name=? ORDER BY strength DESC, updated_ts DESC""",
                (memo_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT memo_name, anchor_type, strength, note, created_ts, updated_ts
                   FROM memory_anchors ORDER BY strength DESC, updated_ts DESC"""
            ).fetchall()
        return [
            {
                "memo_name": r["memo_name"],
                "anchor_type": r["anchor_type"],
                "strength": int(r["strength"] or 3),
                "note": r["note"] or "",
                "created_ts": float(r["created_ts"] or 0),
                "updated_ts": float(r["updated_ts"] or 0),
            }
            for r in rows
        ]

    def anchor_get_boost(self, memo_name: str) -> dict:
        """Return ranking boost and metadata for one memo's anchors."""
        rows = self.anchor_list(memo_name)
        if not rows:
            return {"boost": 0.0, "types": [], "max_strength": 0, "notes": []}
        max_strength = max(int(r.get("strength") or 3) for r in rows)
        boost = min(0.28, 0.04 * max_strength + 0.02 * max(0, len(rows) - 1))
        return {
            "boost": boost,
            "types": [r["anchor_type"] for r in rows],
            "max_strength": max_strength,
            "notes": [r.get("note", "") for r in rows if r.get("note")],
        }

    def health_check(self) -> dict:
        """Return non-mutating index health diagnostics for WebUI."""
        conn = self._connect()
        self._init_extra_tables()
        issues: list[dict] = []

        def add(kind: str, severity: str, memo_name: str, message: str, detail: str = ""):
            issues.append({
                "kind": kind,
                "severity": severity,
                "memo_name": memo_name,
                "message": message,
                "detail": detail,
            })

        total = int(conn.execute("SELECT COUNT(DISTINCT memo_name) AS n FROM chunks").fetchone()["n"])
        chunks = int(conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
        missing_vec = 0
        if self._vec_ok:
            try:
                missing_vec = int(conn.execute(
                    "SELECT COUNT(*) AS n FROM chunks c LEFT JOIN vec_chunks v ON v.rowid=c.id WHERE v.rowid IS NULL"
                ).fetchone()["n"])
            except Exception:
                missing_vec = 0
        else:
            missing_vec = int(conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NULL"
            ).fetchone()["n"])
        if missing_vec:
            add("missing_vector", "high", "", f"{missing_vec} 个 chunk 缺少向量", "建议 /memos-reindex")

        rows = conn.execute(
            """SELECT memo_name, MAX(ts_text) AS ts_text, MAX(occurred_at) AS occurred_at,
                      MAX(event_ts) AS event_ts, MAX(time_basis) AS time_basis,
                      MAX(source_created_ts) AS source_created_ts, MAX(source_updated_ts) AS source_updated_ts,
                      MAX(importance) AS importance,
                      MAX(memory_type) AS memory_type, MAX(long_effect) AS long_effect,
                      MAX(trigger_hint) AS trigger_hint, MIN(chunk_text) AS chunk_text,
                      COUNT(*) AS chunk_count, MAX(created_ts) AS created_ts
               FROM chunks GROUP BY memo_name"""
        ).fetchall()
        seen_preview: dict[str, str] = {}
        day_counts: dict[str, int] = {}
        valid_types = {
            "plot_fact", "relationship_shift", "emotional_anchor",
            "behavior_bias", "promise_or_rule", "daily_texture",
        }
        for r in rows:
            mn = r["memo_name"]
            mt = r["memory_type"] or ""
            imp = int(r["importance"] or 0)
            text = (r["chunk_text"] or "").strip()
            ts_text = r["ts_text"] or ""
            if mt not in valid_types:
                add("bad_type", "medium", mn, "记忆类型异常", mt)
            if imp < 1 or imp > 5:
                add("bad_importance", "medium", mn, "importance 不在 1-5", str(imp))
            if not (r["trigger_hint"] or "").strip():
                add("missing_trigger", "low", mn, "缺少触发线索", text[:120])
            if not float(r["event_ts"] or 0):
                add("unknown_event_time", "low", mn, "记忆发生时间无法确定", ts_text or "未注明时间")
            elif (r["time_basis"] or "unknown") == "source_time_fallback":
                add("inferred_event_time", "low", mn, "发生时间由 Memos 来源时间推定", r["occurred_at"] or ts_text)
            if len(text) > 900:
                add("too_long", "medium", mn, "单条记忆预览过长", f"{len(text)} chars")
            sig = re.sub(r"\s+", "", text[:120])
            if sig and sig in seen_preview and seen_preview[sig] != mn:
                add("possible_duplicate", "low", mn, "疑似重复记忆", seen_preview[sig])
            elif sig:
                seen_preview[sig] = mn
            if ts_text:
                day_counts[ts_text] = day_counts.get(ts_text, 0) + 1
        for day, count in day_counts.items():
            if count >= 30:
                add("day_spike", "medium", "", f"{day} 有 {count} 条记忆", "可能是批量导入或日期异常")

        try:
            feedback_rows = conn.execute(
                """SELECT memo_name, COUNT(*) AS n
                   FROM recall_feedback_events WHERE action='incorrect'
                   GROUP BY memo_name ORDER BY COUNT(*) DESC"""
            ).fetchall()
            for r in feedback_rows:
                add("fact_check_feedback", "medium", r["memo_name"], "收到事实错误反馈，建议核验原日记", f"反馈 {r['n']} 次")
        except Exception:
            pass

        sev_order = {"high": 0, "medium": 1, "low": 2}
        issues.sort(key=lambda x: (sev_order.get(x["severity"], 9), x["kind"], x["memo_name"]))
        return {
            "total_memos": total,
            "total_chunks": chunks,
            "issue_count": len(issues),
            "issues": issues[:500],
            "counts": {
                "high": sum(1 for i in issues if i["severity"] == "high"),
                "medium": sum(1 for i in issues if i["severity"] == "medium"),
                "low": sum(1 for i in issues if i["severity"] == "low"),
            },
        }

    def update_memory_meta(
        self,
        memo_name: str,
        *,
        memory_type: str,
        long_effect: str,
        trigger_hint: str,
        importance: int | None = None,
        scene_anchor: str | None = None,
        retrieval_key: str | None = None,
        state_change: str | None = None,
        entities: list[str] | None = None,
    ) -> int:
        """Update v1.9+ metadata for all chunks of a memo."""
        conn = self._connect()
        assignments = ["memory_type=?", "long_effect=?", "trigger_hint=?"]
        values: list[Any] = [memory_type, long_effect, trigger_hint]
        if importance is not None:
            assignments.append("importance=?")
            values.append(int(importance))
        for column, value in (
            ("scene_anchor", scene_anchor), ("retrieval_key", retrieval_key),
            ("state_change", state_change),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(value)
        if entities is not None:
            assignments.append("entities=?")
            values.append(",".join(str(x).strip() for x in entities if str(x).strip()))
        values.append(memo_name)
        cur = conn.execute(
            f"UPDATE chunks SET {', '.join(assignments)} WHERE memo_name=?",
            tuple(values),
        )
        conn.commit()
        self.invalidate_bm25()
        return int(cur.rowcount)

    def get_memo_meta(self, memo_name: str) -> dict | None:
        """Return one memo's consolidated metadata from chunks."""
        conn = self._connect()
        row = conn.execute(
            """SELECT memo_name, MAX(ts_text) AS ts_text, MAX(occurred_at) AS occurred_at,
                      MAX(event_ts) AS event_ts, MAX(time_basis) AS time_basis,
                      MAX(source_created_ts) AS source_created_ts, MAX(source_updated_ts) AS source_updated_ts,
                      MAX(importance) AS importance, MAX(manual) AS manual, MAX(tags) AS tags, MAX(memory_type) AS memory_type,
                      MAX(long_effect) AS long_effect, MAX(trigger_hint) AS trigger_hint,
                      MAX(scene_anchor) AS scene_anchor, MAX(retrieval_key) AS retrieval_key,
                      MAX(state_change) AS state_change, MAX(entities) AS entities,
                      MAX(content_hash) AS content_hash, COUNT(*) AS passage_count,
                      MIN(chunk_text) AS chunk_text
               FROM chunks WHERE memo_name=? GROUP BY memo_name""",
            (memo_name,),
        ).fetchone()
        if not row:
            return None
        return {
            "memo_name": row["memo_name"],
            "ts_text": row["ts_text"] or "",
            "occurred_at": row["occurred_at"] or "",
            "event_ts": float(row["event_ts"] or 0.0),
            "time_basis": row["time_basis"] or "unknown",
            "source_created_ts": float(row["source_created_ts"] or 0.0),
            "source_updated_ts": float(row["source_updated_ts"] or 0.0),
            "importance": int(row["importance"] or 3),
            "manual": int(row["manual"] or 0),
            "tags": [t for t in (row["tags"] or "").split(",") if t.strip()],
            "memory_type": row["memory_type"] or "plot_fact",
            "long_effect": row["long_effect"] or "",
            "trigger_hint": row["trigger_hint"] or "",
            "scene_anchor": row["scene_anchor"] or "",
            "retrieval_key": row["retrieval_key"] or "",
            "state_change": row["state_change"] or "",
            "entities": [x for x in (row["entities"] or "").split(",") if x.strip()],
            "content_hash": row["content_hash"] or "",
            "passage_count": int(row["passage_count"] or 0),
            "chunk_text": row["chunk_text"] or "",
        }

    def get_memo_passages(self, memo_name: str) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            """SELECT id AS chunk_id, passage_index, char_start, char_end, chunk_text,
                      content_hash, scene_anchor, retrieval_key, state_change, entities
               FROM chunks WHERE memo_name=? ORDER BY passage_index, id""",
            (memo_name,),
        ).fetchall()
        return [{
            "chunk_id": int(r["chunk_id"] or 0),
            "passage_index": int(r["passage_index"] or 0),
            "char_start": int(r["char_start"] or 0),
            "char_end": int(r["char_end"] or 0),
            "text": r["chunk_text"] or "",
            "content_hash": r["content_hash"] or "",
            "scene_anchor": r["scene_anchor"] or "",
            "retrieval_key": r["retrieval_key"] or "",
            "state_change": r["state_change"] or "",
            "entities": [x for x in (r["entities"] or "").split(",") if x.strip()],
        } for r in rows]

    def passage_index_stats(self) -> dict[str, int]:
        conn = self._connect()
        row = conn.execute(
            """SELECT COUNT(*) AS passages, COUNT(DISTINCT memo_name) AS memos,
                      SUM(CASE WHEN COALESCE(content_hash,'')<>'' AND char_end>char_start THEN 1 ELSE 0 END) AS traced,
                      COUNT(DISTINCT CASE WHEN COALESCE(content_hash,'')<>'' AND char_end>char_start THEN memo_name END) AS traced_memos,
                      COUNT(DISTINCT CASE WHEN passage_index>0 THEN memo_name END) AS multi_passage_memos,
                      COUNT(DISTINCT CASE WHEN COALESCE(scene_anchor,'')<>'' OR COALESCE(retrieval_key,'')<>''
                                          OR COALESCE(state_change,'')<>'' OR COALESCE(entities,'')<>'' THEN memo_name END) AS machine_meta_memos
               FROM chunks"""
        ).fetchone()
        return {
            "passages": int(row["passages"] or 0),
            "memos": int(row["memos"] or 0),
            "traced_passages": int(row["traced"] or 0),
            "traced_memos": int(row["traced_memos"] or 0),
            "multi_passage_memos": int(row["multi_passage_memos"] or 0),
            "machine_meta_memos": int(row["machine_meta_memos"] or 0),
        }

    def get_memo_embedding(self, memo_name: str) -> list[float] | None:
        """Get the first chunk embedding for a memo."""
        conn = self._connect()
        row = conn.execute(
            "SELECT embedding FROM chunks WHERE memo_name=? AND embedding IS NOT NULL ORDER BY rowid LIMIT 1",
            (memo_name,)).fetchone()
        if row and row["embedding"]:
            return _deserialize_f32(row["embedding"])
        return None

    def get_memo_embedding_avg(self, memo_name: str) -> list[float] | None:
        """Get an average embedding across all chunks of one memo."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT embedding FROM chunks WHERE memo_name=? AND embedding IS NOT NULL ORDER BY rowid",
            (memo_name,),
        ).fetchall()
        vecs = [_deserialize_f32(r["embedding"]) for r in rows if r["embedding"]]
        if not vecs:
            return None
        dim = len(vecs[0])
        same_dim = [v for v in vecs if len(v) == dim]
        if not same_dim:
            return None
        return [sum(v[i] for v in same_dim) / len(same_dim) for i in range(dim)]

    def get_memo_tags(self, memo_name: str) -> set[str]:
        """Get tags for a memo as a set of tag strings (without #)."""
        conn = self._connect()
        row = conn.execute("SELECT tags FROM chunks WHERE memo_name=? ORDER BY rowid LIMIT 1", (memo_name,)).fetchone()
        if row and row["tags"]:
            return set(t.lstrip("#").strip() for t in row["tags"].replace(",", " ").split() if t.strip())
        return set()

    def _gather_candidates(
        self,
        query_vec: list[float],
        top_k: int,
        min_similarity: float,
        candidate_memo_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """收集通过相关度门槛的候选(未去重、未加权)。sqlite-vec 优先,失败回退纯 Python 余弦。

        过采样 top_k*10(至少 20)条,给后续加权重排留足空间 —— 否则纯 cosine 截断会
        把重要但相关度稍低的日记提前淘汰,加权就没意义了。
        """
        conn = self._connect()
        over = max(top_k * 10, 20)

        if self._vec_ok and candidate_memo_names is None:
            try:
                cur = conn.execute(
                    """SELECT c.id AS chunk_id, c.memo_name, c.chunk_text, c.ts_text, c.tags, c.importance,
                              c.manual, c.memory_type, c.long_effect, c.trigger_hint,
                              c.created_ts, c.occurred_at, c.event_ts, c.time_basis,
                              c.source_created_ts, c.source_updated_ts, c.passage_index,
                              c.char_start, c.char_end, c.content_hash, c.scene_anchor,
                              c.retrieval_key, c.state_change, c.entities, v.distance AS dist
                       FROM vec_chunks v JOIN chunks c ON c.id = v.rowid
                       WHERE v.embedding MATCH ? AND k = ?
                       ORDER BY v.distance""",
                    (_serialize_f32(query_vec), over),
                )
                cands: list[dict] = []
                for r in cur.fetchall():
                    rel = 1.0 - float(r["dist"])
                    if rel < min_similarity:
                        continue
                    cands.append({
                        "chunk_id": int(r["chunk_id"] or 0),
                        "memo_name": r["memo_name"], "chunk_text": r["chunk_text"],
                        "ts_text": r["ts_text"], "tags": r["tags"],
                        "importance": r["importance"], "manual": r["manual"],
                        "memory_type": r["memory_type"], "long_effect": r["long_effect"],
                        "trigger_hint": r["trigger_hint"],
                        "created_ts": r["created_ts"], "occurred_at": r["occurred_at"],
                        "event_ts": r["event_ts"], "time_basis": r["time_basis"],
                        "source_created_ts": r["source_created_ts"], "source_updated_ts": r["source_updated_ts"],
                        "passage_index": int(r["passage_index"] or 0),
                        "char_start": int(r["char_start"] or 0), "char_end": int(r["char_end"] or 0),
                        "content_hash": r["content_hash"] or "", "scene_anchor": r["scene_anchor"] or "",
                        "retrieval_key": r["retrieval_key"] or "", "state_change": r["state_change"] or "",
                        "entities": r["entities"] or "",
                        "relevance": rel,
                    })
                return cands
            except Exception as exc:
                logger.warning("[memos-mem] vec 检索失败,回退余弦: %s", exc)

        sql = """SELECT id AS chunk_id, memo_name, chunk_text, ts_text, tags, importance, manual,
                      memory_type, long_effect, trigger_hint, created_ts, occurred_at,
                      event_ts, time_basis, source_created_ts, source_updated_ts,
                      passage_index, char_start, char_end, content_hash, scene_anchor,
                      retrieval_key, state_change, entities, embedding
               FROM chunks"""
        params: list[Any] = []
        candidate_names: set[str] | None = None
        if candidate_memo_names is not None:
            candidate_names = {str(name) for name in candidate_memo_names if name}
            if not candidate_names:
                return []
            # Keep well below SQLite's variable limit. Large months are still
            # supported by scanning local rows and filtering before cosine work.
            if len(candidate_names) <= 900:
                names = sorted(candidate_names)
                sql += " WHERE memo_name IN (" + ",".join("?" for _ in names) + ")"
                params.extend(names)
        cur = conn.execute(sql, params)
        cands = []
        for r in cur.fetchall():
            if candidate_names is not None and r["memo_name"] not in candidate_names:
                continue
            blob = r["embedding"]
            if not blob:
                continue
            rel = _cosine(query_vec, _deserialize_f32(blob))
            if rel < min_similarity:
                continue
            cands.append({
                "chunk_id": int(r["chunk_id"] or 0),
                "memo_name": r["memo_name"], "chunk_text": r["chunk_text"],
                "ts_text": r["ts_text"], "tags": r["tags"],
                "importance": r["importance"], "manual": r["manual"],
                "memory_type": r["memory_type"], "long_effect": r["long_effect"],
                "trigger_hint": r["trigger_hint"],
                "created_ts": r["created_ts"], "occurred_at": r["occurred_at"],
                "event_ts": r["event_ts"], "time_basis": r["time_basis"],
                "source_created_ts": r["source_created_ts"], "source_updated_ts": r["source_updated_ts"],
                "passage_index": int(r["passage_index"] or 0),
                "char_start": int(r["char_start"] or 0), "char_end": int(r["char_end"] or 0),
                "content_hash": r["content_hash"] or "", "scene_anchor": r["scene_anchor"] or "",
                "retrieval_key": r["retrieval_key"] or "", "state_change": r["state_change"] or "",
                "entities": r["entities"] or "",
                "relevance": rel,
            })
        return cands

    # ---------- 维护 ----------
    async def clear_all(self) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM chunks")
        # Reindex rebuilds derived index data only. Query feedback and legacy
        # product data must survive it.
        for tbl in (
            "memo_graph", "memo_similarity_edges", "memo_similarity_clusters",
            "memo_keywords", "memo_month_index", "memo_month_routes",
        ):
            try: conn.execute(f"DELETE FROM {tbl}")
            except Exception: pass
        if self._vec_ok:
            try:
                conn.execute("DELETE FROM vec_chunks")
            except Exception:
                pass
        conn.commit()
        self.invalidate_bm25()
        self.invalidate_month_index()

    def count(self) -> int:
        cur = self._connect().execute("SELECT COUNT(*) AS n FROM chunks")
        return int(cur.fetchone()["n"])

    def distinct_memo_count(self) -> int:
        """去重后的日记条数(一篇日记可能拆成多 chunk)。"""
        cur = self._connect().execute(
            "SELECT COUNT(DISTINCT memo_name) AS n FROM chunks")
        return int(cur.fetchone()["n"])

    def all_memo_names(self) -> set:
        """vec 库里所有 memo_name 集合。用于对账(reconcile)。"""
        cur = self._connect().execute("SELECT DISTINCT memo_name FROM chunks")
        return {r["memo_name"] for r in cur.fetchall()}

    def delete_by_memo_name(self, memo_name: str) -> int:
        """删某篇日记的所有 chunk(含 vec_chunks 同步清)。返回删除条数。
        别名 of delete_memo,给 reconcile 用。"""
        return self.delete_memo(memo_name)

    def list_memories(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        """列出全部去重后的日记元信息(按 created_ts 倒序)。
        一篇日记可能拆成多 chunk,这里 GROUP BY memo_name 去重,每篇取首个 chunk 的预览。
        webui 用这个拿数据画表格/饼图/时间轴。"""
        cur = self._connect().execute(
            """SELECT memo_name,
                      MAX(ts_text)    AS ts_text,
                      MAX(occurred_at) AS occurred_at,
                      MAX(event_ts) AS event_ts,
                      MAX(time_basis) AS time_basis,
                      MAX(source_created_ts) AS source_created_ts,
                      MAX(source_updated_ts) AS source_updated_ts,
                      MAX(importance) AS importance,
                      MAX(created_ts) AS created_ts,
                      MAX(manual)     AS manual,
                      MAX(tags)       AS tags,
                      MAX(memory_type) AS memory_type,
                      MAX(long_effect) AS long_effect,
                      MAX(trigger_hint) AS trigger_hint,
                      MIN(chunk_text) AS chunk_text
               FROM chunks
               GROUP BY memo_name
               ORDER BY COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        out = []
        for r in cur:
            tags_raw = r["tags"] or ""
            tags = [t for t in tags_raw.split(",") if t.strip()] if tags_raw else []
            out.append({
                "memo_name": r["memo_name"],
                "ts_text": r["ts_text"] or "",
                "occurred_at": r["occurred_at"] or "",
                "event_ts": float(r["event_ts"] or 0.0),
                "time_basis": r["time_basis"] or "unknown",
                "source_created_ts": float(r["source_created_ts"] or 0.0),
                "source_updated_ts": float(r["source_updated_ts"] or 0.0),
                "importance": int(r["importance"] or 3),
                "created_ts": float(r["created_ts"] or 0),
                "manual": bool(r["manual"] or 0),
                "tags": tags,
                "memory_type": r["memory_type"] or "plot_fact",
                "long_effect": r["long_effect"] or "",
                "trigger_hint": r["trigger_hint"] or "",
                "preview": (r["chunk_text"] or "")[:120],
            })
        return out

    def memo_name_exists(self, memo_name: str) -> bool:
        cur = self._connect().execute(
            "SELECT 1 FROM chunks WHERE memo_name=? LIMIT 1", (memo_name,)
        )
        return cur.fetchone() is not None

    def delete_memo(self, memo_name: str) -> int:
        """删某篇日记的所有 chunk(含 vec_chunks 同步清)。返回删除条数。
        注意:必须先查出 id 再删 chunks,否则子查询在 chunks 删空后拿不到 id。
        """
        conn = self._connect()
        rows = conn.execute(
            "SELECT id FROM chunks WHERE memo_name=?", (memo_name,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        n = conn.execute("DELETE FROM chunks WHERE memo_name=?", (memo_name,)).rowcount
        conn.execute("DELETE FROM memo_keywords WHERE memo_name=?", (memo_name,))
        conn.execute("DELETE FROM memo_feedback WHERE memo_name=?", (memo_name,))
        conn.execute("DELETE FROM recall_feedback_events WHERE memo_name=?", (memo_name,))
        conn.execute("DELETE FROM memo_similarity_clusters WHERE memo_name=?", (memo_name,))
        conn.execute("DELETE FROM memo_similarity_edges WHERE memo_a=? OR memo_b=?", (memo_name, memo_name))
        try:
            conn.execute("DELETE FROM memory_anchors WHERE memo_name=?", (memo_name,))
        except Exception:
            pass
        conn.execute("DELETE FROM memo_graph WHERE memo_a=? OR memo_b=?", (memo_name, memo_name))
        if self._vec_ok and self.emb_dim and ids:
            try:
                conn.executemany(
                    "DELETE FROM vec_chunks WHERE rowid=?",
                    [(i,) for i in ids],
                )
            except Exception:
                pass
        conn.commit()
        if n:
            self.invalidate_bm25()
            self.invalidate_month_index()
        return int(n)

    def delete_memo_index(self, memo_name: str) -> int:
        """Replace derived chunks without deleting query feedback or other user data."""
        conn = self._connect()
        rows = conn.execute("SELECT id FROM chunks WHERE memo_name=?", (memo_name,)).fetchall()
        ids = [int(r["id"]) for r in rows]
        n = conn.execute("DELETE FROM chunks WHERE memo_name=?", (memo_name,)).rowcount
        conn.execute("DELETE FROM memo_keywords WHERE memo_name=?", (memo_name,))
        conn.execute("DELETE FROM memo_similarity_clusters WHERE memo_name=?", (memo_name,))
        conn.execute("DELETE FROM memo_similarity_edges WHERE memo_a=? OR memo_b=?", (memo_name, memo_name))
        conn.execute("DELETE FROM memo_graph WHERE memo_a=? OR memo_b=?", (memo_name, memo_name))
        if self._vec_ok and self.emb_dim and ids:
            try:
                conn.executemany("DELETE FROM vec_chunks WHERE rowid=?", [(i,) for i in ids])
            except Exception:
                pass
        conn.commit()
        if n:
            self.invalidate_bm25()
            self.invalidate_month_index()
        return int(n)
