from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
ACTIVE_SESSION_STATES = ("active", "digest_ready")


class _ClosingConnection(sqlite3.Connection):
    """Make transaction context managers release Windows file handles too."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class HouseStore:
    """Thread-safe SQLite persistence for the optional house subsystem."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS house_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS house_sessions (
                    id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    umo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_conversation_at TEXT,
                    awakened_at TEXT,
                    closed_at TEXT,
                    timezone TEXT NOT NULL,
                    input_revision TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    digest_id TEXT,
                    linked_at TEXT,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_house_sessions_scope_status
                    ON house_sessions(scope_key, status, started_at DESC);
                CREATE TABLE IF NOT EXISTS house_source_snapshots (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_version TEXT NOT NULL DEFAULT '',
                    captured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(session_id) REFERENCES house_sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_house_sources_session
                    ON house_source_snapshots(session_id, source_name);
                CREATE TABLE IF NOT EXISTS house_artifacts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    reality_status TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL DEFAULT '[]',
                    mood_json TEXT NOT NULL DEFAULT '[]',
                    thought_cues_json TEXT NOT NULL DEFAULT '[]',
                    drive_candidates_json TEXT NOT NULL DEFAULT '[]',
                    content_hash TEXT NOT NULL,
                    semantic_key TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    sendable INTEGER NOT NULL DEFAULT 0,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    model_source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES house_sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_house_artifacts_scope_created
                    ON house_artifacts(scope_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_house_artifacts_session
                    ON house_artifacts(session_id, status, created_at);
                CREATE TABLE IF NOT EXISTS house_digests (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL UNIQUE,
                    afterglow TEXT NOT NULL,
                    awareness TEXT NOT NULL DEFAULT '',
                    thought_cues_json TEXT NOT NULL DEFAULT '[]',
                    drive_effects_json TEXT NOT NULL DEFAULT '[]',
                    source_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    expires_at TEXT NOT NULL,
                    link_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES house_sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS house_deliveries (
                    id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    artifact_revision INTEGER NOT NULL,
                    scope_key TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    sent_at TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(artifact_id) REFERENCES house_artifacts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_house_delivery_artifact
                    ON house_deliveries(artifact_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS house_feedback (
                    id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    feedback_type TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(artifact_id) REFERENCES house_artifacts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_house_feedback_artifact
                    ON house_feedback(artifact_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS house_consumption_ledger (
                    id TEXT PRIMARY KEY,
                    source_artifact_id TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    effect_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE(source_artifact_id, target_kind, target_id)
                );
                CREATE TABLE IF NOT EXISTS house_character_interactions (
                    id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    scene_period TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    pose TEXT NOT NULL DEFAULT '',
                    expression TEXT NOT NULL DEFAULT '',
                    variant_id TEXT NOT NULL DEFAULT '',
                    outfit TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT 'tap',
                    reaction_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_house_character_interactions_scope
                    ON house_character_interactions(scope_key, created_at DESC);
                """
            )
            interaction_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(house_character_interactions)"
                ).fetchall()
            }
            for name in ("variant_id", "outfit"):
                if name not in interaction_columns:
                    conn.execute(
                        f"ALTER TABLE house_character_interactions ADD COLUMN {name} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            conn.execute(
                "INSERT INTO house_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=5.0, factory=_ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _session(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["input"] = _loads(item.pop("input_json", "{}"), {})
        return item

    @staticmethod
    def _artifact(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for source, target, fallback in (
            ("source_ids_json", "source_ids", []),
            ("mood_json", "mood", []),
            ("thought_cues_json", "thought_cues", []),
            ("drive_candidates_json", "drive_candidates", []),
        ):
            item[target] = _loads(item.pop(source, ""), fallback)
        item["sendable"] = bool(item.get("sendable"))
        item["is_read"] = bool(item.get("is_read"))
        return item

    @staticmethod
    def _digest(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for source, target in (
            ("thought_cues_json", "thought_cues"),
            ("drive_effects_json", "drive_effects"),
            ("source_artifact_ids_json", "source_artifact_ids"),
        ):
            item[target] = _loads(item.pop(source, ""), [])
        return item

    def get_active_session(self, scope_key: str) -> dict[str, Any] | None:
        marks = ",".join("?" for _ in ACTIVE_SESSION_STATES)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM house_sessions WHERE scope_key=? AND status IN ({marks}) "
                "ORDER BY started_at DESC LIMIT 1",
                (scope_key, *ACTIVE_SESSION_STATES),
            ).fetchone()
        return self._session(row)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM house_sessions WHERE id=?", (str(session_id),),
            ).fetchone()
        return self._session(row)

    def add_source_snapshot(
        self,
        session_id: str,
        source_name: str,
        payload: dict[str, Any],
        *,
        source_version: str = "",
        payload_hash: str = "",
        truncated: bool = False,
        error: str = "",
    ) -> dict[str, Any]:
        item = {
            "id": "hss_" + uuid.uuid4().hex[:20],
            "session_id": str(session_id),
            "source_name": str(source_name)[:80],
            "source_version": str(source_version)[:40],
            "captured_at": utc_now_iso(),
            "payload_json": _json(payload),
            "payload_hash": str(payload_hash)[:128],
            "truncated": 1 if truncated else 0,
            "error": str(error or "")[:500],
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO house_source_snapshots(id,session_id,source_name,source_version,"
                "captured_at,payload_json,payload_hash,truncated,error) VALUES(?,?,?,?,?,?,?,?,?)",
                tuple(item.values()),
            )
            conn.commit()
        item["payload"] = payload
        item.pop("payload_json", None)
        item["truncated"] = bool(item["truncated"])
        return item

    def list_source_snapshots(self, session_id: str, limit: int = 80) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM house_source_snapshots WHERE session_id=? "
                "ORDER BY captured_at DESC LIMIT ?",
                (str(session_id), max(1, min(300, int(limit)))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json", ""), {})
            item["truncated"] = bool(item.get("truncated"))
            result.append(item)
        return result

    def ensure_session(
        self,
        scope_key: str,
        umo: str,
        started_at: str,
        last_conversation_at: str,
        time_zone: str,
        input_revision: str,
        input_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in ACTIVE_SESSION_STATES)
            row = conn.execute(
                f"SELECT * FROM house_sessions WHERE scope_key=? AND status IN ({marks}) "
                "ORDER BY started_at DESC LIMIT 1",
                (scope_key, *ACTIVE_SESSION_STATES),
            ).fetchone()
            if row is not None:
                conn.commit()
                return self._session(row) or {}, False
            now = utc_now_iso()
            session_id = "hs_" + uuid.uuid4().hex[:20]
            conn.execute(
                "INSERT INTO house_sessions(id,scope_key,umo,status,started_at,last_conversation_at,"
                "timezone,input_revision,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, scope_key, umo, "active", started_at,
                    last_conversation_at, time_zone, input_revision,
                    _json(input_payload), now, now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM house_sessions WHERE id=?", (session_id,)).fetchone()
        return self._session(row) or {}, True

    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "umo", "status", "awakened_at", "closed_at", "digest_id",
            "linked_at", "consumed_at", "input_revision", "input_json",
        }
        payload = {key: value for key, value in fields.items() if key in allowed}
        if "input_json" in payload and not isinstance(payload["input_json"], str):
            payload["input_json"] = _json(payload["input_json"])
        payload["updated_at"] = utc_now_iso()
        assignments = ",".join(f"{key}=?" for key in payload)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE house_sessions SET {assignments} WHERE id=?",
                (*payload.values(), session_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM house_sessions WHERE id=?", (session_id,)).fetchone()
        return self._session(row)

    def list_sessions(self, scope_key: str = "", limit: int = 60) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._lock, self._connect() as conn:
            if scope_key:
                rows = conn.execute(
                    "SELECT * FROM house_sessions WHERE scope_key=? ORDER BY started_at DESC LIMIT ?",
                    (scope_key, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM house_sessions ORDER BY started_at DESC LIMIT ?", (limit,),
                ).fetchall()
        return [self._session(row) or {} for row in rows]

    def insert_artifact(self, value: dict[str, Any]) -> dict[str, Any]:
        now = str(value.get("created_at") or utc_now_iso())
        artifact_id = str(value.get("id") or ("ha_" + uuid.uuid4().hex[:20]))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO house_artifacts(
                    id,session_id,scope_key,artifact_type,title,content,summary,reality_status,
                    source_ids_json,mood_json,thought_cues_json,drive_candidates_json,
                    content_hash,semantic_key,revision,status,sendable,is_read,model_source,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    artifact_id, value["session_id"], value["scope_key"], value["artifact_type"],
                    str(value.get("title") or "")[:120], str(value.get("content") or "")[:8000],
                    str(value.get("summary") or "")[:500], str(value.get("reality_status") or "internal"),
                    _json(value.get("source_ids") or []), _json(value.get("mood") or []),
                    _json(value.get("thought_cues") or []), _json(value.get("drive_candidates") or []),
                    str(value.get("content_hash") or ""), str(value.get("semantic_key") or "")[:240],
                    max(1, int(value.get("revision") or 1)), str(value.get("status") or "sealed"),
                    1 if value.get("sendable") else 0, 1 if value.get("is_read") else 0,
                    str(value.get("model_source") or "")[:80], now, now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM house_artifacts WHERE id=?", (artifact_id,)).fetchone()
        return self._artifact(row) or {}

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM house_artifacts WHERE id=?", (artifact_id,)).fetchone()
        return self._artifact(row)

    def list_artifacts(
        self,
        scope_key: str = "",
        session_id: str = "",
        artifact_type: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("scope_key", scope_key), ("session_id", session_id),
            ("artifact_type", artifact_type), ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit = max(1, min(500, int(limit)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM house_artifacts{where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._artifact(row) or {} for row in rows]

    def update_artifact(self, artifact_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"status", "is_read", "title", "content", "summary", "revision"}
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return self.get_artifact(artifact_id)
        if "is_read" in payload:
            payload["is_read"] = 1 if payload["is_read"] else 0
        payload["updated_at"] = utc_now_iso()
        assignments = ",".join(f"{key}=?" for key in payload)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE house_artifacts SET {assignments} WHERE id=?",
                (*payload.values(), artifact_id),
            )
            conn.commit()
        return self.get_artifact(artifact_id)

    def delete_artifact(self, artifact_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM house_artifacts WHERE id=?", (str(artifact_id),))
            conn.commit()
        return bool(cur.rowcount)

    def add_feedback(self, artifact_id: str, scope_key: str, feedback_type: str, note: str = "") -> dict[str, Any]:
        item = {
            "id": "hf_" + uuid.uuid4().hex[:20], "artifact_id": artifact_id,
            "scope_key": scope_key, "feedback_type": feedback_type,
            "note": str(note or "")[:500], "created_at": utc_now_iso(),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO house_feedback(id,artifact_id,scope_key,feedback_type,note,created_at) "
                "VALUES(?,?,?,?,?,?)",
                tuple(item.values()),
            )
            conn.commit()
        return item

    def feedback_summary(self, scope_key: str = "") -> dict[str, int]:
        with self._lock, self._connect() as conn:
            if scope_key:
                rows = conn.execute(
                    "SELECT feedback_type,COUNT(*) n FROM house_feedback WHERE scope_key=? GROUP BY feedback_type",
                    (scope_key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT feedback_type,COUNT(*) n FROM house_feedback GROUP BY feedback_type"
                ).fetchall()
        return {str(row["feedback_type"]): int(row["n"]) for row in rows}

    def upsert_digest(self, value: dict[str, Any]) -> dict[str, Any]:
        digest_id = str(value.get("id") or ("hd_" + uuid.uuid4().hex[:20]))
        created = str(value.get("created_at") or utc_now_iso())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO house_digests(
                    id,session_id,afterglow,awareness,thought_cues_json,drive_effects_json,
                    source_artifact_ids_json,expires_at,link_status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    afterglow=excluded.afterglow, awareness=excluded.awareness,
                    thought_cues_json=excluded.thought_cues_json,
                    drive_effects_json=excluded.drive_effects_json,
                    source_artifact_ids_json=excluded.source_artifact_ids_json,
                    expires_at=excluded.expires_at, link_status=excluded.link_status
                """,
                (
                    digest_id, value["session_id"], str(value.get("afterglow") or "")[:1200],
                    str(value.get("awareness") or "")[:800], _json(value.get("thought_cues") or []),
                    _json(value.get("drive_effects") or []), _json(value.get("source_artifact_ids") or []),
                    value["expires_at"], str(value.get("link_status") or "pending"), created,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM house_digests WHERE session_id=?", (value["session_id"],)).fetchone()
        return self._digest(row) or {}

    def get_digest(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM house_digests WHERE session_id=?", (session_id,)).fetchone()
        return self._digest(row)

    def update_digest_status(self, digest_id: str, status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE house_digests SET link_status=? WHERE id=?", (status, digest_id))
            conn.commit()

    def consume_many(
        self,
        artifact_ids: Iterable[str],
        target_kind: str,
        target_id: str,
        effect_hash: str,
    ) -> int:
        created = utc_now_iso()
        inserted = 0
        with self._lock, self._connect() as conn:
            for artifact_id in artifact_ids:
                try:
                    conn.execute(
                        "INSERT INTO house_consumption_ledger(id,source_artifact_id,target_kind,target_id,"
                        "consumed_at,effect_hash,status) VALUES(?,?,?,?,?,?,?)",
                        (
                            "hc_" + uuid.uuid4().hex[:20], artifact_id, target_kind,
                            target_id, created, effect_hash, "consumed",
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    continue
            conn.commit()
        return inserted

    def delivery_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM house_deliveries WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
        return dict(row) if row else None

    def create_delivery(
        self,
        artifact: dict[str, Any],
        umo: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        existing = self.delivery_by_key(idempotency_key)
        if existing:
            return existing
        item = {
            "id": "hm_" + uuid.uuid4().hex[:20],
            "artifact_id": artifact["id"],
            "artifact_revision": int(artifact.get("revision") or 1),
            "scope_key": artifact["scope_key"], "umo": umo,
            "idempotency_key": idempotency_key, "status": "pending",
            "sent_at": None, "error": "", "created_at": utc_now_iso(),
        }
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO house_deliveries(id,artifact_id,artifact_revision,scope_key,umo,"
                    "idempotency_key,status,sent_at,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    tuple(item.values()),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                pass
        return self.delivery_by_key(idempotency_key) or item

    def finish_delivery(self, delivery_id: str, status: str, error: str = "") -> dict[str, Any] | None:
        sent_at = utc_now_iso() if status == "sent" else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE house_deliveries SET status=?,sent_at=?,error=? WHERE id=?",
                (status, sent_at, str(error or "")[:500], delivery_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM house_deliveries WHERE id=?", (delivery_id,)).fetchone()
        return dict(row) if row else None

    def recent_deliveries(self, scope_key: str = "", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if scope_key:
                rows = conn.execute(
                    "SELECT * FROM house_deliveries WHERE scope_key=? ORDER BY created_at DESC LIMIT ?",
                    (scope_key, max(1, min(200, int(limit)))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM house_deliveries ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(200, int(limit))),),
                ).fetchall()
        return [dict(row) for row in rows]

    def record_character_interaction(
        self,
        *,
        scope_key: str,
        session_id: str = "",
        scene_period: str = "",
        location: str = "",
        pose: str = "",
        expression: str = "",
        variant_id: str = "",
        outfit: str = "",
        action: str = "tap",
        reaction_key: str = "",
    ) -> dict[str, Any]:
        item = {
            "id": "hi_" + uuid.uuid4().hex[:20],
            "scope_key": str(scope_key)[:240],
            "session_id": str(session_id)[:80],
            "scene_period": str(scene_period)[:32],
            "location": str(location)[:40],
            "pose": str(pose)[:40],
            "expression": str(expression)[:40],
            "variant_id": str(variant_id)[:100],
            "outfit": str(outfit)[:80],
            "action": str(action or "tap")[:32],
            "reaction_key": str(reaction_key)[:80],
            "created_at": utc_now_iso(),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO house_character_interactions(id,scope_key,session_id,scene_period,"
                "location,pose,expression,variant_id,outfit,action,reaction_key,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(item.values()),
            )
            conn.commit()
        return item

    def recent_character_interactions(
        self,
        scope_key: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM house_character_interactions WHERE scope_key=? "
                "ORDER BY created_at DESC LIMIT ?",
                (str(scope_key)[:240], max(1, min(100, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self, scope_key: str = "") -> dict[str, Any]:
        where = " WHERE scope_key=?" if scope_key else ""
        params = (scope_key,) if scope_key else ()
        with self._lock, self._connect() as conn:
            artifacts = conn.execute(
                f"SELECT COUNT(*) total,SUM(CASE WHEN is_read=0 THEN 1 ELSE 0 END) unread,"
                f"SUM(CASE WHEN sendable=1 AND status='sealed' THEN 1 ELSE 0 END) sendable "
                f"FROM house_artifacts{where}", params,
            ).fetchone()
            sessions = conn.execute(
                f"SELECT COUNT(*) total,SUM(CASE WHEN status IN ('active','digest_ready','linked') THEN 1 ELSE 0 END) active "
                f"FROM house_sessions{where}", params,
            ).fetchone()
            types = conn.execute(
                f"SELECT artifact_type,COUNT(*) n FROM house_artifacts{where} GROUP BY artifact_type",
                params,
            ).fetchall()
        return {
            "artifacts": int(artifacts["total"] or 0),
            "unread": int(artifacts["unread"] or 0),
            "sendable": int(artifacts["sendable"] or 0),
            "sessions": int(sessions["total"] or 0),
            "active_sessions": int(sessions["active"] or 0),
            "types": {str(row["artifact_type"]): int(row["n"]) for row in types},
        }
