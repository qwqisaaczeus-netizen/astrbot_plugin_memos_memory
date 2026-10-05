"""Deterministic, read-only SQLite inventory helpers for offline calibration."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _uri(path: Path) -> str:
    return "file:" + quote(str(path.resolve()), safe="/\\:") + "?mode=ro&immutable=1"


def _encode(value: Any) -> bytes:
    if value is None:
        return b"N;"
    if isinstance(value, bytes):
        return b"B" + len(value).to_bytes(8, "big") + value
    if isinstance(value, bool):
        return b"Q1;" if value else b"Q0;"
    if isinstance(value, int):
        return b"I" + str(value).encode() + b";"
    if isinstance(value, float):
        return b"F" + value.hex().encode() + b";"
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return b"S" + len(raw).to_bytes(8, "big") + raw
    raw = repr(value).encode("utf-8")
    return b"R" + len(raw).to_bytes(8, "big") + raw


def table_inventory(path: str | Path) -> dict[str, Any]:
    conn = sqlite3.connect(_uri(Path(path)), uri=True)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        output: dict[str, Any] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({quoted})")]
            rows = list(conn.execute(f"SELECT * FROM {quoted}"))
            digest = hashlib.sha256()
            encoded_rows = sorted(b"".join(_encode(value) for value in row) for row in rows)
            for encoded in encoded_rows:
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            output[table] = {
                "columns": columns,
                "rows": len(rows),
                "sha256": digest.hexdigest(),
            }
        return output
    finally:
        conn.close()
