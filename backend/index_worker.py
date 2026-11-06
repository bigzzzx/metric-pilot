"""Durable, bounded catalog projection retries; SQL remains the source of truth.

The job lease only fences *job acknowledgements*.  The separate writer lock in
catalog.vector_index serializes actual remote writes, including request callers.
No timeout is treated as proof that a remote write did not happen.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

from backend.core import db

TABLES = {'catalog': 'catalog_index_jobs', 'wiki': 'metric_wiki_index_jobs',
          'lineage': 'lineage_index_jobs'}
MAX_ATTEMPTS = 5
LEASE_SECONDS = 90


def migrate(con):
    """Idempotent migration, safe inside a caller's governance transaction."""
    con.execute('''CREATE TABLE IF NOT EXISTS catalog_index_jobs(
        id TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT)''')
    for table in TABLES.values():
        columns = {r[1] for r in con.execute(f'PRAGMA table_info({table})')}
        if not columns:
            continue
        upgrading = 'attempts' not in columns
        for name, definition in (
            ('attempts', 'INTEGER NOT NULL DEFAULT 0'),
            ('next_attempt_at', 'REAL NOT NULL DEFAULT 0'),
            ('lease_token', "TEXT NOT NULL DEFAULT ''"),
            ('lease_until', 'REAL NOT NULL DEFAULT 0'),
            ('last_attempt_at', 'REAL'),
        ):
            if name not in columns:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        # Legacy failed/running jobs predate retry budgets and lease ownership.
        if upgrading:
            con.execute(f"UPDATE {table} SET status='pending',completed_at=NULL WHERE status IN ('failed','running')")
        con.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_ready ON {table}(status,next_attempt_at,lease_until)')


def init_schema(connection_factory=None):
    con = (connection_factory or db)()
    try:
        migrate(con)
        con.commit()
    finally:
        con.close()


def enqueue_catalog(con):
    """Insert an outbox record in the SAME transaction as the catalog change."""
    pending = con.execute("SELECT id FROM catalog_index_jobs WHERE status='pending' ORDER BY created_at,id LIMIT 1").fetchone()
    if pending:
        # A pending job has not taken its snapshot yet. It will always rebuild
        # current truth, so another committed change can safely share this wakeup.
        con.execute("UPDATE catalog_index_jobs SET attempts=0,next_attempt_at=0,error='' WHERE id=?",(pending['id'],))
        return pending['id']
    identifier = str(uuid.uuid4())
    con.execute('INSERT INTO catalog_index_jobs(id) VALUES (?)', (identifier,))
    return identifier


@contextmanager
def writer_lock(connection_factory=None):
    """Crash-released cross-process lock, independent of the platform database.

    Unlike an expiring timestamp lock, a paused old writer cannot outlive this
    lock and write after a replacement. No platform DB transaction is held while
    embedding or Milvus is contacted. A busy caller returns to durable retries.
    """
    con = (connection_factory or db)()
    try:
        filename = next((row[2] for row in con.execute('PRAGMA database_list') if row[1] == 'main'), '')
    finally:
        con.close()
    if not filename:
        raise RuntimeError('index_writer_requires_file_database')
    lock_path = Path(filename).resolve().with_name(Path(filename).name + '.catalog-index-lock')
    lock = sqlite3.connect(str(lock_path), timeout=0)
    try:
        lock.execute('BEGIN EXCLUSIVE')
        yield
    finally:
        lock.rollback()
        lock.close()


def _view(con, table, identifier):
    row = con.execute(f'SELECT * FROM {table} WHERE id=?', (identifier,)).fetchone()
    return dict(row) if row else {'id': identifier, 'status': 'missing'}


def run_job(kind, identifier, *, connection_factory=None, refresh=None, now=None,
            max_attempts=MAX_ATTEMPTS, lease_seconds=LEASE_SECONDS, base_delay=5):
    """Claim one due job and CAS its acknowledgement; retries always use live truth."""
    table = TABLES[kind]
    factory = connection_factory or db
    clock = now if callable(now) else time.time if now is None else lambda: now
    started = clock()
    token = str(uuid.uuid4())
    con = factory()
    try:
        con.execute('BEGIN IMMEDIATE')
        job = _view(con, table, identifier)
        if job['status'] in {'missing', 'completed', 'failed'}:
            return job
        if job['status'] == 'running' and job['lease_until'] > started:
            return {**job, 'claimed': False}
        if job['status'] == 'pending' and job['next_attempt_at'] > started:
            return {**job, 'claimed': False}
        if job['attempts'] >= max_attempts:
            con.execute(f"UPDATE {table} SET status='failed',error='retry_budget_exhausted',lease_token='',lease_until=0 WHERE id=?", (identifier,))
            con.commit()
            return _view(con, table, identifier)
        con.execute(f'''UPDATE {table} SET status='running',attempts=attempts+1,
            lease_token=?,lease_until=?,last_attempt_at=?,completed_at=NULL WHERE id=?''',
            (token, started + lease_seconds, started, identifier))
        con.commit()
    finally:
        con.close()
    try:
        if refresh is None:
            from backend.catalog.vector_index import ensure_catalog_index
            success = bool(ensure_catalog_index())
        else:
            success = bool(refresh())
    except Exception:
        success = False
    finished = clock()
    con = factory()
    try:
        con.execute('BEGIN IMMEDIATE')
        job = _view(con, table, identifier)
        if job.get('lease_token') != token or job['status'] != 'running':
            return {**job, 'stale_acknowledgement': True}
        status = 'completed' if success else 'failed' if job['attempts'] >= max_attempts else 'pending'
        delay = min(300, base_delay * 2 ** min(job['attempts'] - 1, 10))
        con.execute(f'''UPDATE {table} SET status=?,error=?,next_attempt_at=?,
            lease_token='',lease_until=0,completed_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id=? AND lease_token=?''',
            (status, '' if success else 'vector_index_unavailable', 0 if success else finished + delay,
             int(success), identifier, token))
        con.commit()
        return _view(con, table, identifier)
    finally:
        con.close()


def reset_job(con, kind, identifier, *, now=None):
    """Explicit administrator retry, never steals a still-live execution lease."""
    table = TABLES[kind]
    clock = time.time() if now is None else now
    job = _view(con, table, identifier)
    if job['status'] in {'missing', 'completed'}:
        return job
    if job['status'] == 'running' and job['lease_until'] > clock:
        return {**job, 'retry_blocked': True}
    con.execute(f'''UPDATE {table} SET status='pending',attempts=0,next_attempt_at=0,
        error='',lease_token='',lease_until=0,completed_at=NULL WHERE id=?''', (identifier,))
    return _view(con, table, identifier)


def run_once(*, connection_factory=None, refresh=None, now=None, limit=5):
    factory = connection_factory or db
    clock = time.time() if now is None else now() if callable(now) else now
    con = factory()
    try:
        ready = []
        for kind, table in TABLES.items():
            rows = con.execute(f'''SELECT id,created_at FROM {table}
                WHERE (status='pending' AND next_attempt_at<=?) OR (status='running' AND lease_until<=?)
                ORDER BY created_at,id LIMIT ?''', (clock, clock, min(limit, 20))).fetchall()
            ready.extend((r['created_at'], kind, r['id']) for r in rows)
    finally:
        con.close()
    return [run_job(kind, identifier, connection_factory=factory, refresh=refresh, now=now)
            for _, kind, identifier in sorted(ready)[:min(limit, 20)]]


def job_health(connection_factory=None):
    from backend.vector_store import backend_name
    backend = backend_name()
    enabled = _worker_requested() and backend == 'milvus'
    con = (connection_factory or db)()
    try:
        counts = {s: 0 for s in ('pending', 'running', 'failed')}
        oldest = None
        for table in TABLES.values():
            for row in con.execute(f"SELECT status,COUNT(*) AS n,MIN(created_at) AS oldest FROM {table} WHERE status!='completed' GROUP BY status"):
                if row['status'] in counts:
                    counts[row['status']] += row['n']
                    oldest = min(oldest, row['oldest']) if oldest else row['oldest']
        return {**counts, 'oldest_unfinished_at': oldest, 'max_attempts': MAX_ATTEMPTS,
                'backend': backend, 'worker_enabled': enabled,
                'disabled_reason': None if enabled else 'vector_backend_disabled' if backend != 'milvus' else 'worker_disabled'}
    finally:
        con.close()


class IndexWorker:
    """One lightweight poller per app process; claims coordinate across processes."""
    def __init__(self, connection_factory=None, interval=2):
        self.factory = connection_factory or db
        self.interval = max(0.1, interval)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name='catalog-index-worker', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self, timeout=2):
        self.stop_event.set()
        self.thread.join(timeout)

    def _loop(self):
        from backend.observability import log_event
        while not self.stop_event.is_set():
            try:
                run_once(connection_factory=self.factory, limit=1)
            except Exception as exc:
                log_event('catalog.index_worker_error', error_type=type(exc).__name__)
            self.stop_event.wait(self.interval)


def _worker_requested():
    return os.getenv('CATALOG_INDEX_WORKER_ENABLED', '1').lower() not in {'0', 'false', 'off'}


def start_worker(connection_factory=None):
    from backend.vector_store import using_milvus
    # Offline/SQLite deployments deliberately have no derived Milvus index.
    # Preserve their outbox for a future backend switch without consuming retry
    # budgets or presenting an optional disabled service as a failed dependency.
    if not _worker_requested() or not using_milvus():
        return None
    return IndexWorker(connection_factory).start()
