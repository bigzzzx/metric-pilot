"""Local lineage ingestion, independent from the vector index worker."""
from __future__ import annotations

import os
import threading
import time
import uuid

from fastapi import HTTPException

from backend.core import db
from backend.observability import log_event


SOURCE_LEASE_SECONDS = 120


def validate_scan_interval(kind, minutes):
    if minutes and (kind != 'postgresql' or minutes < 5):
        raise HTTPException(422, '自动采集仅支持 PostgreSQL；间隔至少 5 分钟，填 0 关闭。')


def discover_source(source_id, actor_id, *, connection_factory=None, lease_token=None):
    """Read a consistent remote catalog snapshot, then compare-and-save locally."""
    from backend.catalog.sync import record_discovery
    from backend.data_sources.service import connector_from_row

    factory = connection_factory or db
    con = factory()
    try:
        con.execute('BEGIN')
        source = con.execute('SELECT * FROM data_sources WHERE id=? AND enabled=1', (source_id,)).fetchone()
        if not source:
            raise HTTPException(404, 'Enabled data source not found')
        version = con.execute('SELECT COALESCE(MAX(version),0) FROM catalog_sync_snapshots WHERE source_id=?', (source_id,)).fetchone()[0]
    finally:
        con.close()
    result = connector_from_row(source).discover()
    guard = {key: source[key] for key in ('lineage_source_revision', 'enabled', 'kind', 'auto_lineage_interval_minutes')}
    review = record_discovery(source_id, result, actor_id, connection_factory=factory, expected_source=guard,
                              expected_version=version, automatic=bool(lease_token), lease_token=lease_token)
    log_event('catalog.source_sync', source_id=source_id, kind=source['kind'], table_count=result['table_count'])
    return {**result, 'tables': [{k:v for k,v in item.items() if k!='view_definition'} for item in result['tables']], **review}


def scan_due_source(factory=db, clock=time.time):
    """At most one authorized source per tick; never change remote database settings."""
    now, token = clock(), uuid.uuid4().hex
    con = factory()
    try:
        with con:
            con.execute('BEGIN IMMEDIATE')
            source = con.execute('''SELECT s.id,s.auto_lineage_interval_minutes FROM data_sources s
                LEFT JOIN lineage_source_scan_state scan ON scan.source_id=s.id
                WHERE s.enabled=1 AND s.kind='postgresql' AND s.auto_lineage_interval_minutes>=5
                AND COALESCE(scan.next_scan_at,0)<=? ORDER BY COALESCE(scan.next_scan_at,0),s.id LIMIT 1''', (now,)).fetchone()
            if not source:
                return False
            con.execute('''INSERT INTO lineage_source_scan_state(source_id,status,lease_token,next_scan_at)
                VALUES (?,'scanning',?,?) ON CONFLICT(source_id) DO UPDATE SET
                status='scanning',lease_token=excluded.lease_token,next_scan_at=excluded.next_scan_at''',
                (source['id'], token, now+SOURCE_LEASE_SECONDS))
    finally:
        con.close()
    try:
        discover_source(source['id'], 'system:lineage', connection_factory=factory, lease_token=token)
        status, error = 'completed', ''
    except Exception as exc:
        status, error = 'failed', 'snapshot_conflict' if getattr(exc,'status_code',None)==409 else type(exc).__name__
        log_event('lineage.source_scan_failed', level=30, source_id=source['id'], error_type=error)
    con = factory()
    try:
        with con:
            con.execute('''UPDATE lineage_source_scan_state SET status=?,lease_token='',next_scan_at=?,
                last_scan_at=CURRENT_TIMESTAMP,last_error=? WHERE source_id=? AND lease_token=?''',
                (status, clock()+source['auto_lineage_interval_minutes']*60, error, source['id'], token))
    finally:
        con.close()
    return True


def worker_enabled():
    return os.getenv('LINEAGE_COLLECTOR_ENABLED', '1').lower() not in {'0','false','off'}


def status_view(con):
    groups = {row['parse_status']:row['n'] for row in con.execute(
        'SELECT parse_status,COUNT(*) AS n FROM lineage_query_history GROUP BY parse_status')}
    views = {row['status']:row['n'] for row in con.execute('''SELECT CASE WHEN generation>processed_generation
        THEN 'pending' ELSE analysis_status END AS status, COUNT(*) AS n FROM lineage_view_observations GROUP BY 1''')}
    scans = [dict(row) for row in con.execute('''SELECT s.id AS source_id,s.name,s.auto_lineage_interval_minutes,
        COALESCE(scan.status,'waiting') AS status,scan.last_scan_at,scan.last_error
        FROM data_sources s LEFT JOIN lineage_source_scan_state scan ON scan.source_id=s.id
        WHERE s.enabled=1 AND s.kind='postgresql' AND s.auto_lineage_interval_minutes>0 ORDER BY s.name''')]
    return dict(enabled=worker_enabled(), queries=groups, views=views, sources=scans)


class LineageCollector:
    def __init__(self, factory, interval=2):
        self.factory = factory
        self.interval = interval
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, name='lineage-collector', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self, timeout=2):
        self.stopped.set()
        self.thread.join(timeout)

    def _loop(self):
        from backend.lineage.capture import process_pending_query
        from backend.lineage.views import process_pending_view
        while not self.stopped.is_set():
            for consume, batch in ((process_pending_query,8),(process_pending_view,8),(scan_due_source,1)):
                try:
                    for _ in range(batch):
                        if self.stopped.is_set() or not consume(self.factory):
                            break
                except Exception as exc:
                    log_event('lineage.collector_error', level=30, stage=consume.__name__, error_type=type(exc).__name__)
            self.stopped.wait(self.interval)


def start_lineage_worker(connection_factory=None):
    return LineageCollector(connection_factory or db).start() if worker_enabled() else None
