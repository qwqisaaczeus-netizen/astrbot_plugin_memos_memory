from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from astrbot_plugin_memos_memory.data_backup import DataBackupManager


def _make_db(path: Path, value: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE sample(value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample(value) VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _set_db_value(path: Path, value: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE sample SET value=?", (value,))
        conn.commit()
    finally:
        conn.close()


def _get_db_value(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("SELECT value FROM sample").fetchone()[0])
    finally:
        conn.close()


class DataBackupManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vec_db = self.root / "memories.db"
        self.episodic_db = self.root / "episodic_memory.db"
        self.backup_dir = self.root / "data_backups"
        _make_db(self.vec_db, "vector-data")
        _make_db(self.episodic_db, "episode-data")
        (self.root / "xinchao_state.json").write_text(
            json.dumps({"drive": "share"}), encoding="utf-8",
        )
        (self.root / "astrbot_plugin_memos_memory_config.json").write_text(
            json.dumps({"memos_token": "must-not-be-backed-up"}), encoding="utf-8",
        )
        self.manager = DataBackupManager(
            backup_dir=str(self.backup_dir),
            vec_db_path=str(self.vec_db),
            episodic_db_path=str(self.episodic_db),
            plugin_version="test",
            interval_days=14,
            keep=2,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_creates_consistent_secret_free_archive(self) -> None:
        result = self.manager.create(force=True, reason="unit_test")
        self.assertTrue(result["created"])
        archive_path = Path(result["path"])
        self.assertTrue(archive_path.is_file())
        with zipfile.ZipFile(archive_path, "r") as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {"memories.db", "episodic_memory.db", "xinchao_state.json", "manifest.json"},
            )
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            self.assertFalse(manifest["contains_config_or_tokens"])
            self.assertEqual(manifest["reason"], "unit_test")
            self.assertNotIn("must-not-be-backed-up", archive.read("manifest.json").decode("utf-8"))
            with tempfile.TemporaryDirectory() as extracted:
                archive.extract("memories.db", extracted)
                conn = sqlite3.connect(str(Path(extracted) / "memories.db"))
                try:
                    value = conn.execute("SELECT value FROM sample").fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(value, "vector-data")

    def test_schedule_skips_until_interval_is_due(self) -> None:
        created = self.manager.create(force=True)
        skipped = self.manager.create(force=False)
        self.assertTrue(created["created"])
        self.assertFalse(skipped["created"])
        self.assertEqual(skipped["reason"], "not_due")
        old_ts = time.time() - 15 * 86400
        os.utime(Path(created["path"]), (old_ts, old_ts))
        due = self.manager.create(force=False)
        self.assertTrue(due["created"])

    def test_retention_keeps_latest_archives(self) -> None:
        for _ in range(4):
            self.assertTrue(self.manager.create(force=True)["created"])
        archives = list(self.backup_dir.glob("memory_backup_*.zip"))
        self.assertEqual(len(archives), 2)
        self.assertEqual(self.manager.status()["count"], 2)

    def test_failure_is_bounded_and_leaves_no_partial_zip(self) -> None:
        manager = DataBackupManager(
            backup_dir=str(self.backup_dir),
            vec_db_path=str(self.root / "missing-vector.db"),
            episodic_db_path=str(self.root / "missing-episode.db"),
            plugin_version="test",
        )
        result = manager.create(force=True)
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "failed")
        self.assertIn("no plugin database", result["error"])
        self.assertEqual(list(self.backup_dir.glob("*.tmp")), [])

    def test_inspect_and_list_validate_archive(self) -> None:
        created = self.manager.create(force=True, reason="webui_manual")
        listed = self.manager.list_archives()
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["items"][0]["file"], created["file"])
        self.assertTrue(listed["items"][0]["manifest_ok"])
        inspected = self.manager.inspect(created["file"])
        self.assertTrue(inspected["valid"])
        self.assertEqual(
            set(inspected["restore_files"]),
            {"memories.db", "episodic_memory.db", "xinchao_state.json"},
        )
        self.assertEqual(len(inspected["sha256"]), 64)

    def test_prepare_and_apply_restore_preserves_config_and_runtime_telemetry(self) -> None:
        (self.root / "xinchao_settings.json").write_text(
            json.dumps({"sensitivity": 0.4}), encoding="utf-8",
        )
        (self.root / "runtime_telemetry.json").write_text(
            json.dumps({"samples": ["old"]}), encoding="utf-8",
        )
        created = self.manager.create(force=True, reason="unit_restore")

        _set_db_value(self.vec_db, "new-vector")
        _set_db_value(self.episodic_db, "new-episode")
        (self.root / "xinchao_state.json").write_text(
            json.dumps({"drive": "rest"}), encoding="utf-8",
        )
        (self.root / "xinchao_settings.json").write_text(
            json.dumps({"sensitivity": 0.9}), encoding="utf-8",
        )
        (self.root / "runtime_telemetry.json").write_text(
            json.dumps({"samples": ["current"]}), encoding="utf-8",
        )
        config_before = (self.root / "astrbot_plugin_memos_memory_config.json").read_text(
            encoding="utf-8",
        )

        prepared = self.manager.prepare_restore(created["file"])
        pending = prepared["pending_restore"]
        self.assertTrue(prepared["restart_required"])
        self.assertTrue((self.backup_dir / pending["safety_backup"]).is_file())
        self.assertTrue(self.manager.list_archives()["items"][0])

        applied = self.manager.apply_pending_restore()
        self.assertTrue(applied["applied"])
        self.assertEqual(_get_db_value(self.vec_db), "vector-data")
        self.assertEqual(_get_db_value(self.episodic_db), "episode-data")
        self.assertEqual(
            json.loads((self.root / "xinchao_state.json").read_text(encoding="utf-8"))["drive"],
            "share",
        )
        self.assertEqual(
            json.loads((self.root / "xinchao_settings.json").read_text(encoding="utf-8"))["sensitivity"],
            0.4,
        )
        self.assertEqual(
            json.loads((self.root / "runtime_telemetry.json").read_text(encoding="utf-8"))["samples"],
            ["current"],
        )
        self.assertEqual(
            (self.root / "astrbot_plugin_memos_memory_config.json").read_text(encoding="utf-8"),
            config_before,
        )
        self.assertEqual(self.manager.pending_restore(), {})
        self.assertTrue(self.manager.last_restore()["applied"])

    def test_restore_target_change_fails_without_touching_current_data(self) -> None:
        created = self.manager.create(force=True)
        _set_db_value(self.vec_db, "current-vector")
        prepared = self.manager.prepare_restore(created["file"])
        target = self.backup_dir / prepared["pending_restore"]["target_file"]
        with target.open("ab") as stream:
            stream.write(b"changed")
        result = self.manager.apply_pending_restore()
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "failed")
        self.assertEqual(_get_db_value(self.vec_db), "current-vector")
        self.assertEqual(self.manager.pending_restore()["status"], "failed")

    def test_pending_target_and_safety_backup_are_protected_with_keep_one(self) -> None:
        manager = DataBackupManager(
            backup_dir=str(self.backup_dir),
            vec_db_path=str(self.vec_db),
            episodic_db_path=str(self.episodic_db),
            plugin_version="test",
            keep=1,
        )
        target = manager.create(force=True)["file"]
        prepared = manager.prepare_restore(target)
        pending = prepared["pending_restore"]
        self.assertTrue((self.backup_dir / target).is_file())
        self.assertTrue((self.backup_dir / pending["safety_backup"]).is_file())
        self.assertEqual(manager.status()["count"], 2)
        with self.assertRaises(RuntimeError):
            manager.delete_archive(target)
        with self.assertRaises(RuntimeError):
            manager.prepare_restore(target)
        cancelled = manager.cancel_restore()
        self.assertTrue(cancelled["cancelled"])
        self.assertTrue(manager.delete_archive(target)["deleted"])

    def test_partial_restore_failure_rolls_back_already_replaced_database(self) -> None:
        created = self.manager.create(force=True)
        _set_db_value(self.vec_db, "current-vector")
        _set_db_value(self.episodic_db, "current-episode")
        self.manager.prepare_restore(created["file"])
        real_replace = os.replace

        def fail_episode_replace(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if destination_path == self.episodic_db and source_path.name.startswith("new_"):
                raise OSError("simulated second database replacement failure")
            return real_replace(source, destination)

        with mock.patch(
            "astrbot_plugin_memos_memory.data_backup.os.replace",
            side_effect=fail_episode_replace,
        ):
            result = self.manager.apply_pending_restore()
        self.assertFalse(result["applied"])
        self.assertIn("simulated", result["error"])
        self.assertEqual(_get_db_value(self.vec_db), "current-vector")
        self.assertEqual(_get_db_value(self.episodic_db), "current-episode")


if __name__ == "__main__":
    unittest.main()
