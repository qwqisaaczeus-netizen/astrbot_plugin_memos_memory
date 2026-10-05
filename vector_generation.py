"""Shadow-generation state machine for vector tables (4.6.0-test2 / 8.3.C).

A `VectorGeneration` instance owns the model/dim → generation-id mapping, the
active/pending table names, validation, atomic switch and rollback. Higher
layers never build `vec_*` table names themselves; they ask
`gen.tables_in_use()` or `gen.table_name(kind)`.

State diagram::

    none ──prepare_active(model,dim)──▶ active
    active ──prepare_shadow(new_model,new_dim)──▶ preparing   (active still serves)
    preparing ──switch_after_validation()──▶ active'  (prev = old active)
    active' ──rollback()──▶ active   (prev cleared)
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from enum import Enum
from typing import Any, Callable

from .store_utils import serialize_f32

logger = logging.getLogger(__name__)


class GenState(str, Enum):
    NONE = "none"
    ACTIVE = "active"
    PREPARING = "preparing"
    """active still serves; a pending generation is being filled in the background."""

    def __str__(self) -> str:
        return self.value


def _gen_id(model_id: str, dim: int) -> str:
    return hashlib.sha256(f"{model_id or 'unknown'}|{int(dim or 0)}".encode("utf-8")).hexdigest()[:12]


_TABLE_PREFIX = {
    "source": "vec_source_turns",
    "card": "vec_episode_cards",
    "chunk": "vec_source_chunks",
}


def _table_name(kind: str, gen: str) -> str:
    prefix = _TABLE_PREFIX.get(kind, "")
    return f"{prefix}_g{gen}" if prefix and gen else prefix


class VectorGeneration:
    """Owns generation identity, table names and the active ↔ pending swap.

    The instance is __stateless w.r.t. the connection: every call takes the
    live `sqlite3.Connection` and re-reads/writes meta rows inside the same
    lock the caller already holds. A thin read-cache (`_active_cache`,
    `_pending_cache`) avoids a round-trip when callers repeat the same lookup
    inside one transaction.
    """

    KEYS = ("active_generation", "pending_generation", "prev_generation",
            "prev_emb_dim", "prev_emb_model_id", "migration_status",
            "emb_dim", "emb_model_id")

    def __init__(self, vec_ok: bool, get_conn: Callable[[], sqlite3.Connection]):
        self._vec_ok = bool(vec_ok)
        self._get_conn = get_conn
        self._active_cache: str | None = None
        self._pending_cache: str | None = None

    # ---- meta helpers --------------------------------------------------

    def _get(self, conn: sqlite3.Connection, key: str, default: str = "") -> str:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"] if row and row["value"] is not None else default)

    def _set(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value or "")))

    # ---- public state getters -----------------------------------------

    def current_state(self) -> GenState:
        con = self._get_conn()
        active = self._get(con, "active_generation", "")
        pending = self._get(con, "pending_generation", "")
        if active and pending:
            return GenState.PREPARING
        if active:
            return GenState.ACTIVE
        return GenState.NONE

    def active_generation(self) -> str:
        if self._active_cache is None:
            self._active_cache = self._get(self._get_conn(), "active_generation", "")
        return self._active_cache

    def pending_generation(self) -> str:
        if self._pending_cache is None:
            self._pending_cache = self._get(self._get_conn(), "pending_generation", "")
        return self._pending_cache

    def runtime_generation(self, model_id: str, dim: int) -> str:
        return _gen_id(model_id or "unknown", int(dim or 0)) if dim else ""

    def table_name(self, kind: str) -> str:
        return _table_name(kind, self.active_generation())

    @staticmethod
    def table_name_for(kind: str, generation: str) -> str:
        return _table_name(kind, generation)

    # ---- prepare / switch / rollback ----------------------------------

    def prepare_active(self, model_id: str, dim: int) -> str:
        """First-time bootstrap (or legacy `vec_*` upgrade): record the active
        generation from the runtime model/dim and ensure its tables exist.
        Returns the active generation id (empty when dim is 0).
        """
        if not dim:
            return ""
        gen = self.runtime_generation(model_id, dim)
        con = self._get_conn()
        active = self._get(con, "active_generation", "")
        if not active:
            self._migrate_legacy_tables(con, gen)
            self._set(con, "active_generation", gen)
            self._set(con, "emb_dim", str(dim))
            self._set(con, "emb_model_id", model_id or "unknown")
            self._set(con, "migration_status", "ready")
            self._active_cache = gen
        elif active != gen and not self._get(con, "pending_generation", ""):
            # same-DB model swap → schedule shadow migration, keep active serving
            self._set(con, "pending_generation", gen)
            self._set(con, "migration_status", "preparing")
            self._pending_cache = gen
        elif active != gen:
            # pending already recorded for this gen → just make sure tables exist
            self._pending_cache = gen
        self._ensure_tables(con, gen, dim)
        # Adopt legacy single-blob vectors into the active version table once.
        # This is idempotent and lets old databases participate in rollback.
        active_now = self._get(con, "active_generation", "")
        if active_now:
            self._backfill_legacy_blobs(con, active_now)
        con.commit()
        return self.active_generation()

    def _ensure_tables(self, conn: sqlite3.Connection, gen: str, dim: int) -> bool:
        if not self._vec_ok or not gen or not dim:
            return False
        ok = True
        for kind in _TABLE_PREFIX:
            try:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_table_name(kind, gen)} "
                    f"USING vec0(embedding float[{dim}] distance_metric=cosine)"
                )
            except Exception as exc:  # pragma: no cover - vec0 failure is recoverable
                logger.warning("[memory][gen] vec %s table failed for gen %s: %s", kind, gen[:8], exc)
                ok = False
        return ok

    def _migrate_legacy_tables(self, conn: sqlite3.Connection, gen: str) -> None:
        """On first active bootstrap, adopt any old no-suffix `vec_*` table
        rows into the active generation, then drop the legacy tables. Only runs
        when sqlite-vec is actually loaded (otherwise the tables don't exist
        and this is a no-op)."""
        if not self._vec_ok:
            return
        for kind in _TABLE_PREFIX:
            legacy = _TABLE_PREFIX[kind]
            try:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (legacy,)
                ).fetchone()
            except Exception:
                exists = None
            if not exists:
                continue
            target = _table_name(kind, gen)
            try:
                rows = conn.execute(f"SELECT rowid, embedding FROM {legacy}").fetchall()
                for row in rows:
                    try:
                        conn.execute(
                            f"INSERT OR REPLACE INTO {target}(rowid,embedding) VALUES (?,?)",
                            (int(row["rowid"]), row["embedding"]),
                        )
                    except Exception:
                        continue
                conn.execute(f"DROP TABLE {legacy}")
                logger.info("[memory][archive] legacy vec table %s adopted into gen %s (%d rows)",
                            legacy, gen[:8], len(rows))
            except Exception as exc:
                logger.warning("[memory][gen] legacy %s migrate failed (left in place): %s", legacy, exc)

    def smoke_test(self, gen: str, dim: int) -> bool:
        """Validate version coverage first, then sqlite-vec readability.

        For an empty archive a generation is valid without rows. Otherwise all
        existing source/card/chunk rows must have a vector for `gen` before the
        atomic switch is allowed.
        """
        if not gen or not dim:
            return False
        con = self._get_conn()
        try:
            for table, kind in (
                ("source_turns", "source"),
                ("episodes", "card"),
                ("source_turn_chunks", "chunk"),
            ):
                total = int(con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] or 0)
                ready = int(con.execute(
                    "SELECT COUNT(*) AS n FROM embedding_versions WHERE kind=? AND generation=?",
                    (kind, gen),
                ).fetchone()["n"] or 0)
                if ready < total:
                    logger.warning(
                        "[memory][gen] validation incomplete kind=%s gen=%s ready=%d total=%d",
                        kind, gen[:8], ready, total,
                    )
                    return False
            if not self._vec_ok:
                return True
            total_source = int(con.execute("SELECT COUNT(*) AS n FROM source_turns").fetchone()["n"] or 0)
            if total_source:
                probe = [0.0] * dim
                con.execute(
                    f"SELECT rowid FROM {_table_name('source', gen)} WHERE embedding MATCH ? AND k=1",
                    (serialize_f32(probe),),
                ).fetchone()
            return True
        except Exception as exc:
            logger.warning("[memory][gen] smoke_test failed for gen %s: %s", gen[:8], exc)
            return False

    def switch_after_validation(
        self,
        dim: int,
        model_id: str,
        take_snapshot: Callable[[], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomic active←pending switch. Caller provides `take_snapshot` for a
        `pre_switch` backup. Validation failures never mutate active state."""
        con = self._get_conn()
        pending = self._get(con, "pending_generation", "")
        if not pending:
            return {"switched": False, "reason": "no_pending"}
        if not self.smoke_test(pending, dim):
            return {"switched": False, "reason": "validation_failed", "pending": pending[:8]}
        prev = self._get(con, "active_generation", "")
        if take_snapshot is not None:
            try:
                take_snapshot()
            except Exception as exc:  # pragma: no cover - snapshot failure non-fatal
                logger.warning("[memory][archive] pre-switch snapshot failed: %s", exc)
        self._set(con, "prev_generation", prev)
        self._set(con, "prev_emb_dim", self._get(con, "emb_dim", "0"))
        self._set(con, "prev_emb_model_id", self._get(con, "emb_model_id", ""))
        self._set(con, "active_generation", pending)
        self._set(con, "pending_generation", "")
        self._set(con, "emb_dim", str(dim))
        self._set(con, "emb_model_id", model_id or "unknown")
        self._set(con, "migration_status", "ready")
        self._active_cache = pending
        self._pending_cache = ""
        # Ensure active tables exist and materialize the selected generation into
        # compatibility blob columns. All generations remain in embedding_versions.
        self._ensure_tables(con, pending, dim)
        self._materialize_generation(con, pending)
        self._cleanup_other_tables(con, keep={pending, prev})
        con.commit()
        logger.info("[memory][archive] gen switch %s → %s (prev kept %s)",
                   prev[:8] if prev else "none", pending[:8], prev[:8] if prev else "none")
        return {"switched": True, "from": prev, "to": pending, "dim": dim}

    def rollback(self) -> dict[str, Any]:
        con = self._get_conn()
        prev = self._get(con, "prev_generation", "")
        if not prev:
            return {"rolled_back": False, "reason": "no_prev"}
        current = self._get(con, "active_generation", "")
        prev_dim = int(self._get(con, "prev_emb_dim", "0") or 0)
        prev_model = self._get(con, "prev_emb_model_id", "") or "unknown"
        self._set(con, "active_generation", prev)
        self._set(con, "prev_generation", "")
        self._set(con, "pending_generation", "")
        self._set(con, "emb_dim", str(prev_dim))
        self._set(con, "emb_model_id", prev_model)
        self._set(con, "migration_status", "rolled_back")
        self._active_cache = prev
        self._pending_cache = ""
        if prev_dim:
            self._ensure_tables(con, prev, prev_dim)
        self._materialize_generation(con, prev)
        self._cleanup_other_tables(con, keep={prev, current})
        con.commit()
        logger.warning("[memory][archive] gen rollback %s → %s", current[:8], prev[:8])
        return {"rolled_back": True, "from": current, "to": prev}

    def _backfill_legacy_blobs(self, conn: sqlite3.Connection, generation: str) -> None:
        for table, kind in (
            ("episodes", "card"),
            ("source_turns", "source"),
            ("source_turn_chunks", "chunk"),
        ):
            try:
                conn.execute(
                    f"""INSERT OR IGNORE INTO embedding_versions
                        (kind,row_id,generation,embedding,created_ts)
                        SELECT ?,id,?,embedding,strftime('%s','now') FROM {table}
                        WHERE embedding IS NOT NULL""",
                    (kind, generation),
                )
            except sqlite3.OperationalError:
                # source_turn_chunks may not exist during an older schema's
                # first metadata pass; Repository schema creation retries later.
                continue

    def _materialize_generation(self, conn: sqlite3.Connection, generation: str) -> None:
        """Project one versioned generation into legacy blob columns.

        The version table remains authoritative; this projection keeps old
        callers and diagnostics working while Python-cosine can switch/rollback
        without losing either generation.
        """
        for table, kind in (
            ("episodes", "card"),
            ("source_turns", "source"),
            ("source_turn_chunks", "chunk"),
        ):
            conn.execute(f"UPDATE {table} SET embedding=NULL,embedding_generation='' ")
            conn.execute(
                f"""UPDATE {table} SET
                    embedding=(SELECT ev.embedding FROM embedding_versions ev
                               WHERE ev.kind=? AND ev.row_id={table}.id AND ev.generation=?),
                    embedding_generation=?
                    WHERE EXISTS (SELECT 1 FROM embedding_versions ev
                                  WHERE ev.kind=? AND ev.row_id={table}.id AND ev.generation=?)""",
                (kind, generation, generation, kind, generation),
            )

    def _cleanup_other_tables(self, conn: sqlite3.Connection, keep: set[str]) -> int:
        dropped = 0
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vec\\_%' ESCAPE '\\'"
            ).fetchall()
        except Exception:
            rows = []
        for row in rows:
            name = str(row["name"])
            gen = ""
            for prefix in _TABLE_PREFIX.values():
                if name.startswith(prefix + "_g"):
                    gen = name[len(prefix) + 2:]
                    break
            if gen and gen not in keep:
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {name}")
                    dropped += 1
                except Exception:
                    pass
        return dropped

    # ---- diagnosis ----------------------------------------------------

    def snapshot_status(self) -> dict[str, Any]:
        con = self._get_conn()
        return {
            "active_generation": self._get(con, "active_generation", ""),
            "pending_generation": self._get(con, "pending_generation", ""),
            "prev_generation": self._get(con, "prev_generation", ""),
            "active_state": str(self.current_state()),
            "migration_status": self._get(con, "migration_status", ""),
        }
