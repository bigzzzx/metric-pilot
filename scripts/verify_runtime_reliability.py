"""Disposable authenticated recovery and durable-index acceptance.

This script never uses a configured business database, Milvus, or a model.
The production transaction/receipt is real SQLite. Crash-window HTTP checks
construct an expired acknowledgement state; subprocess-kill tests live in the
production-recovery test suite. Index scheduling uses an injected refresh result.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(directory):
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.auth import SESSION_COOKIE, create_session
    from backend.evaluation.data_platform import data_platform_environment
    from backend import index_worker
    from backend.data_services import production_runner
    from backend.tools.errors import ToolRuntimeError

    checks = []

    def check(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
        if not passed:
            raise AssertionError(name)

    with data_platform_environment(directory) as (factory, _, warehouse), ExitStack() as stack:
        stack.enter_context(patch.object(index_worker, 'db', factory))
        index_worker.init_schema(factory)
        # Deliberately offline projection: no service credentials or network.
        stack.enter_context(patch('backend.catalog.vector_index.ensure_catalog_index', return_value=False))
        admin, analyst, anonymous = TestClient(app), TestClient(app), TestClient(app)
        for client in (admin, analyst, anonymous):
            stack.callback(client.close)
        admin.cookies.set(SESSION_COOKIE, create_session('eval-admin'))
        analyst.cookies.set(SESSION_COOKIE, create_session('eval-analyst'))
        root = '/api/data-services/production'
        indexes = '/api/catalog/evidence/index'
        check('anonymous production API rejected', anonymous.get(root + '/runs').status_code == 401)
        check('anonymous index jobs rejected', anonymous.get(indexes + '/jobs').status_code == 401)
        job_payload = dict(name='恢复验收：合成订单', source_asset_id='asset-orders', time_column='order_date',
                           amount_column='amount', status_column='status', order_id_column='order_id')
        response = admin.post(root + '/jobs', json=job_payload)
        check('admin registers isolated production job', response.status_code == 201)
        job = response.json()
        table = job['config']['target_table']
        check('analyst cannot register job', analyst.post(root + '/jobs', json=job_payload).status_code == 403)
        pending = admin.post(root + '/jobs/' + job['id'] + '/runs', json=dict(
            start_date='2026-08-01', end_date='2026-08-02', request_key='reliability-http'))
        check('request pauses for explicit approval', pending.status_code == 200 and pending.json()['status'] == 'approval_required')
        physical = sqlite3.connect(warehouse)
        check('unapproved request does not create output', physical.execute(
            'SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone() is None)
        physical.close()
        identifier = pending.json()['approval']['approval_id']
        approved = admin.post('/api/tool-approvals/' + identifier + '/decision', json={'decision': 'approved'})
        check('approved HTTP run succeeds', approved.status_code == 200 and approved.json()['tool_result']['status'] == 'success')
        run_id = approved.json()['tool_result']['data']['run_id']
        run = admin.get(root + '/runs/' + run_id).json()
        check('real transaction has golden aggregate', run['status'] == 'succeeded' and float(run['result']['amount']) == 100)
        check('private execution proof is not returned to clients', not {'snapshot_json', 'snapshot_hash', 'execution_token'}.intersection(run))
        check('anonymous recovery rejected', anonymous.post(root + '/runs/' + run_id + '/recover').status_code == 401)
        check('analyst cannot recover another actor run', analyst.post(root + '/runs/' + run_id + '/recover').status_code in (403, 404))
        con = factory()
        version_before = con.execute('SELECT data_version FROM asset_materializations WHERE asset_id=?',
                                     (job['target_asset_id'],)).fetchone()[0]
        # A durable receipt really exists. Only the platform acknowledgement is
        # changed, representing a process that died before saving final status.
        con.execute("UPDATE production_runs SET status='running',lease_expires_at='2999-01-01T00:00:00+00:00' WHERE id=?", (run_id,))
        con.commit(); con.close()
        check('fresh execution lease cannot be stolen', admin.post(root + '/runs/' + run_id + '/recover').status_code == 409)
        con = factory()
        con.execute("UPDATE production_runs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (run_id,))
        con.commit(); con.close()
        recovered = admin.post(root + '/runs/' + run_id + '/recover')
        check('expired acknowledged commit recovers over HTTP', recovered.status_code == 200 and recovered.json()['status'] == 'succeeded')
        repeated = admin.post(root + '/runs/' + run_id + '/recover')
        check('recovery is idempotent', repeated.status_code == 200 and repeated.json()['status'] == 'succeeded')
        con = factory()
        check('recovery does not increment data version twice', con.execute(
            'SELECT data_version FROM asset_materializations WHERE asset_id=?', (job['target_asset_id'],)).fetchone()[0] == version_before)
        con.close()
        physical = sqlite3.connect(warehouse)
        check('recovery never duplicates warehouse rows', physical.execute(
            'SELECT COUNT(*),SUM(paid_amount),SUM(order_count) FROM "' + table + '"').fetchone() == (1, 100.0, 1))
        physical.close()

        con = factory()
        con.execute("UPDATE production_runs SET status='running',lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (run_id,))
        con.commit(); con.close()
        with patch.object(production_runner, 'locked_warehouse', side_effect=ToolRuntimeError('writer_unavailable', 'Injected offline warehouse')):
            unknown = admin.post(root + '/runs/' + run_id + '/recover')
        check('unavailable warehouse remains explicitly unknown', unknown.status_code == 200 and unknown.json()['status'] == 'unknown')
        retry_pending = admin.post(root + '/jobs/' + job['id'] + '/runs', json=dict(
            start_date='2026-08-01', end_date='2026-08-02', request_key='blocked-unknown'))
        check('even unknown recovery never bypasses new approval', retry_pending.status_code == 200 and retry_pending.json()['status'] == 'approval_required')
        denied = admin.post('/api/tool-approvals/' + retry_pending.json()['approval']['approval_id'] + '/decision', json={'decision': 'approved'})
        check('unresolved prior outcome blocks an approved new write', denied.status_code == 200 and denied.json()['tool_result'].get('error', {}).get('code') == 'job_running')
        check('restored connection resolves original receipt without replay', admin.post(root + '/runs/' + run_id + '/recover').json()['status'] == 'succeeded')

        con = factory()
        con.execute("INSERT INTO lineage_index_jobs(id) VALUES ('reliability-budget')")
        con.execute("INSERT INTO lineage_index_jobs(id,status,attempts,lease_token,lease_until) VALUES ('reliability-crashed','running',1,'dead-process',0)")
        con.commit(); con.close()
        first = index_worker.run_job('lineage', 'reliability-budget', connection_factory=factory,
                                     refresh=lambda: False, now=100, max_attempts=2)
        check('index failure schedules bounded retry', first['status'] == 'pending' and first['attempts'] == 1 and first['next_attempt_at'] == 105)
        calls = []
        early = index_worker.run_job('lineage', 'reliability-budget', connection_factory=factory,
                                     refresh=lambda: calls.append(1) or True, now=104, max_attempts=2)
        check('retry backoff prevents early refresh', early['status'] == 'pending' and not calls)
        failed = index_worker.run_job('lineage', 'reliability-budget', connection_factory=factory,
                                      refresh=lambda: False, now=105, max_attempts=2)
        check('retry budget ends in visible failed state', failed['status'] == 'failed' and failed['attempts'] == 2)
        health = admin.get(indexes + '/jobs')
        check('admin sees durable index backlog', health.status_code == 200 and health.json()['health']['failed'] >= 1)
        check('analyst cannot reset index retry budget', analyst.post(indexes + '/retry/lineage/reliability-budget').status_code == 403)
        retried = admin.post(indexes + '/retry/lineage/reliability-budget')
        check('admin explicitly resets failed index job', retried.status_code == 200)
        current = time.time() + 1000
        results = index_worker.run_once(connection_factory=factory, refresh=lambda: True, now=current, limit=20)
        check('persistent worker resumes jobs without original HTTP request', any(
            item['id'] == 'reliability-budget' and item['status'] == 'completed' for item in results))
        con = factory()
        crashed = dict(con.execute("SELECT * FROM lineage_index_jobs WHERE id='reliability-crashed'").fetchone())
        check('expired crashed consumer is reclaimed', crashed['status'] == 'completed' and crashed['attempts'] == 2)
        con.close()
        return dict(data='isolated synthetic SQLite', passed=sum(c['passed'] for c in checks), total=len(checks),
                    checks=checks, llm_calls=0, external_service_calls=0,
                    limitations=['HTTP crash-window state is constructed after a real SQLite commit; not a subprocess kill test.',
                                 'Index scheduling uses injected refresh outcomes; this is not real Milvus acceptance.'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    from cryptography.fernet import Fernet
    os.environ.update(EMBEDDING_BACKEND='hash', VECTOR_STORE_BACKEND='sqlite', RERANK_BACKEND='none',
                      MEMORY_VECTOR_BACKEND='disabled', CATALOG_INDEX_WORKER_ENABLED='0',
                      APP_ENCRYPTION_KEY=Fernet.generate_key().decode())
    os.environ.pop('DEEPSEEK_API_KEY', None)
    with tempfile.TemporaryDirectory(prefix='metric-pilot-reliability-') as directory:
        import backend.core as core
        core.DATA_DIR = Path(directory)
        core.PLATFORM_DB = core.DATA_DIR / 'platform.db'
        core.DEMO_DB = core.DATA_DIR / 'warehouse.db'
        core.db.__defaults__ = (core.PLATFORM_DB,)
        try:
            result = verify(Path(directory))
        finally:
            from backend.checkpoint import _connection
            _connection.close()
    report = json.dumps(result, ensure_ascii=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding='utf-8')
    print(report)


if __name__ == '__main__':
    main()
