"""Independent task ledger; source archives remain authoritative elsewhere."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


class ConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    job_id: str
    category: str
    scope: str
    prompt_version: str
    schema_version: str
    route_version: str
    deadline_at: float
    max_attempts: int = 3
    priority: int = 0

    def __post_init__(self):
        if not all(isinstance(v, str) and v for v in (self.task_id, self.job_id, self.category,
                   self.scope, self.prompt_version, self.schema_version, self.route_version)):
            raise ValueError('task identity and versions required')
        if not math.isfinite(self.deadline_at) or self.deadline_at <= 0:
            raise ValueError('invalid task deadline')
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError('max_attempts must be 1..3')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('unsupported ledger version')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and tables:
                raise ValueError('refusing unrelated database')
            if version == 1 and (db.execute('PRAGMA application_id').fetchone()[0] != 1296905777
                                 or not {'tasks', 'attempts', 'artifacts'} <= tables):
                raise ValueError('not a generation runtime ledger')
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    input_id TEXT NOT NULL, status TEXT NOT NULL, owner TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, output_id TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
                    digest TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    route_id TEXT NOT NULL, started REAL NOT NULL, ended REAL,
                    outcome TEXT NOT NULL, metadata TEXT NOT NULL,
                    UNIQUE(task_id, ordinal));
                CREATE INDEX IF NOT EXISTS attempt_task ON attempts(task_id);
                PRAGMA user_version=1;
                PRAGMA application_id=1296905777;
                COMMIT;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _artifact(self, db, task_id, kind, value):
        body = encoded(value)
        digest = hashlib.sha256(body.encode()).hexdigest()
        aid = hashlib.sha256((task_id + ':' + kind + ':' + digest).encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?)',
                   (aid, task_id, kind, digest, body, time.time()))
        return aid

    def create(self, spec: TaskSpec, request: dict):
        # Only semantic inputs. Routing credentials/configuration are never stored.
        if set(request) != {'prompt', 'contexts', 'system_prompt'}:
            raise ValueError('only semantic request fields permitted')
        if not isinstance(request['prompt'], str) or not isinstance(request['system_prompt'], str):
            raise ValueError('invalid text request')
        if not isinstance(request['contexts'], list) or any(
            not isinstance(c, dict) or set(c) != {'role', 'content'} or
            c['role'] not in {'user', 'assistant', 'system'} or not isinstance(c['content'], str)
            for c in request['contexts']):
            raise ValueError('contexts must contain only role and content')
        spec_json = encoded(asdict(spec))
        fingerprint = hashlib.sha256((spec_json + encoded(request)).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint FROM tasks WHERE id=?', (spec.task_id,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ConflictError('same task identity with changed input/contract')
                return
            aid = self._artifact(db, spec.task_id, 'input', request)
            now = time.time()
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,NULL,0,NULL,?,?)',
                       (spec.task_id, spec_json, fingerprint, aid, 'pending', now, now))

    def read(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            return dict(row)

    def artifact(self, aid):
        with self.connect() as db:
            row = db.execute('SELECT body,digest FROM artifacts WHERE id=?', (aid,)).fetchone()
            if row is None or hashlib.sha256(row['body'].encode()).hexdigest() != row['digest']:
                raise ConflictError('missing or damaged artifact')
            return json.loads(row['body'])

    def claim(self, task_id, route_id):
        # route_id is an opaque hash, never a URL containing credentials.
        if len(route_id) != 64 or any(c not in '0123456789abcdef' for c in route_id):
            raise ValueError('route_id must be a SHA256 identity')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            spec = json.loads(row['spec'])
            if row['status'] != 'pending' or row['attempts'] >= spec['max_attempts']:
                raise ConflictError('task is not claimable')
            if time.time() >= spec['deadline_at']:
                raise ConflictError('task deadline expired')
            self.reserve(db, task_id, spec)
            attempt_id = uuid.uuid4().hex
            ordinal = row['attempts'] + 1
            now = time.time()
            db.execute('UPDATE tasks SET status=?,owner=?,attempts=?,updated=? WHERE id=?',
                       ('running', attempt_id, ordinal, now, task_id))
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,NULL,?,?)',
                       (attempt_id, task_id, ordinal, route_id, now, 'running', '{}'))
            return attempt_id

    def reserve(self, db, task_id, spec):
        """Extended by the scheduled store inside the same claim transaction."""

    def finish(self, task_id, owner, outcome, metadata, output=None):
        if outcome not in {'succeeded', 'awaiting_recovery', 'cancelled', 'output_rejected'}:
            raise ValueError('invalid completion state')
        allowed = {'kind', 'http_status', 'retry_after', 'possibly_billed', 'elapsed_ms',
                   'effective_timeout', 'capabilities', 'usage', 'validation', 'timeout_source',
                   'provider_request_id', 'cost_estimate', 'diagnostics',
                   'finish_reason', 'completion_tokens', 'reasoning_tokens'}
        if set(metadata) - allowed:
            raise ValueError('unexpected telemetry field')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,owner FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None or row['status'] != 'running' or row['owner'] != owner:
                raise ConflictError('stale completion')
            aid = self._artifact(db, task_id, 'output', output) if output is not None else None
            if outcome == 'succeeded' and aid is None:
                raise ValueError('success needs validated artifact')
            now = time.time()
            db.execute('UPDATE attempts SET ended=?,outcome=?,metadata=? WHERE id=?',
                       (now, outcome, encoded(metadata), owner))
            db.execute('UPDATE tasks SET status=?,owner=NULL,output_id=?,updated=? WHERE id=?',
                       (outcome, aid, now, task_id))

    def export_metadata(self, task_id):
        row = self.read(task_id)
        spec = json.loads(row['spec'])
        with self.connect() as db:
            attempts = [dict(r) for r in db.execute('SELECT * FROM attempts WHERE task_id=? ORDER BY ordinal', (task_id,))]
        return {'task_id': task_id, 'status': row['status'], 'category': spec['category'],
                'attempts': attempts, 'input_digest': row['fingerprint'], 'output_id': row['output_id']}
