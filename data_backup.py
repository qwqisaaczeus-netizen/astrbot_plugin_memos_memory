"""Periodic, secret-free backups and restart-safe restore management."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any


_JSON_BACKUP_FILES = (
    "xinchao_state.json",
    "xinchao_settings.json",
    "runtime_telemetry.json",
    "episodic_db_location.json",
)
_JSON_RESTORE_FILES = {
    "xinchao_state.json",
    "xinchao_settings.json",
}


class DataBackupManager:
    def __init__(
        self,
        *,
        backup_dir: str,
        vec_db_path: str,
        episodic_db_path: str,
        plugin_version: str,
        interval_days: int = 14,
        keep: int = 6,
        enabled: bool = True,
    ) -> None:
        self.backup_dir = Path(backup_dir).expanduser().resolve()
        self.vec_db_path = Path(vec_db_path).expanduser().resolve()
        self.episodic_db_path = Path(episodic_db_path).expanduser().resolve()
        self.plugin_version = str(plugin_version)
        self.interval_days = max(1, min(365, int(interval_days)))
        self.keep = max(1, min(52, int(keep)))
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._last_result: dict[str, Any] = {}
        self._pending_path = self.backup_dir / "pending_restore.json"
        self._last_restore_path = self.backup_dir / "last_restore.json"

    def _archives(self) -> list[Path]:
        if not self.backup_dir.exists():
            return []
        found: list[tuple[float, Path]] = []
        for path in self.backup_dir.glob("memory_backup_*.zip"):
            try:
                found.append((float(path.stat().st_mtime), path))
            except OSError:
                continue
        return [item[1] for item in sorted(found, key=lambda item: item[0], reverse=True)]

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        os.replace(temp_path, path)

    def pending_restore(self) -> dict[str, Any]:
        pending = self._read_json(self._pending_path)
        if pending:
            pending["exists"] = True
        return pending

    def last_restore(self) -> dict[str, Any]:
        return self._read_json(self._last_restore_path)

    def status(self) -> dict[str, Any]:
        archives = self._archives()
        latest = archives[0] if archives else None
        last_ts = float(latest.stat().st_mtime) if latest else 0.0
        interval_seconds = self.interval_days * 86400
        return {
            "enabled": self.enabled,
            "interval_days": self.interval_days,
            "keep": self.keep,
            "backup_dir": str(self.backup_dir),
            "count": len(archives),
            "latest_file": latest.name if latest else "",
            "latest_ts": last_ts,
            "latest_bytes": int(latest.stat().st_size) if latest else 0,
            "next_due_ts": last_ts + interval_seconds if last_ts else 0.0,
            "due": not last_ts or time.time() >= last_ts + interval_seconds,
            "pending_restore": self.pending_restore(),
            "last_restore": self.last_restore(),
            "last_result": dict(self._last_result),
        }

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()

    @staticmethod
    def _backup_sqlite(source_path: Path, target_path: Path) -> dict[str, Any]:
        source = sqlite3.connect(str(source_path), timeout=15, check_same_thread=False)
        target = sqlite3.connect(str(target_path), timeout=15, check_same_thread=False)
        try:
            source.backup(target, pages=256, sleep=0.02)
            row = target.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise RuntimeError(f"integrity_check failed for {source_path.name}")
        finally:
            target.close()
            source.close()
        return {
            "name": source_path.name,
            "bytes": target_path.stat().st_size,
            "sha256": DataBackupManager._digest(target_path),
        }

    @staticmethod
    def _check_sqlite(path: Path) -> None:
        conn = sqlite3.connect(str(path), timeout=15)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise RuntimeError(f"integrity_check failed for {path.name}")
        finally:
            conn.close()

    @staticmethod
    def _sqlite_counts(path: Path) -> dict[str, int]:
        conn = sqlite3.connect(str(path), timeout=15)
        try:
            tables = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            queries = {
                "memories": ("chunks", "SELECT COUNT(DISTINCT memo_name) FROM chunks"),
                "pending_messages": ("pending_messages", "SELECT COUNT(*) FROM pending_messages"),
                "episodes": ("episodes", "SELECT COUNT(*) FROM episodes WHERE active=1"),
                "source_turns": ("source_turns", "SELECT COUNT(*) FROM source_turns"),
                "semantic_states": ("semantic_states", "SELECT COUNT(*) FROM semantic_states"),
                "state_versions": (
                    "semantic_state_versions", "SELECT COUNT(*) FROM semantic_state_versions"
                ),
                "eval_cases": ("recall_eval_cases", "SELECT COUNT(*) FROM recall_eval_cases"),
            }
            result: dict[str, int] = {}
            for key, (table, sql) in queries.items():
                if table in tables:
                    result[key] = int(conn.execute(sql).fetchone()[0] or 0)
            return result
        finally:
            conn.close()

    def _resolve_archive(self, file_name: str) -> Path:
        raw = str(file_name or "").strip()
        if not raw or raw != Path(raw).name or not raw.startswith("memory_backup_") or not raw.endswith(".zip"):
            raise ValueError("invalid backup file name")
        path = (self.backup_dir / raw).resolve()
        if path.parent != self.backup_dir or not path.is_file():
            raise FileNotFoundError("backup file not found")
        return path

    @staticmethod
    def _validate_zip_structure(archive: zipfile.ZipFile) -> dict[str, Any]:
        bad_name = archive.testzip()
        if bad_name:
            raise RuntimeError(f"backup ZIP integrity check failed: {bad_name}")
        names = archive.namelist()
        for name in names:
            posix = Path(name.replace("\\", "/"))
            if name.startswith(("/", "\\")) or ".." in posix.parts:
                raise RuntimeError("backup contains an unsafe path")
        if "manifest.json" not in names:
            raise RuntimeError("backup manifest is missing")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("backup manifest is invalid")
        if manifest.get("contains_config_or_tokens") is not False:
            raise RuntimeError("backup manifest secret policy is invalid")
        if not manifest.get("databases"):
            raise RuntimeError("backup archive contains no database snapshot")
        return manifest

    @classmethod
    def _validate_archive(cls, path: Path) -> dict[str, Any]:
        with zipfile.ZipFile(path, "r") as archive:
            return cls._validate_zip_structure(archive)

    def _protected_archive_names(self) -> set[str]:
        pending = self.pending_restore()
        return {
            str(value) for value in (
                pending.get("target_file"), pending.get("safety_backup")
            ) if value
        }

    def _prune(self) -> int:
        protected = self._protected_archive_names()
        removable = [path for path in self._archives() if path.name not in protected]
        keep_unprotected = max(0, self.keep - len(protected))
        removed = 0
        for path in removable[keep_unprotected:]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _create_locked(self, *, force: bool, reason: str, prune: bool = True) -> dict[str, Any]:
        current = self.status()
        if not self.enabled and not force:
            return {"created": False, "reason": "disabled", **current}
        if not force and not current["due"]:
            return {"created": False, "reason": "not_due", **current}
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        final_path = self.backup_dir / f"memory_backup_{timestamp}.zip"
        if final_path.exists():
            final_path = self.backup_dir / f"memory_backup_{timestamp}_{time.time_ns() % 1000000:06d}.zip"
        temp_zip = final_path.with_suffix(".zip.tmp")
        try:
            with tempfile.TemporaryDirectory(prefix="memory_backup_", dir=self.backup_dir) as temp_name:
                temp_dir = Path(temp_name)
                manifest: dict[str, Any] = {
                    "format": 1,
                    "plugin": "astrbot_plugin_memos_memory",
                    "plugin_version": self.plugin_version,
                    "created_ts": time.time(),
                    "reason": str(reason or "scheduled"),
                    "databases": [],
                    "json_files": [],
                    "contains_config_or_tokens": False,
                }
                db_sources: list[tuple[str, Path]] = []
                for label, path in (
                    ("memories.db", self.vec_db_path),
                    ("episodic_memory.db", self.episodic_db_path),
                ):
                    if path.exists() and path.is_file() and path not in [item[1] for item in db_sources]:
                        db_sources.append((label, path))
                staged: list[tuple[str, Path]] = []
                for label, source_path in db_sources:
                    target_path = temp_dir / label
                    info = self._backup_sqlite(source_path, target_path)
                    info["archive_name"] = label
                    manifest["databases"].append(info)
                    staged.append((label, target_path))

                data_dir = self.vec_db_path.parent
                for name in _JSON_BACKUP_FILES:
                    source_path = data_dir / name
                    if not source_path.exists() or not source_path.is_file():
                        continue
                    target_path = temp_dir / name
                    target_path.write_bytes(source_path.read_bytes())
                    manifest["json_files"].append({
                        "name": name,
                        "bytes": target_path.stat().st_size,
                        "sha256": self._digest(target_path),
                    })
                    staged.append((name, target_path))

                if not manifest["databases"]:
                    raise RuntimeError("no plugin database is available for backup")
                manifest_path = temp_dir / "manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                staged.append(("manifest.json", manifest_path))
                with zipfile.ZipFile(
                    temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                ) as archive:
                    for archive_name, source_path in staged:
                        archive.write(source_path, archive_name)
                self._validate_archive(temp_zip)
                os.replace(temp_zip, final_path)
            pruned = self._prune() if prune else 0
            result = {
                "created": True,
                "reason": reason,
                "file": final_path.name,
                "path": str(final_path),
                "bytes": final_path.stat().st_size,
                "sha256": self._digest(final_path),
                "pruned": pruned,
                "created_ts": final_path.stat().st_mtime,
            }
            self._last_result = result
            return {**result, **self.status()}
        except Exception as exc:
            try:
                temp_zip.unlink(missing_ok=True)
            except OSError:
                pass
            result = {
                "created": False,
                "reason": "failed",
                "error": str(exc)[:500],
                "created_ts": time.time(),
            }
            self._last_result = result
            return {**result, **self.status()}

    def create(self, *, force: bool = False, reason: str = "scheduled") -> dict[str, Any]:
        with self._lock:
            return self._create_locked(force=force, reason=reason)

    def _inspect_path(self, path: Path) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="backup_inspect_") as temp_name:
            temp_dir = Path(temp_name)
            with zipfile.ZipFile(path, "r") as archive:
                manifest = self._validate_zip_structure(archive)
                names = set(archive.namelist())
                counts: dict[str, dict[str, int]] = {}
                verified: list[str] = []
                for entry in list(manifest.get("databases") or []) + list(manifest.get("json_files") or []):
                    archive_name = str(entry.get("archive_name") or entry.get("name") or "")
                    if not archive_name or archive_name not in names:
                        raise RuntimeError(f"manifest entry is missing: {archive_name or 'unnamed'}")
                    target = temp_dir / Path(archive_name).name
                    target.write_bytes(archive.read(archive_name))
                    expected = str(entry.get("sha256") or "").upper()
                    if expected and self._digest(target) != expected:
                        raise RuntimeError(f"digest mismatch: {archive_name}")
                    verified.append(archive_name)
                    if archive_name.endswith(".db"):
                        self._check_sqlite(target)
                        counts[archive_name] = self._sqlite_counts(target)
            stat = path.stat()
            return {
                "valid": True,
                "file": path.name,
                "bytes": int(stat.st_size),
                "modified_ts": float(stat.st_mtime),
                "sha256": self._digest(path),
                "manifest": manifest,
                "verified_files": verified,
                "counts": counts,
                "restore_files": [
                    name for name in verified
                    if name in {"memories.db", "episodic_memory.db"} or name in _JSON_RESTORE_FILES
                ],
            }

    def inspect(self, file_name: str) -> dict[str, Any]:
        with self._lock:
            return self._inspect_path(self._resolve_archive(file_name))

    def list_archives(self) -> dict[str, Any]:
        with self._lock:
            pending = self.pending_restore()
            items = []
            for path in self._archives():
                manifest: dict[str, Any] = {}
                error = ""
                try:
                    with zipfile.ZipFile(path, "r") as archive:
                        if "manifest.json" in archive.namelist():
                            loaded = json.loads(archive.read("manifest.json").decode("utf-8"))
                            manifest = loaded if isinstance(loaded, dict) else {}
                        else:
                            error = "manifest missing"
                except Exception as exc:
                    error = str(exc)[:200]
                stat = path.stat()
                items.append({
                    "file": path.name,
                    "bytes": int(stat.st_size),
                    "modified_ts": float(stat.st_mtime),
                    "created_ts": float(manifest.get("created_ts") or stat.st_mtime),
                    "plugin_version": str(manifest.get("plugin_version") or "unknown"),
                    "reason": str(manifest.get("reason") or "unknown"),
                    "database_count": len(manifest.get("databases") or []),
                    "json_count": len(manifest.get("json_files") or []),
                    "manifest_ok": bool(manifest) and not error,
                    "error": error,
                    "pending_restore": path.name == pending.get("target_file"),
                    "safety_backup": path.name == pending.get("safety_backup"),
                })
            return {"items": items, **self.status()}

    def prepare_restore(self, file_name: str) -> dict[str, Any]:
        with self._lock:
            existing = self.pending_restore()
            if existing:
                raise RuntimeError("a restore is already pending; cancel it before selecting another backup")
            target = self._resolve_archive(file_name)
            inspection = self._inspect_path(target)
            pending = {
                "format": 1,
                "status": "preparing",
                "target_file": target.name,
                "target_sha256": inspection["sha256"],
                "scheduled_ts": time.time(),
                "restore_files": list(inspection.get("restore_files") or []),
                "counts": dict(inspection.get("counts") or {}),
            }
            self._write_json_atomic(self._pending_path, pending)
            safety = self._create_locked(
                force=True, reason="pre_restore_safety", prune=False,
            )
            if not safety.get("created"):
                self._pending_path.unlink(missing_ok=True)
                raise RuntimeError("pre-restore safety backup failed: " + str(safety.get("error") or "unknown"))
            pending.update({
                "status": "ready",
                "safety_backup": safety.get("file"),
                "prepared_ts": time.time(),
            })
            self._write_json_atomic(self._pending_path, pending)
            self._prune()
            return {
                "scheduled": True,
                "restart_required": True,
                "pending_restore": pending,
                "inspection": inspection,
            }

    def cancel_restore(self) -> dict[str, Any]:
        with self._lock:
            pending = self.pending_restore()
            self._pending_path.unlink(missing_ok=True)
            return {"cancelled": bool(pending), "pending_restore": pending}

    def delete_archive(self, file_name: str) -> dict[str, Any]:
        with self._lock:
            path = self._resolve_archive(file_name)
            protected = self._protected_archive_names()
            if path.name in protected:
                raise RuntimeError("backup is protected by a pending restore")
            path.unlink()
            return {"deleted": True, "file": path.name, **self.status()}

    def archive_path(self, file_name: str) -> Path:
        with self._lock:
            return self._resolve_archive(file_name)

    def apply_pending_restore(self) -> dict[str, Any]:
        with self._lock:
            pending = self.pending_restore()
            if not pending:
                return {"applied": False, "reason": "none"}
            if pending.get("status") != "ready":
                return {"applied": False, "reason": "not_ready", "pending_restore": pending}
            try:
                source = self._resolve_archive(str(pending.get("target_file") or ""))
                expected_sha = str(pending.get("target_sha256") or "").upper()
                if expected_sha and self._digest(source) != expected_sha:
                    raise RuntimeError("selected backup changed after restore was scheduled")
                inspection = self._inspect_path(source)
                with tempfile.TemporaryDirectory(prefix="backup_restore_", dir=self.backup_dir) as temp_name:
                    temp_dir = Path(temp_name)
                    with zipfile.ZipFile(source, "r") as archive:
                        destinations: dict[str, Path] = {
                            "memories.db": self.vec_db_path,
                            "episodic_memory.db": self.episodic_db_path,
                        }
                        for name in _JSON_RESTORE_FILES:
                            destinations[name] = self.vec_db_path.parent / name
                        replacements: list[tuple[Path, Path]] = []
                        for name in inspection.get("restore_files") or []:
                            destination = destinations.get(name)
                            if destination is None:
                                continue
                            staged = temp_dir / ("new_" + name)
                            staged.write_bytes(archive.read(name))
                            if name.endswith(".db"):
                                self._check_sqlite(staged)
                            replacements.append((staged, destination))
                    if not replacements:
                        raise RuntimeError("backup contains no restorable plugin data")
                    rollback_dir = temp_dir / "rollback"
                    rollback_dir.mkdir()
                    rollback: dict[Path, Path | None] = {}
                    replaced: list[Path] = []
                    try:
                        for index, (staged, destination) in enumerate(replacements):
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            old_copy: Path | None = None
                            if destination.exists():
                                old_copy = rollback_dir / f"{index}_{destination.name}"
                                shutil.copy2(destination, old_copy)
                            rollback[destination] = old_copy
                            os.replace(staged, destination)
                            replaced.append(destination)
                        for destination in replaced:
                            if destination.suffix == ".db":
                                for suffix in ("-wal", "-shm"):
                                    try:
                                        destination.with_name(destination.name + suffix).unlink(missing_ok=True)
                                    except OSError:
                                        pass
                    except Exception:
                        for destination in reversed(replaced):
                            old_copy = rollback.get(destination)
                            if old_copy is None:
                                destination.unlink(missing_ok=True)
                            elif old_copy.exists():
                                os.replace(old_copy, destination)
                        raise
                result = {
                    "applied": True,
                    "file": source.name,
                    "safety_backup": pending.get("safety_backup"),
                    "restored_files": list(inspection.get("restore_files") or []),
                    "applied_ts": time.time(),
                }
                self._write_json_atomic(self._last_restore_path, result)
                self._pending_path.unlink(missing_ok=True)
                return result
            except Exception as exc:
                pending["status"] = "failed"
                pending["last_error"] = str(exc)[:500]
                pending["failed_ts"] = time.time()
                pending["attempts"] = int(pending.get("attempts") or 0) + 1
                self._write_json_atomic(self._pending_path, pending)
                return {
                    "applied": False,
                    "reason": "failed",
                    "error": pending["last_error"],
                    "pending_restore": pending,
                }
