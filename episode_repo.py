"""EpisodeRepo Repository (4.6.0-test2 / 3.2, 4.6.0 schema bump).

Owns episodes / episode_evidence / episode_turn_links / diary_rollbacks plus
the recall eval tables. Vector tables and database identity/snapshots belong
elsewhere; this class only knows the active `VectorGeneration` to address
`vec_episode_cards_g{gen}`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import Any, Callable

from .store_utils import cosine, deserialize_f32, escape_like, extract_terms, json_list, serialize_f32
from .vector_generation import VectorGeneration

logger = logging.getLogger(__name__)

SUPPORTED_TIERS = ("must_write", "supporting", "archive_only")


def normalize_tier(value: Any) -> str:
    val = str(value or "supporting").strip()
    return val if val in SUPPORTED_TIERS else "supporting"


class EpisodeRepo:
    """Repository: episode cards + grounded evidence + rollbacks."""

    def __init__(
        self,
        gen: VectorGeneration,
        get_conn: Callable[[], sqlite3.Connection],
        lock: threading.RLock,
    ):
        self._gen = gen
        self._get_conn = get_conn
        self._lock = lock

    # ---- schema --------------------------------------------------------

    def init_schema(self, conn: sqlite3.Connection) -> None:
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
                embedding_generation TEXT DEFAULT '',
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
                evidence_tier TEXT DEFAULT 'supporting',
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS diary_rollbacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memo_name TEXT NOT NULL,
                episode_id TEXT DEFAULT '',
                old_content TEXT NOT NULL,
                new_content TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_ts REAL NOT NULL,
                reverted_ts REAL
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recall_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                query_text TEXT NOT NULL DEFAULT '',
                query_hash TEXT NOT NULL DEFAULT '',
                intent TEXT NOT NULL DEFAULT '',
                safety_triggered INTEGER NOT NULL DEFAULT 0,
                safety_reason TEXT NOT NULL DEFAULT '',
                rescue_candidates INTEGER NOT NULL DEFAULT 0,
                rescue_added INTEGER NOT NULL DEFAULT 0,
                rescue_merged INTEGER NOT NULL DEFAULT 0,
                selected_before_json TEXT NOT NULL DEFAULT '[]',
                selected_after_json TEXT NOT NULL DEFAULT '[]',
                rescue_selected_json TEXT NOT NULL DEFAULT '[]',
                outcome TEXT NOT NULL DEFAULT 'not_triggered',
                feedback TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            )"""
        )
        # idempotent column adds (4.6.0-test2)
        episode_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(episodes)").fetchall()}
        for col, ddl in (
            ("scene_start_turn", "ALTER TABLE episodes ADD COLUMN scene_start_turn INTEGER DEFAULT -1"),
            ("scene_end_turn", "ALTER TABLE episodes ADD COLUMN scene_end_turn INTEGER DEFAULT -1"),
            ("scene_boundary_reasons_json", "ALTER TABLE episodes ADD COLUMN scene_boundary_reasons_json TEXT DEFAULT '[]'"),
            ("diary_render_version", "ALTER TABLE episodes ADD COLUMN diary_render_version TEXT DEFAULT ''"),
            ("must_coverage", "ALTER TABLE episodes ADD COLUMN must_coverage REAL DEFAULT -1"),
            ("support_coverage", "ALTER TABLE episodes ADD COLUMN support_coverage REAL DEFAULT -1"),
            ("transcript_risk", "ALTER TABLE episodes ADD COLUMN transcript_risk REAL DEFAULT -1"),
            ("render_retry_reason", "ALTER TABLE episodes ADD COLUMN render_retry_reason TEXT DEFAULT ''"),
            ("original_memo_version", "ALTER TABLE episodes ADD COLUMN original_memo_version TEXT DEFAULT ''"),
            ("source_overlap_ratio", "ALTER TABLE episodes ADD COLUMN source_overlap_ratio REAL DEFAULT -1"),
            ("direct_quote_ratio", "ALTER TABLE episodes ADD COLUMN direct_quote_ratio REAL DEFAULT -1"),
            ("compression_ratio", "ALTER TABLE episodes ADD COLUMN compression_ratio REAL DEFAULT -1"),
            ("render_fallback", "ALTER TABLE episodes ADD COLUMN render_fallback INTEGER DEFAULT 0"),
        ):
            if col not in episode_cols:
                conn.execute(ddl)
        ev_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(episode_evidence)").fetchall()}
        if "evidence_tier" not in ev_cols:
            conn.execute("ALTER TABLE episode_evidence ADD COLUMN evidence_tier TEXT DEFAULT 'supporting'")
        ep_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(episodes)").fetchall()}
        if "embedding_generation" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN embedding_generation TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_active ON episodes(active, event_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_memo ON episodes(memo_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_episode ON episode_evidence(episode_id, evidence_index)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_turn_link ON episode_turn_links(batch_id, turn_index)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_cases_enabled ON recall_eval_cases(enabled, updated_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_observations_time ON recall_observations(created_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_observations_outcome ON recall_observations(outcome, feedback)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rollback_memo ON diary_rollbacks(memo_name)")
        # backfill legacy turn_links
        legacy_links = conn.execute(
            """SELECT e.episode_id,e.source_batch_id,v.evidence_index,v.turn_indexes_json
               FROM episodes e JOIN episode_evidence v ON v.episode_id=e.episode_id
               WHERE e.source_batch_id IS NOT NULL AND e.source_batch_id!=''"""
        ).fetchall()
        for row in legacy_links:
            for value in json_list(row["turn_indexes_json"]):
                try:
                    turn_index = int(value)
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO episode_turn_links
                       (episode_id,batch_id,turn_index,evidence_index) VALUES (?,?,?,?)""",
                    (row["episode_id"], row["source_batch_id"], turn_index, int(row["evidence_index"] or 0)),
                )

    @staticmethod
    def episode_id_for_memo(memo_name: str) -> str:
        return "ep_" + hashlib.sha256(str(memo_name).encode("utf-8")).hexdigest()[:24]

    # ---- upsert --------------------------------------------------------

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
        diary_render_version: str = "",
        must_coverage: float = -1.0,
        support_coverage: float = -1.0,
        transcript_risk: float = -1.0,
        render_retry_reason: str = "",
        original_memo_version: str = "",
        source_overlap_ratio: float = -1.0,
        direct_quote_ratio: float = -1.0,
        compression_ratio: float = -1.0,
        render_fallback: bool = False,
        runtime_gen: str = "",
    ) -> str:
        memo_name = str(memo_name or "").strip()
        if not memo_name:
            raise ValueError("memo_name is required")
        episode_id = str(episode.get("episode_id") or self.episode_id_for_memo(memo_name))
        entities = episode.get("entities") if isinstance(episode.get("entities"), list) else []
        unresolved = episode.get("unresolved") if isinstance(episode.get("unresolved"), list) else []
        raw_evidence = episode.get("evidence") if isinstance(episode.get("evidence"), list) else []
        evidence: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for item in raw_evidence:
            if not isinstance(item, dict):
                continue
            indexes = item.get("turn_indexes") if isinstance(item.get("turn_indexes"), list) else []
            sig = (
                str(item.get("kind") or "event").strip(),
                str(item.get("actor") or "").strip(),
                " ".join(str(item.get("detail") or item.get("fact") or "").split()),
                " ".join(str(item.get("quote") or item.get("quote_text") or "").split()),
                tuple(indexes),
            )
            if sig in seen:
                continue
            seen.add(sig)
            evidence.append(item)
        now = time.time()
        gen = runtime_gen or self._gen.active_generation() or ""
        active_gen = self._gen.active_generation()
        canonical_embedding = serialize_f32(embedding) if embedding and gen == active_gen else None
        canonical_gen = gen if canonical_embedding else ""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO episodes
                   (episode_id,memo_name,source_batch_id,source_kind,legacy,active,occurred_at,event_ts,time_basis,
                    memory_type,importance,scene_anchor,retrieval_key,state_change,long_effect,trigger_hint,
                    entities_json,affect_before,affect_after,unresolved_json,card_text,diary_content_hash,
                    evidence_quality,source_updated_ts,embedding,embedding_generation,created_ts,updated_ts,
                    scene_start_turn,scene_end_turn,scene_boundary_reasons_json,
                    diary_render_version,must_coverage,support_coverage,transcript_risk,
                     render_retry_reason,original_memo_version,source_overlap_ratio,
                     direct_quote_ratio,compression_ratio,render_fallback)
                   VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    source_updated_ts=excluded.source_updated_ts,
                    embedding=CASE WHEN excluded.embedding IS NOT NULL THEN excluded.embedding ELSE episodes.embedding END,
                    embedding_generation=CASE WHEN excluded.embedding IS NOT NULL THEN excluded.embedding_generation ELSE episodes.embedding_generation END,
                    updated_ts=excluded.updated_ts,
                    scene_start_turn=excluded.scene_start_turn,scene_end_turn=excluded.scene_end_turn,
                    scene_boundary_reasons_json=excluded.scene_boundary_reasons_json,
                    diary_render_version=excluded.diary_render_version,must_coverage=excluded.must_coverage,
                    support_coverage=excluded.support_coverage,transcript_risk=excluded.transcript_risk,
                     render_retry_reason=excluded.render_retry_reason,original_memo_version=excluded.original_memo_version,
                     source_overlap_ratio=excluded.source_overlap_ratio,direct_quote_ratio=excluded.direct_quote_ratio,
                     compression_ratio=excluded.compression_ratio,render_fallback=excluded.render_fallback""",
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
                    float(source_updated_ts or 0), canonical_embedding, canonical_gen, now, now,
                    int(episode.get("scene_start_turn")) if episode.get("scene_start_turn") is not None else -1,
                    int(episode.get("scene_end_turn")) if episode.get("scene_end_turn") is not None else -1,
                    json.dumps(
                        episode.get("scene_boundary_reasons")
                        if isinstance(episode.get("scene_boundary_reasons"), list) else [],
                        ensure_ascii=False,
                    ),
                    str(diary_render_version or ""),
                    float(must_coverage if must_coverage is not None else -1.0),
                    float(support_coverage if support_coverage is not None else -1.0),
                    float(transcript_risk if transcript_risk is not None else -1.0),
                    str(render_retry_reason or ""),
                    str(original_memo_version or ""),
                    float(source_overlap_ratio if source_overlap_ratio is not None else -1.0),
                    float(direct_quote_ratio if direct_quote_ratio is not None else -1.0),
                    float(compression_ratio if compression_ratio is not None else -1.0),
                    1 if render_fallback else 0,
                ),
            )
            row = conn.execute("SELECT id FROM episodes WHERE memo_name=?", (memo_name,)).fetchone()
            row_id = int(row["id"])
            if embedding and gen:
                conn.execute(
                    """INSERT OR REPLACE INTO embedding_versions
                       (kind,row_id,generation,embedding,created_ts) VALUES ('card',?,?,?,?)""",
                    (row_id, gen, serialize_f32(embedding), now),
                )
            conn.execute("DELETE FROM episode_evidence WHERE episode_id=?", (episode_id,))
            conn.execute("DELETE FROM episode_turn_links WHERE episode_id=?", (episode_id,))
            for index, item in enumerate(evidence):
                detail = str(item.get("detail") or item.get("fact") or "").strip()
                quote = str(item.get("quote") or item.get("quote_text") or "").strip()
                if not detail and not quote:
                    continue
                tier = normalize_tier(item.get("tier"))
                conn.execute(
                    """INSERT INTO episode_evidence
                       (episode_id,evidence_index,kind,actor,detail,quote_text,turn_indexes_json,
                        confidence,grounded,evidence_tier)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        episode_id, index, str(item.get("kind") or "event"), str(item.get("actor") or ""),
                        detail or quote, quote,
                        json.dumps(item.get("turn_indexes") if isinstance(item.get("turn_indexes"), list) else []),
                        max(0.0, min(1.0, float(item.get("confidence") or 0.5))),
                        1 if item.get("grounded") else 0,
                        tier,
                    ),
                )
                if source_batch_id:
                    for value in (item.get("turn_indexes") or []):
                        try:
                            turn_index = int(value)
                        except (TypeError, ValueError):
                            continue
                        conn.execute(
                            """INSERT OR IGNORE INTO episode_turn_links
                               (episode_id,batch_id,turn_index,evidence_index) VALUES (?,?,?,?)""",
                            (episode_id, source_batch_id, turn_index, index),
                        )
            if self._gen._vec_ok and gen:
                try:
                    tbl = self._gen.table_name_for("card", gen)
                    conn.execute(f"DELETE FROM {tbl} WHERE rowid=?", (row_id,))
                    if embedding:
                        conn.execute(
                            f"INSERT INTO {tbl}(rowid,embedding) VALUES (?,?)",
                            (row_id, serialize_f32(embedding)),
                        )
                except Exception as exc:
                    logger.debug("[memory][episode] card vector upsert failed gen=%s: %s", gen[:8], exc)
            conn.commit()
        return episode_id

    # ---- reads ---------------------------------------------------------

    def get_episode(self, memo_name: str) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT * FROM episodes WHERE memo_name=? AND active=1", (memo_name,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["entities"] = json_list(data.pop("entities_json", "[]"))
        data["unresolved"] = json_list(data.pop("unresolved_json", "[]"))
        active_gen = self._gen.active_generation()
        data["embedding_ready"] = bool(data.get("embedding")) and (
            not active_gen or str(data.get("embedding_generation") or "") == active_gen
        )
        data.pop("embedding", None)
        return data

    def get_episode_by_id(self, episode_id: str) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT * FROM episodes WHERE episode_id=? AND active=1", (str(episode_id),),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["entities"] = json_list(data.pop("entities_json", "[]"))
        data["unresolved"] = json_list(data.pop("unresolved_json", "[]"))
        data.pop("embedding", None)
        return data

    def list_episodes(self, *, query: str = "", quality: str = "",
                      limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        clauses = ["active=1"]
        params: list[Any] = []
        if quality:
            clauses.append("evidence_quality=?")
            params.append(quality)
        rows = self._get_conn().execute(
            f"""SELECT episode_id,memo_name,source_batch_id,source_kind,legacy,
                       occurred_at,event_ts,time_basis,memory_type,importance,
                       scene_anchor,retrieval_key,state_change,long_effect,trigger_hint,
                       entities_json,affect_before,affect_after,unresolved_json,card_text,
                       diary_content_hash,evidence_quality,source_updated_ts,created_ts,updated_ts,
                       scene_start_turn,scene_end_turn,must_coverage,support_coverage,transcript_risk,
                       source_overlap_ratio,direct_quote_ratio,compression_ratio,render_retry_reason,
                       render_fallback,diary_render_version
                FROM episodes WHERE {' AND '.join(clauses)}
                ORDER BY event_ts DESC, updated_ts DESC LIMIT ? OFFSET ?""",
            (*params, max(1, min(1000, int(limit))), max(0, int(offset))),
        ).fetchall()
        query_terms = extract_terms(query)
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
            item["entities"] = json_list(item.pop("entities_json", "[]"))
            item["unresolved"] = json_list(item.pop("unresolved_json", "[]"))
            result.append(item)
        return result

    def episode_detail(self, memo_name: str) -> dict[str, Any] | None:
        episode = self.get_episode(memo_name)
        if not episode:
            return None
        episode["evidence"] = self.evidence_for_memo(memo_name, "", limit=100)
        if episode.get("source_batch_id"):
            row = self._get_conn().execute(
                "SELECT turn_index,role,content,event_ts,event_timezone FROM source_turns "
                "WHERE batch_id=? ORDER BY turn_index",
                (str(episode["source_batch_id"]),),
            ).fetchall()
            episode["source_turns"] = [dict(r) for r in row]
        else:
            episode["source_turns"] = []
        return episode

    def episodes_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        if not batch_id:
            return []
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT memo_name FROM episodes WHERE active=1 AND source_batch_id=? ORDER BY event_ts,id",
                (str(batch_id),),
            ).fetchall()
        out = []
        for row in rows:
            detail = self.episode_detail(str(row["memo_name"]))
            if detail:
                out.append(detail)
        return out

    def memo_names(self) -> set[str]:
        rows = self._get_conn().execute("SELECT memo_name FROM episodes WHERE active=1").fetchall()
        return {str(row["memo_name"]) for row in rows}

    def delete_by_memo_name(self, memo_name: str) -> int:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT id FROM episodes WHERE memo_name=?", (memo_name,)).fetchone()
            if not row:
                return 0
            row_id = int(row["id"])
            conn.execute("DELETE FROM episodes WHERE id=?", (row_id,))
            if self._gen._vec_ok:
                active_gen = self._gen.active_generation()
                if active_gen:
                    try:
                        tbl = self._gen.table_name("card")
                        conn.execute(f"DELETE FROM {tbl} WHERE rowid=?", (row_id,))
                    except Exception:
                        pass
            conn.commit()
            return 1

    def evidence_for_memo(self, memo_name: str, query: str = "", limit: int = 3) -> list[dict[str, Any]]:
        ep = self.get_episode(memo_name)
        if not ep:
            return []
        rows = self._get_conn().execute(
            """SELECT kind,actor,detail,quote_text,turn_indexes_json,confidence,grounded,evidence_tier
               FROM episode_evidence WHERE episode_id=? ORDER BY evidence_index""",
            (ep["episode_id"],),
        ).fetchall()
        query_terms = extract_terms(query)
        scored = []
        for index, row in enumerate(rows):
            data = dict(row)
            text = " ".join([data.get("actor") or "", data.get("detail") or "", data.get("quote_text") or ""])
            item_terms = extract_terms(text)
            overlap = len(query_terms & item_terms) / max(1, len(query_terms)) if query_terms else 0.0
            score = overlap * 0.75 + float(data.get("confidence") or 0.0) * 0.20 + (0.05 if data.get("grounded") else 0.0)
            data["turn_indexes"] = json_list(data.pop("turn_indexes_json", "[]"))
            data["tier"] = data.pop("evidence_tier")
            data["match_score"] = round(score, 4)
            scored.append((score, -index, data))
        scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored[:max(0, int(limit))]]

    def episode_evidence_count_by_tier(self) -> dict[str, int]:
        rows = self._get_conn().execute(
            "SELECT evidence_tier, COUNT(*) n FROM episode_evidence GROUP BY evidence_tier"
        ).fetchall()
        out = {tier: 0 for tier in SUPPORTED_TIERS}
        for row in rows:
            out[str(row["evidence_tier"] or "supporting")] = int(row["n"] or 0)
        return out

    # ---- rollback mapping ---------------------------------------------

    def record_rollback(self, *, old_memo_name: str, episode_id: str,
                        new_memo_names: list[str], note: str = "",
                        commit: bool = True, return_last_id: bool = False) -> int:
        if not new_memo_names:
            return 0
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            count = 0
            last_id = 0
            for new_name in new_memo_names:
                if not new_name:
                    continue
                cur = conn.execute(
                    """INSERT INTO diary_rollbacks
                       (memo_name,episode_id,old_content,new_content,note,created_ts)
                       VALUES (?,?,?,?,?,?)""",
                    (str(new_name), str(episode_id), str(old_memo_name), str(new_name),
                     str(note or "")[:500], now),
                )
                count += 1
                last_id = int(cur.lastrowid or 0)
            if commit:
                conn.commit()
            return last_id if return_last_id else count

    def rollback_rows_for_memo(self, memo_name: str) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            """SELECT id,memo_name,episode_id,old_content,new_content,note,created_ts,reverted_ts
               FROM diary_rollbacks WHERE memo_name=? ORDER BY created_ts DESC""",
            (str(memo_name),),
        ).fetchall()
        return [dict(row) for row in rows]

    def rollback_history(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            """SELECT id,memo_name,episode_id,old_content,new_content,note,created_ts,reverted_ts
               FROM diary_rollbacks ORDER BY created_ts DESC LIMIT ?""",
            (max(1, min(200, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_rollback_reverted(self, rollback_id: int) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE diary_rollbacks SET reverted_ts=? WHERE id=?", (time.time(), int(rollback_id))
            )
            conn.commit()

    # ---- embedding migration -------------------------------------------

    def card_embedding_rows(self, *, after_id: int = 0, limit: int = 64,
                            target_generation: str = "") -> list[dict[str, Any]]:
        clauses = ["e.id>?", "e.active=1"]
        params: list[Any] = [max(0, int(after_id))]
        if target_generation:
            clauses.append("NOT EXISTS (SELECT 1 FROM embedding_versions ev WHERE ev.kind='card' AND ev.row_id=e.id AND ev.generation=?)")
            params.append(str(target_generation))
        params.append(max(1, min(500, int(limit))))
        rows = self._get_conn().execute(
            f"""SELECT e.id,e.memo_name,e.card_text FROM episodes e
                WHERE {' AND '.join(clauses)} ORDER BY e.id LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def replace_card_embeddings(self, rows: list[tuple[int, list[float]]],
                                runtime_gen: str) -> int:
        if not rows or not runtime_gen:
            return 0
        with self._lock:
            conn = self._get_conn()
            active_gen = self._gen.active_generation()
            table = self._gen.table_name_for("card", runtime_gen) if self._gen._vec_ok else ""
            updated = 0
            try:
                conn.execute("BEGIN")
                for row_id, vector in rows:
                    exists = conn.execute("SELECT 1 FROM episodes WHERE id=?", (int(row_id),)).fetchone()
                    if not exists:
                        continue
                    blob = serialize_f32(vector)
                    conn.execute(
                        """INSERT OR REPLACE INTO embedding_versions
                           (kind,row_id,generation,embedding,created_ts) VALUES ('card',?,?,?,?)""",
                        (int(row_id), runtime_gen, blob, time.time()),
                    )
                    if runtime_gen == active_gen:
                        conn.execute(
                            "UPDATE episodes SET embedding=?,embedding_generation=? WHERE id=?",
                            (blob, runtime_gen, int(row_id)),
                        )
                    if table:
                        conn.execute(f"DELETE FROM {table} WHERE rowid=?", (int(row_id),))
                        conn.execute(f"INSERT INTO {table}(rowid,embedding) VALUES (?,?)", (int(row_id), blob))
                    updated += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return updated

    # ---- search --------------------------------------------------------

    def search_cards(self, query_vec: list[float], query_text: str, limit: int = 18) -> list[dict[str, Any]]:
        with self._lock:
            return self._search_cards_locked(query_vec, query_text, limit)

    def _search_cards_locked(self, query_vec: list[float], query_text: str, limit: int = 18) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        conn = self._get_conn()
        candidates: dict[int, dict[str, Any]] = {}
        active_gen = self._gen.active_generation()
        if self._gen._vec_ok and query_vec:
            tbl = self._gen.table_name("card")
            if tbl:
                try:
                    rows = conn.execute(
                        f"""SELECT e.*,v.distance AS distance
                            FROM {tbl} v JOIN episodes e ON e.id=v.rowid
                            WHERE v.embedding MATCH ? AND k=? AND e.active=1 ORDER BY v.distance""",
                        (serialize_f32(query_vec), max(limit * 3, 30)),
                    ).fetchall()
                    for row in rows:
                        item = dict(row)
                        item["semantic"] = 1.0 - float(item.pop("distance") or 0.0)
                        candidates[int(item["id"])] = item
                except Exception as exc:
                    logger.debug("[memory][episode] vector card search failed: %s", exc)
        if not candidates:
            if active_gen:
                rows = conn.execute(
                    """SELECT e.*,ev.embedding AS version_embedding
                       FROM embedding_versions ev JOIN episodes e ON e.id=ev.row_id
                       WHERE ev.kind='card' AND ev.generation=? AND e.active=1""",
                    (active_gen,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT e.*,e.embedding AS version_embedding FROM episodes e WHERE e.active=1 AND e.embedding IS NOT NULL"
                ).fetchall()
            for row in rows:
                item = dict(row)
                item["semantic"] = cosine(
                    query_vec, deserialize_f32(item.pop("version_embedding", None))
                )
                candidates[int(item["id"])] = item

        query_terms = extract_terms(query_text)
        if query_terms:
            terms = sorted(query_terms, key=len, reverse=True)[:64]
            like_clause = " OR ".join("LOWER(card_text) LIKE ? ESCAPE '\\'" for _ in terms)
            params = [f"%{escape_like(term)}%" for term in terms]
            rows = conn.execute(
                f"SELECT * FROM episodes WHERE active=1 AND ({like_clause})", params
            ).fetchall()
            for row in rows:
                item = dict(row)
                if int(item["id"]) not in candidates:
                    item["semantic"] = cosine(query_vec, deserialize_f32(item.get("embedding")))
                    candidates[int(item["id"])] = item

        out = []
        for item in candidates.values():
            card_terms = extract_terms(item.get("card_text") or "")
            overlap = len(query_terms & card_terms) / max(1, len(query_terms)) if query_terms else 0.0
            exact = sum(1 for term in query_terms if term in str(item.get("card_text") or "").lower())
            exact_norm = exact / max(1, len(query_terms)) if query_terms else 0.0
            semantic = max(-1.0, min(1.0, float(item.get("semantic") or 0.0)))
            importance = max(1, min(5, int(item.get("importance") or 3)))
            score = semantic * 0.72 + overlap * 0.16 + exact_norm * 0.08 + ((importance - 1) / 4) * 0.04
            item["lexical"] = round(max(overlap, exact_norm), 4)
            item["relevance"] = round(max(0.0, semantic * 0.82 + max(overlap, exact_norm) * 0.18), 6)
            item["score"] = round(score, 6)
            item["entities"] = json_list(item.pop("entities_json", "[]"))
            item["unresolved"] = json_list(item.pop("unresolved_json", "[]"))
            item.pop("embedding", None)
            out.append(item)
        out.sort(key=lambda i: (float(i.get("score") or 0.0), float(i.get("relevance") or 0.0)), reverse=True)
        return out[:limit]

    # ---- eval cases ----------------------------------------------------

    def upsert_eval_case(self, query: str, expected_memos: list[str], *,
                         case_id: str = "", note: str = "", source: str = "manual",
                         enabled: bool = True) -> dict[str, Any]:
        query = " ".join(str(query or "").split()).strip()
        expected = list(dict.fromkeys(str(value).strip() for value in expected_memos if str(value).strip()))
        if not query or not expected:
            raise ValueError("query and expected_memos are required")
        case_id = str(case_id or "").strip() or "case_" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:20]
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            existing = conn.execute(
                "SELECT created_ts,expected_memos_json FROM recall_eval_cases WHERE case_id=?", (case_id,)
            ).fetchone()
            if existing:
                expected = list(dict.fromkeys(json_list(existing["expected_memos_json"]) + expected))
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
            rows = self._get_conn().execute(sql, (max(1, min(2000, int(limit))),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["expected_memos"] = json_list(item.pop("expected_memos_json", "[]"))
            item["enabled"] = bool(item.get("enabled"))
            out.append(item)
        return out

    def delete_eval_case(self, case_id: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute("DELETE FROM recall_eval_cases WHERE case_id=?", (str(case_id),))
            conn.commit()
            return bool(cur.rowcount)

    def record_eval_run(self, mode: str, metrics: dict[str, Any], details: list[dict[str, Any]]) -> int:
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                """INSERT INTO recall_eval_runs(created_ts,mode,case_count,metrics_json,details_json)
                   VALUES (?,?,?,?,?)""",
                (time.time(), str(mode), len(details), json.dumps(metrics, ensure_ascii=False),
                 json.dumps(details, ensure_ascii=False)),
            )
            conn.commit()
            return int(cur.lastrowid)

    # ---- online recall observations ---------------------------------

    def record_recall_observation(self, data: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        query = " ".join(str(data.get("query") or "").split()).strip()
        request_id = str(data.get("request_id") or "").strip()
        if not request_id:
            request_id = "obs_" + hashlib.sha256(
                f"{query}|{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:24]
        selected_before = [str(value) for value in (data.get("selected_before") or []) if str(value)]
        selected_after = [str(value) for value in (data.get("selected_after") or []) if str(value)]
        rescue_selected = [str(value) for value in (data.get("rescue_selected") or []) if str(value)]
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO recall_observations
                   (request_id,query_text,query_hash,intent,safety_triggered,safety_reason,
                    rescue_candidates,rescue_added,rescue_merged,selected_before_json,
                    selected_after_json,rescue_selected_json,outcome,feedback,note,created_ts,updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(request_id) DO UPDATE SET
                    query_text=excluded.query_text,query_hash=excluded.query_hash,
                    intent=excluded.intent,safety_triggered=excluded.safety_triggered,
                    safety_reason=excluded.safety_reason,rescue_candidates=excluded.rescue_candidates,
                    rescue_added=excluded.rescue_added,rescue_merged=excluded.rescue_merged,
                    selected_before_json=excluded.selected_before_json,
                    selected_after_json=excluded.selected_after_json,
                    rescue_selected_json=excluded.rescue_selected_json,outcome=excluded.outcome,
                    note=excluded.note,updated_ts=excluded.updated_ts""",
                (
                    request_id, query, hashlib.sha256(query.encode("utf-8")).hexdigest(),
                    str(data.get("intent") or ""), 1 if data.get("safety_triggered") else 0,
                    str(data.get("safety_reason") or ""), int(data.get("rescue_candidates") or 0),
                    int(data.get("rescue_added") or 0), int(data.get("rescue_merged") or 0),
                    json.dumps(selected_before, ensure_ascii=False),
                    json.dumps(selected_after, ensure_ascii=False),
                    json.dumps(rescue_selected, ensure_ascii=False),
                    str(data.get("outcome") or "not_triggered"), "",
                    str(data.get("note") or ""), now, now,
                ),
            )
            conn.commit()
        return {"request_id": request_id, "outcome": str(data.get("outcome") or "not_triggered")}

    def list_recall_observations(self, limit: int = 100, safety_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM recall_observations"
        params: list[Any] = []
        if safety_only:
            sql += " WHERE safety_triggered=1"
        sql += " ORDER BY created_ts DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        rows = self._get_conn().execute(sql, params).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            for key in ("selected_before_json", "selected_after_json", "rescue_selected_json"):
                item[key.removesuffix("_json")] = json_list(item.pop(key, "[]"))
            item["safety_triggered"] = bool(item.get("safety_triggered"))
            output.append(item)
        return output

    def get_recall_observation(self, request_id: str) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT * FROM recall_observations WHERE request_id=?",
            (str(request_id or ""),),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        for key in ("selected_before_json", "selected_after_json", "rescue_selected_json"):
            item[key.removesuffix("_json")] = json_list(item.pop(key, "[]"))
        item["safety_triggered"] = bool(item.get("safety_triggered"))
        return item

    def recall_observation_summary(self) -> dict[str, Any]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(safety_triggered) triggered,
                      SUM(CASE WHEN outcome='helped' THEN 1 ELSE 0 END) helped,
                      SUM(CASE WHEN outcome='no_change' THEN 1 ELSE 0 END) no_change,
                      SUM(CASE WHEN outcome='no_selection' THEN 1 ELSE 0 END) no_selection,
                      SUM(CASE WHEN feedback='useful' THEN 1 ELSE 0 END) useful,
                      SUM(CASE WHEN feedback='wrong' THEN 1 ELSE 0 END) wrong
                 FROM recall_observations"""
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def set_recall_observation_feedback(self, request_id: str, feedback: str) -> bool:
        value = str(feedback or "").strip()
        if value not in {"", "useful", "wrong"}:
            raise ValueError("feedback must be useful, wrong, or empty")
        with self._lock:
            cur = self._get_conn().execute(
                "UPDATE recall_observations SET feedback=?,updated_ts=? WHERE request_id=?",
                (value, time.time(), str(request_id or "")),
            )
            self._get_conn().commit()
            return bool(cur.rowcount)

    # ---- stats --------------------------------------------------------

    def counts(self) -> dict[str, Any]:
        conn = self._get_conn()
        total = int(conn.execute("SELECT COUNT(*) AS n FROM episodes WHERE active=1").fetchone()["n"])
        grounded = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1 AND evidence_quality='source_grounded'"
        ).fetchone()["n"])
        mixed = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1 AND evidence_quality='mixed_user_edited'"
        ).fetchone()["n"])
        diary = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1 AND evidence_quality='diary_derived'"
        ).fetchone()["n"])
        evidence = int(conn.execute("SELECT COUNT(*) AS n FROM episode_evidence").fetchone()["n"])
        turn_links = int(conn.execute("SELECT COUNT (*) AS n FROM episode_turn_links").fetchone()["n"])
        batch_linked = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1 AND source_batch_id IS NOT NULL AND source_batch_id!=''"
        ).fetchone()["n"])
        recoverable = int(conn.execute(
            """SELECT COUNT(DISTINCT e.episode_id) AS n
               FROM episodes e
               JOIN episode_turn_links l ON l.episode_id=e.episode_id
               JOIN source_turns s ON s.batch_id=l.batch_id AND s.turn_index=l.turn_index
               WHERE e.active=1"""
        ).fetchone()["n"])
        missing_embeddings = int(conn.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE active=1 AND embedding IS NULL"
        ).fetchone()["n"])
        rollbacks = int(conn.execute("SELECT COUNT(*) AS n FROM diary_rollbacks").fetchone()["n"])
        eval_cases = int(conn.execute(
            "SELECT COUNT(*) AS n FROM recall_eval_cases WHERE enabled=1").fetchone()["n"])
        observations = int(conn.execute("SELECT COUNT(*) AS n FROM recall_observations").fetchone()["n"])
        return {
            "episodes": total, "source_grounded": grounded, "mixed_user_edited": mixed,
            "diary_derived": diary, "evidence": evidence, "exact_turn_links": turn_links,
            "batch_linked_episodes": batch_linked, "recoverable_episodes": recoverable,
            "missing_embeddings": missing_embeddings,
            "rollbacks": rollbacks, "recall_eval_cases": eval_cases,
            "recall_observations": observations,
        }
