"""ArchiveGuard (4.6.0-test2 / 8.3.A + 8.3.B + 8.3.E).

Centralizes everything that keeps the lossless archive lossless:
database identity (uuid, canonical path, path-conflict detection), pre-migration
SQLite snapshots via the Backup API, restore, and startup self-check that
compares full record counts against the previous run.

It is a thin orchestrator around `sqlite3.Connection` and the live
`EpisodicStore` whose `_connect()` it borrows. It also owns the
plugin-side concern of resolving/migrating the db file location to AstrBot's
stable plugin_data directory.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ArchiveGuard:
    """Identity, snapshots and startup self-check for the source archive db."""

    SNAPSHOT_KEEP_DEFAULT = 3

    def __init__(
        self,
        db_path: str,
        get_conn: Callable[[], sqlite3.Connection],
        lock: threading.RLock,
        snapshot_keep: int = SNAPSHOT_KEEP_DEFAULT,
    ):
        self.db_path = str(db_path)
        self._get_conn = get_conn
        self._lock = lock
        self.snapshot_keep = max(1, int(snapshot_keep or self.SNAPSHOT_KEEP_DEFAULT))
        self._upgrade_snapshotted = False

    # ---- meta helpers (own copy; VectorGeneration also has its own) ------

    def _get(self, conn: sqlite3.Connection, key: str, default: str = "") -> str:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"] if row and row["value"] is not None else default)

    def _set(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value or "")))

    # ---- 8.3.B: schema for snapshots table -----------------------------

    def init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS db_snapshots (
                file_name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                schema_version TEXT DEFAULT '',
                db_uuid TEXT DEFAULT '',
                counts_json TEXT DEFAULT '{}',
                digest TEXT DEFAULT '',
                created_ts REAL NOT NULL
            )"""
        )

    # ---- 8.3.A: identity ------------------------------------------------

    def ensure_identity(self, conn: sqlite3.Connection, schema_version: int) -> None:
        db_uuid = self._get(conn, "database_uuid", "")
        if not db_uuid:
            db_uuid = str(uuid.uuid4())
            self._set(conn, "database_uuid", db_uuid)
            self._set(conn, "created_version", str(schema_version))
        canonical = self._get(conn, "canonical_db_path", "")
        try:
            resolved = str(Path(self.db_path).resolve())
        except Exception:
            resolved = self.db_path
        if not canonical:
            self._set(conn, "canonical_db_path", resolved)
        elif canonical != resolved:
            # Same physical db opened from another path → record conflict,
            # never silently switch / copy.
            existing = self._get(conn, "path_conflict", "")
            try:
                prior = json.loads(existing) if existing else {}
            except json.JSONDecodeError:
                prior = {}
            if not prior.get("first_seen_ts"):
                prior = {"canonical": canonical, "current": resolved,
                         "first_seen_ts": time.time()}
                self._set(conn, "path_conflict", json.dumps(prior, ensure_ascii=False))
                self._set(conn, "path_conflict_ts", str(time.time()))
            logger.warning(
                "[memory][archive] db path mismatch (uuid=%s): canonical=%s current=%s",
                db_uuid, canonical, resolved,
            )
        self._set(conn, "last_opened_ts", str(time.time()))

    def identity_snapshot(self) -> dict[str, Any]:
        conn = self._get_conn()
        file_size = 0
        try:
            file_size = Path(self.db_path).stat().st_size
        except Exception:
            pass
        return {
            "database_uuid": self._get(conn, "database_uuid", ""),
            "db_path": self.db_path,
            "canonical_db_path": self._get(conn, "canonical_db_path", ""),
            "schema_version": self._get(conn, "schema_version", ""),
            "db_file_size": file_size,
            "path_conflict": self._get(conn, "path_conflict", ""),
            "last_opened_ts": self._get(conn, "last_opened_ts", ""),
        }

    # ---- 8.3.B: snapshots ------------------------------------------------

    def capture_pre_migration(self, conn: sqlite3.Connection, schema_version: int) -> dict[str, Any] | None:
        """Back up an older database before any v5 Repository DDL runs.

        Registration in ``db_snapshots`` is deferred until that table has been
        initialized; the file itself is created from the untouched old schema.
        """
        meta_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not meta_exists:
            return None
        current = self._get(conn, "schema_version", "")
        if current == str(schema_version):
            return None
        snap_dir = self.snapshot_dir()
        snap_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns() % 1_000_000_000:09d}"
        file_name = f"episodic_memory.snap_{stamp}_pre_migration.db"
        dest = snap_dir / file_name
        dst = sqlite3.connect(str(dest))
        try:
            conn.backup(dst)
        finally:
            dst.close()
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:32]
        return {
            "file_name": file_name,
            "kind": "pre_migration",
            "schema_version": current,
            "db_uuid": self._get(conn, "database_uuid", ""),
            "counts": {},
            "digest": digest,
            "created_ts": time.time(),
        }

    def register_pre_migration(self, conn: sqlite3.Connection, snapshot: dict[str, Any] | None,
                               schema_version: int) -> bool:
        if not snapshot:
            return False
        conn.execute(
            """INSERT OR REPLACE INTO db_snapshots
               (file_name,kind,schema_version,db_uuid,counts_json,digest,created_ts)
               VALUES (?,?,?,?,?,?,?)""",
            (snapshot["file_name"], "pre_migration", snapshot.get("schema_version", ""),
             snapshot.get("db_uuid", ""), json.dumps(snapshot.get("counts", {})),
             snapshot.get("digest", ""), float(snapshot.get("created_ts") or time.time())),
        )
        self._set(conn, "upgrade_snapshots_done", str(schema_version))
        self._upgrade_snapshotted = True
        logger.info("[memory][archive] registered pre-DDL snapshot %s", snapshot["file_name"])
        return True

    def snapshot_dir(self) -> Path:
        return Path(self.db_path).parent / "snapshots"

    def take_snapshot(self, conn: sqlite3.Connection, kind: str = "manual") -> dict[str, Any]:
        """Consistency snapshot via SQLite Backup API (handles WAL/SHM)."""
        snap_dir = self.snapshot_dir()
        snap_dir.mkdir(parents=True, exist_ok=True)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        ts = time.strftime("%Y%m%d_%H%M%S")
        file_name = f"episodic_memory.snap_{ts}_{kind}.db"
        dest = snap_dir / file_name
        src = sqlite3.connect(self.db_path)
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        digest = ""
        try:
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:32]
        except Exception:
            pass
        db_uuid = self._get(conn, "database_uuid", "")
        counts = self._full_counts(conn)
        conn.execute(
            """INSERT OR REPLACE INTO db_snapshots
               (file_name,kind,schema_version,db_uuid,counts_json,digest,created_ts)
               VALUES (?,?,?,?,?,?,?)""",
            (file_name, str(kind), str(counts.get("schema_version") or ""),
             db_uuid, json.dumps(counts, ensure_ascii=False), digest, time.time()),
        )
        # Prune oldest per kind, keep snapshot_keep
        rows = conn.execute(
            "SELECT file_name FROM db_snapshots WHERE kind=? ORDER BY created_ts DESC",
            (str(kind),),
        ).fetchall()
        for row in rows[int(self.snapshot_keep):]:
            try:
                (snap_dir / str(row["file_name"])).unlink(missing_ok=True)
            except Exception:
                pass
            conn.execute("DELETE FROM db_snapshots WHERE file_name=?", (str(row["file_name"]),))
        conn.commit()
        logger.info("[memory][archive] snapshot %s kind=%s counts=%s",
                    file_name, kind, {k: counts[k] for k in ("batches", "turns", "episodes", "turn_links")})
        return {"file_name": file_name, "kind": kind, "digest": digest, "counts": counts}

    def take_migration_snapshot(self, conn: sqlite3.Connection, schema_version: int) -> bool:
        """Idempotent pre-migration snapshot when schema bumps."""
        if self._upgrade_snapshotted:
            return False
        done = self._get(conn, "upgrade_snapshots_done", "")
        if done == str(schema_version):
            self._upgrade_snapshotted = True
            return False
        try:
            self.take_snapshot(conn, kind="pre_migration")
            self._set(conn, "upgrade_snapshots_done", str(schema_version))
            self._upgrade_snapshotted = True
            conn.commit()
            return True
        except Exception as exc:
            logger.warning("[memory][archive] pre-migration snapshot failed (continue): %s", exc)
            return False

    def list_snapshots(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._get_conn().execute(
            "SELECT * FROM db_snapshots ORDER BY created_ts DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["counts"] = json.loads(item.pop("counts_json") or "{}")
            except json.JSONDecodeError:
                item["counts"] = {}
            out.append(item)
        return out

    def restore_snapshot(self, file_name: str, live_store_close: Callable[[], None] | None = None,
                         live_store_reopen: Callable[[], None] | None = None) -> dict[str, Any]:
        """Pre-restore snapshot then overwrite db file. Caller closes/reopens."""
        file_name = str(file_name or "").strip()
        if not file_name or "/" in file_name or "\\" in file_name or ".." in file_name:
            return {"restored": False, "reason": "invalid_name"}
        snap_path = self.snapshot_dir() / file_name
        if not snap_path.exists():
            return {"restored": False, "reason": "missing"}
        with self._lock:
            try:
                self.take_snapshot(self._get_conn(), kind="pre_restore")
            except Exception as exc:
                logger.warning("[memory][archive] pre-restore snapshot failed: %s", exc)
            live_path = Path(self.db_path)
            staged = live_path.with_suffix(live_path.suffix + ".restore.tmp")
            backup = live_path.with_suffix(live_path.suffix + ".restore.bak")
            staged.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
            # Stage and validate before touching the live database.
            shutil.copy2(snap_path, staged)
            check = sqlite3.connect(str(staged))
            try:
                result = check.execute("PRAGMA quick_check").fetchone()
                if not result or str(result[0]).lower() != "ok":
                    raise RuntimeError("snapshot quick_check failed")
            finally:
                check.close()
            closed = False
            restored = False
            try:
                if live_store_close is not None:
                    live_store_close()
                    closed = True
                for suffix in ("-wal", "-shm"):
                    Path(self.db_path + suffix).unlink(missing_ok=True)
                if live_path.exists():
                    os.replace(live_path, backup)
                os.replace(staged, live_path)
                restored = True
            except Exception:
                if backup.exists():
                    if live_path.exists():
                        live_path.unlink(missing_ok=True)
                    os.replace(backup, live_path)
                raise
            finally:
                staged.unlink(missing_ok=True)
                if restored:
                    backup.unlink(missing_ok=True)
                if closed and live_store_reopen is not None:
                    live_store_reopen()
        # refresh self-check baseline after a successful reopen
        self.record_full_counts()
        logger.warning("[memory][archive] restored snapshot %s", file_name)
        return {"restored": True, "file_name": file_name}

    # ---- 8.3.E: startup self-check -------------------------------------

    def _full_counts(self, conn: sqlite3.Connection) -> dict[str, Any]:
        return {
            "schema_version": self._get(conn, "schema_version", ""),
            "batches": int(conn.execute("SELECT COUNT(*) AS n FROM source_batches").fetchone()["n"]),
            "turns": int(conn.execute("SELECT COUNT(*) AS n FROM source_turns").fetchone()["n"]),
            "episodes": int(conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]),
            "turn_links": int(conn.execute("SELECT COUNT(*) AS n FROM episode_turn_links").fetchone()["n"]),
            "ts": time.time(),
        }

    def record_full_counts(self) -> dict[str, Any]:
        counts = self._full_counts(self._get_conn())
        with self._lock:
            conn = self._get_conn()
            self._set(conn, "last_full_counts", json.dumps(counts, ensure_ascii=False))
            conn.commit()
        return counts

    def startup_self_check(self) -> dict[str, Any]:
        conn = self._get_conn()
        current = self._full_counts(conn)
        previous_raw = self._get(conn, "last_full_counts_before_check", "")
        previous: dict[str, Any] = {}
        try:
            previous = json.loads(previous_raw) if previous_raw else {}
        except json.JSONDecodeError:
            previous = {}
        issues: list[str] = []
        for key in ("batches", "turns", "turn_links", "episodes"):
            if key in previous and current.get(key, 0) < int(previous.get(key) or 0):
                issues.append(f"{key}: {previous.get(key)} → {current.get(key)}")
        if issues:
            self._set(conn, "startup_issue", "archive counts dropped: " + "; ".join(issues))
            logger.warning("[memory][archive] startup self-check counts dropped: %s",
                           "; ".join(issues))
        else:
            self._set(conn, "startup_issue", "")
        # Set baseline for the *next* startup
        self._set(conn, "last_full_counts_before_check", json.dumps(current, ensure_ascii=False))
        conn.commit()
        return {"current": current, "previous": previous, "issues": issues}

    def startup_diagnosis(self) -> dict[str, Any]:
        """Single payload for plugin startup log + identity sidecar."""
        identity = self.identity_snapshot()
        check = self.startup_self_check()
        return {
            "identity": identity,
            "self_check": check,
            "startup_issue": self._get(self._get_conn(), "startup_issue", ""),
        }


# ---- plugin-side path resolution & migration (8.3.A) -------------------

def resolve_plugin_data_db(filename: str) -> str:
    """Resolve db path to the AstrBot stable plugin_data directory.

    Falls back to the legacy relative path under ./data/<plugin>/ when
    StarTools is unavailable (e.g. in tests using object.__new__(plugin)).
    """
    try:
        from astrbot.core.star.star_tools import StarTools

        data_dir = StarTools.get_data_dir("astrbot_plugin_memos_memory")
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / filename)
    except Exception:
        # test or unit fallback
        legacy = Path("./data/astrbot_plugin_memos_memory") / filename
        legacy.parent.mkdir(parents=True, exist_ok=True)
        return str(legacy)


def copy_db_files(src: Path, dst: Path) -> None:
    """Copy a SQLite db plus its -wal/-shm siblings, preserving the source."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            shutil.copy2(s, Path(str(dst) + suffix))


def _probe_episode_db(path: Path) -> dict[str, Any]:
    """Read enough identity/count data to distinguish an archive from an empty db."""
    result: dict[str, Any] = {
        "path": str(path.resolve()), "exists": path.exists(), "database_uuid": "",
        "source_turns": 0, "source_batches": 0, "episodes": 0,
    }
    if not path.exists() or path.stat().st_size <= 0:
        return result
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            tables = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "meta" in tables:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='database_uuid'"
                ).fetchone()
                result["database_uuid"] = str(row[0]) if row else ""
            for table in ("source_turns", "source_batches", "episodes"):
                if table in tables:
                    result[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        result["probe_error"] = str(exc)
    return result


def _archive_population(probe: dict[str, Any]) -> int:
    return int(probe.get("source_turns") or 0) + int(probe.get("episodes") or 0)


def _location_sidecar(canonical: Path) -> Path:
    return canonical.parent / "episodic_db_location.json"


def record_episode_db_location(db_path: str) -> None:
    """Persist the selected archive path outside the archive itself.

    This lets the next plugin build recover the last known populated source even
    when a relative setting resolves differently after AstrBot is relocated.
    """
    path = Path(db_path).expanduser().resolve()
    canonical = Path(resolve_plugin_data_db(path.name)).expanduser().resolve()
    sidecar = _location_sidecar(canonical)
    payload = _probe_episode_db(path)
    payload["selected_path"] = str(path)
    payload["recorded_at"] = int(time.time())
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        temp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, sidecar)
    except Exception as exc:
        logger.warning("[memory][archive] failed to record archive location: %s", exc)


def migrate_episode_db_location(configured_path: str) -> str:
    """Choose the intended archive without silently switching to an empty db."""
    configured = Path(configured_path).expanduser()
    # An absolute setting is an explicit operator choice, including before the
    # database is created. Never rewrite it merely because it does not exist yet.
    if configured.is_absolute():
        return str(configured.resolve())
    canonical_abs = resolve_plugin_data_db(configured.name)
    canonical = Path(canonical_abs).expanduser().resolve()
    configured_abs = configured.resolve()
    if configured_abs == canonical:
        return str(canonical)

    canonical_probe = _probe_episode_db(canonical)
    configured_probe = _probe_episode_db(configured_abs)

    # A configured legacy file is stronger evidence than a same-named empty
    # canonical file. If both populated files have different UUIDs, preserve the
    # configured source and report the conflict instead of merging or overwriting.
    if configured_abs.exists():
        configured_pop = _archive_population(configured_probe)
        canonical_pop = _archive_population(canonical_probe)
        configured_uuid = str(configured_probe.get("database_uuid") or "")
        canonical_uuid = str(canonical_probe.get("database_uuid") or "")
        if canonical.exists() and configured_pop > 0 and (
            canonical_pop == 0 or (configured_uuid and canonical_uuid and configured_uuid != canonical_uuid)
        ):
            logger.error(
                "[memory][archive] database path conflict; keeping configured source=%s "
                "(turns=%s episodes=%s uuid=%s), canonical=%s (turns=%s episodes=%s uuid=%s)",
                configured_abs, configured_probe["source_turns"], configured_probe["episodes"],
                configured_uuid[:12], canonical, canonical_probe["source_turns"],
                canonical_probe["episodes"], canonical_uuid[:12],
            )
            return str(configured_abs)
        if canonical.exists():
            return str(canonical)
        try:
            copy_db_files(configured_abs, canonical)
            logger.info("[memory][archive] relocated legacy db %s -> %s", configured_abs, canonical)
            return str(canonical)
        except Exception as exc:
            logger.error("[memory][archive] relocation failed; keeping source %s: %s", configured_abs, exc)
            return str(configured_abs)

    # The external sidecar survives plugin replacement and identifies the last
    # archive actually opened by the plugin.
    sidecar = _location_sidecar(canonical)
    if sidecar.exists():
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            remembered = Path(str(payload.get("selected_path") or "")).expanduser().resolve()
            remembered_probe = _probe_episode_db(remembered)
            if remembered.exists() and _archive_population(remembered_probe) > _archive_population(canonical_probe):
                logger.warning("[memory][archive] recovered populated archive path from sidecar: %s", remembered)
                return str(remembered)
        except Exception as exc:
            logger.warning("[memory][archive] ignored invalid location sidecar %s: %s", sidecar, exc)

    # No populated alternative was found; use/create the stable canonical path.
    return str(canonical)
