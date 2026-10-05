"""Durable scheduling policy, shared job budgets and conservative recovery."""
import hashlib
import json
import time

from .store import TaskStore, ConflictError, encoded


class SchedulingStore(TaskStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, cap INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS schedules (
                    task_id TEXT PRIMARY KEY, policy TEXT NOT NULL,
                    owner TEXT, lease_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS legacy_imports (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                    family TEXT NOT NULL, status TEXT NOT NULL, digest TEXT NOT NULL);
                COMMIT;
            ''')

    def attach(self, task_id, policy, job_cap=6):
        if type(job_cap) is not int or not 1 <= job_cap <= 100:
            raise ValueError('job cap must be explicitly bounded')
        policy_text = encoded(policy)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT spec FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            spec = json.loads(row['spec'])
            job = db.execute('SELECT cap FROM jobs WHERE id=?', (spec['job_id'],)).fetchone()
            if job and job['cap'] != job_cap:
                raise ConflictError('job budget cannot silently change')
            existing = db.execute('SELECT policy FROM schedules WHERE task_id=?', (task_id,)).fetchone()
            if existing and existing['policy'] != policy_text:
                raise ConflictError('route policy changed; revise task')
            db.execute('INSERT OR IGNORE INTO jobs(id,cap) VALUES(?,?)', (spec['job_id'], job_cap))
            db.execute('INSERT OR IGNORE INTO schedules(task_id,policy) VALUES(?,?)', (task_id, policy_text))

    def reserve(self, db, task_id, spec):
        if not db.execute('SELECT 1 FROM schedules WHERE task_id=?', (task_id,)).fetchone():
            raise ConflictError('scheduled task requires policy and budget')
        result = db.execute('UPDATE jobs SET used=used+1 WHERE id=? AND used<cap', (spec['job_id'],))
        if result.rowcount != 1:
            raise ConflictError('job attempt budget exhausted')

    def acquire(self, task_id, owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT spec,status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            end = json.loads(task['spec'])['deadline_at'] + 30
            result = db.execute('UPDATE schedules SET owner=?,lease_until=? WHERE task_id=? AND owner IS NULL',
                                (owner, end, task_id))
            if result.rowcount != 1:
                raise ConflictError('task owned by another scheduler; explicit recovery required')

    def release(self, task_id, owner):
        with self.connect() as db:
            db.execute('UPDATE schedules SET owner=NULL,lease_until=0 WHERE task_id=? AND owner=?', (task_id, owner))

    def reopen(self, task_id, owner):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            lease = db.execute('SELECT owner FROM schedules WHERE task_id=?', (task_id,)).fetchone()
            if lease is None or lease['owner'] != owner:
                raise ConflictError('scheduler ownership lost')
            result = db.execute("UPDATE tasks SET status='pending' WHERE id=? AND status='awaiting_recovery'", (task_id,))
            if result.rowcount != 1:
                raise ConflictError('task not recoverable by policy')

    def recover_expired(self, now=None):
        """Expired in-flight attempts stay unknown, never automatically reissued."""
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT task_id FROM schedules WHERE owner IS NOT NULL AND lease_until<?', (now,)).fetchall()
            for row in rows:
                tid = row['task_id']
                db.execute("UPDATE attempts SET outcome='outcome_unknown',ended=?,metadata=? WHERE task_id=? AND outcome='running'",
                           (now, encoded({'kind':'interrupted', 'possibly_billed':True}), tid))
                db.execute("UPDATE tasks SET status='outcome_unknown',owner=NULL,updated=? WHERE id=? AND status='running'", (now, tid))
                db.execute('UPDATE schedules SET owner=NULL,lease_until=0 WHERE task_id=?', (tid,))
            return len(rows)

    def attempts_for(self, task_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM attempts WHERE task_id=? ORDER BY ordinal', (task_id,))]

    def job(self, job_id):
        with self.connect() as db:
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def pause_pending(self, task_id, status='awaiting_recovery'):
        if status not in {'awaiting_recovery', 'cancelled'}:
            raise ValueError('invalid pending state')
        with self.connect() as db:
            db.execute('UPDATE tasks SET status=?,updated=? WHERE id=? AND status IN (?,?)',
                       (status, time.time(), task_id, 'pending', 'awaiting_recovery'))

    def resumable(self, limit=128):
        if not 1 <= limit <= 1024:
            raise ValueError('invalid recovery page size')
        with self.connect() as db:
            rows = db.execute('''SELECT t.id,t.spec,s.policy FROM tasks t
                JOIN schedules s ON s.task_id=t.id
                WHERE s.owner IS NULL AND t.status IN ('pending','awaiting_recovery')
                ORDER BY t.created LIMIT ?''', (limit,)).fetchall()
            return [dict(r) for r in rows]

    def import_legacy(self, source):
        """Metadata-only staging: do not execute unverified old payloads/checkpoints."""
        from .preflight import readonly
        reader = readonly(source)
        try:
            reader.execute('BEGIN')
            rows = reader.execute('SELECT id,source_batch_id,task,status FROM failed_llm_requests').fetchall()
        finally:
            reader.close()
        with self.connect() as db:
            for row in rows:
                rid, batch, family, state = tuple(row)
                identity = hashlib.sha256((family+'\0'+(batch or rid)).encode()).hexdigest()
                digest = hashlib.sha256(encoded(list(row)).encode()).hexdigest()
                status = 'already_restored' if state == 'restored' else ('needs_source_verification' if batch else 'unbound')
                db.execute('INSERT OR IGNORE INTO legacy_imports VALUES(?,?,?,?,?,?)',
                           (rid, identity, batch, family, status, digest))
            return [dict(r) for r in db.execute('SELECT source_id,status,COUNT(*) AS count FROM legacy_imports GROUP BY source_id,status')]
