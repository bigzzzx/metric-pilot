"""Real HTTP, approval, SQL and latency acceptance in isolated synthetic warehouses."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def prepare_postgres(factory, warehouse, dsn):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from backend.core import secrets
    settings = conninfo_to_dict(dsn)
    if settings.get('dbname') != 'metric_pilot_acceptance':
        raise ValueError('PostgreSQL acceptance writes are restricted to the dedicated metric_pilot_acceptance database')
    # This script is explicitly for an isolated disposable database, never an attached business source.
    with psycopg.connect(dsn) as pg:
        pg.execute('CREATE SCHEMA mp_acceptance')
        pg.execute('''CREATE TABLE mp_acceptance.orders(order_id INTEGER PRIMARY KEY,user_id INTEGER,
            product_id INTEGER,amount NUMERIC,status TEXT,order_date DATE,quantity INTEGER)''')
        local = factory(warehouse)
        rows = local.execute('SELECT * FROM orders ORDER BY order_id').fetchall()
        local.close()
        with pg.cursor() as cursor:
            cursor.executemany('INSERT INTO mp_acceptance.orders VALUES (%s,%s,%s,%s,%s,%s,%s)', [tuple(row) for row in rows])
        pg.execute(sql.SQL('CREATE ROLE mp_acceptance_reader LOGIN PASSWORD {}').format(sql.Literal(settings['password'])))
        pg.execute('ALTER ROLE mp_acceptance_reader SET default_transaction_read_only=on')
        pg.execute('GRANT USAGE ON SCHEMA mp_acceptance TO mp_acceptance_reader')
        pg.execute('GRANT SELECT ON mp_acceptance.orders TO mp_acceptance_reader')
    con = factory()
    con.execute('''INSERT INTO data_sources(id,name,kind,endpoint,catalog_name,auth_type,credential,enabled,username)
        VALUES ('acceptance-pg','隔离 PostgreSQL 验收','postgresql',?,'metric_pilot_acceptance','read_only_account',?,1,'mp_acceptance_reader')''',
        (settings['host'] + ':' + settings.get('port', '5432'), secrets().encrypt(settings['password'].encode()).decode()))
    con.execute("UPDATE catalog_assets SET source_id='acceptance-pg',namespace='mp_acceptance' WHERE id='asset-orders'")
    for row in con.execute("SELECT id,contract_json FROM catalog_metrics WHERE contract_json!='{}'").fetchall():
        contract = json.loads(row['contract_json'])
        if contract.get('asset_id') == 'asset-orders':
            contract.update(source_id='acceptance-pg', namespace='mp_acceptance', dialect='postgresql')
            con.execute('UPDATE catalog_metrics SET contract_json=? WHERE id=?', (json.dumps(contract), row['id']))
    con.commit(); con.close()


def validate(directory, dsn=None):
    import sqlite3
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from backend.auth import SESSION_COOKIE, create_session
    from backend.main import app
    from backend.evaluation.data_platform import data_platform_environment
    from backend.tools.cache import dependency_fingerprint
    from backend.semantic_metrics import nodes
    from backend import agent

    checks = []
    def check(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})
        if not passed:
            raise AssertionError(name)

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {'APP_ENCRYPTION_KEY':Fernet.generate_key().decode(),
            'EMBEDDING_BACKEND':'hash', 'RERANK_BACKEND':'none', 'VECTOR_STORE_BACKEND':'sqlite'}))
        factory, _, warehouse = stack.enter_context(data_platform_environment(directory))
        if dsn:
            prepare_postgres(factory, warehouse, dsn)
            stack.enter_context(patch.dict(os.environ, {'METRIC_PILOT_ACCEPTANCE_WRITER':dsn}))
        # Authentication is real cookie/session lookup; no user-dependency override.
        analyst, admin = TestClient(app), TestClient(app)
        analyst.cookies.set(SESSION_COOKIE, create_session('eval-analyst'))
        admin.cookies.set(SESSION_COOKIE, create_session('eval-admin'))
        check('anonymous API is rejected', TestClient(app).get('/api/data-services/metrics').status_code == 401)
        catalog = analyst.get('/api/data-services/metrics').json()
        metric = next(item for item in catalog if item['metric_id'] == 'metric-demo-paid')
        payload = dict(metric_id=metric['metric_id'],version=metric['version'],start_date='2026-08-01',end_date='2026-09-01')
        result = analyst.post('/api/data-services/metrics/query',json=payload)
        check('authenticated structured metric API', result.status_code == 200)
        check('golden paid amount', Decimal(str(result.json()['rows'][0]['paid_amount'])) == 150)
        check('unknown source freshness is explicit', result.json()['freshness_status'] == 'unknown')
        import asyncio
        with patch.object(agent, 'complete_structured', side_effect=AssertionError('LLM must not be used')):
            planned = asyncio.run(nodes.metric_plan(nodes.metric_router(dict(message='2026年8月演示已支付金额',
                user_id='eval-analyst',route='data_query',history=[],conversation_id='service-acceptance'))))
        check('Agent and API share the same SQL', planned['plan']['sql'] == result.json()['sql'])
        check('old version rejected', analyst.post('/api/data-services/metrics/query',json={**payload,'version':'stale'}).status_code == 409)
        check('unregistered filter rejected', analyst.post('/api/data-services/metrics/query',json={**payload,
            'filters':[{'dimension':'status','value':'REFUNDED'}]}).status_code == 422)
        times, service_times = [], []
        for _ in range(30):
            started = time.perf_counter()
            response = analyst.post('/api/data-services/metrics/query',json=payload)
            check_name = response.status_code == 200 and Decimal(str(response.json()['rows'][0]['paid_amount'])) == 150
            if not check_name:
                raise AssertionError('metric benchmark correctness')
            times.append((time.perf_counter()-started)*1000)
            service_times.append(response.json()['duration_ms'])
        job_payload = dict(name='验收：已支付订单日汇总',source_asset_id='asset-orders',time_column='order_date',
            amount_column='amount',status_column='status',order_id_column='order_id')
        if dsn:
            job_payload['writer_env'] = 'METRIC_PILOT_ACCEPTANCE_WRITER'
        job_response = admin.post('/api/data-services/production/jobs',json=job_payload)
        check('job registered', job_response.status_code == 201)
        job = job_response.json()
        check('analyst cannot register production', analyst.post('/api/data-services/production/jobs',json=job_payload).status_code == 403)
        before = dependency_fingerprint(factory)
        def request_and_decide(key, decision='approved'):
            pending = admin.post(f'/api/data-services/production/jobs/{job["id"]}/runs',json=dict(
                start_date='2026-08-01',end_date='2026-08-02',request_key=key))
            check('run requires approval: '+key, pending.json().get('status') == 'approval_required')
            identifier = pending.json()['approval']['approval_id']
            return admin.post(f'/api/tool-approvals/{identifier}/decision',json={'decision':decision}).json()['tool_result']
        rejected = request_and_decide('rejection','rejected')
        check('rejection does not execute', rejected['status'] == 'rejected' and admin.get('/api/data-services/production/runs').json() == [])
        started = time.perf_counter()
        first = request_and_decide('first')
        production_ms = round((time.perf_counter()-started)*1000,2)
        check('approved run succeeds', first['status'] == 'success')
        run = admin.get('/api/data-services/production/runs/'+first['data']['run_id']).json()
        check('golden daily amount/count', Decimal(run['result']['amount']) == 100 and run['result']['orders'] == 1)
        check('successful steps persisted', len(run['steps']) == 4 and run['status'] == 'succeeded')
        duplicate = request_and_decide('first')
        check('duplicate key cannot write again', duplicate['data']['run_id'] == first['data']['run_id'] and duplicate['data']['idempotent'])
        rerun = request_and_decide('partition-rerun')
        check('partition rerun succeeds', rerun['status'] == 'success')
        asset = admin.get('/api/catalog/assets/'+job['target_asset_id']).json()
        check('new output remains draft', asset['publish_status'] == 'draft')
        with patch('backend.catalog.vector_index.ensure_catalog_index', return_value=False):
            published = admin.post('/api/catalog/admin/assets/'+job['target_asset_id']+'/publish?version=1')
        check('existing review/publication works', published.status_code == 200)
        graph = analyst.get('/api/catalog/assets/'+job['target_asset_id']+'/lineage').json()
        check('observed production lineage', len([edge for edge in graph['edges'] if edge['id'].startswith('production:')]) == 2)
        con = factory()
        materialized = con.execute('SELECT * FROM asset_materializations WHERE asset_id=?',(job['target_asset_id'],)).fetchone()
        check('materialization data version increments only for real writes', materialized['data_version'] == 2)
        con.close()
        check('data update invalidates existing cache identity', before != dependency_fingerprint(factory))
        if dsn:
            import psycopg
            with psycopg.connect(dsn) as pg:
                pg.execute('UPDATE mp_acceptance.orders SET amount=NULL WHERE order_id=1')
        else:
            pg = sqlite3.connect(warehouse)
            pg.execute('UPDATE orders SET amount=NULL WHERE order_id=1'); pg.commit(); pg.close()
        failed = request_and_decide('quality-failure')
        check('quality failure is explicit', failed['error']['code'] == 'quality_failed')
        from backend.tools.sql_tool import run_readonly_sql
        table = '.'.join('"'+part+'"' for part in (job['config']['target_namespace'],job['config']['target_table']) if part)
        rows = run_readonly_sql('SELECT SUM(paid_amount) AS amount,SUM(order_count) AS orders FROM '+table,
                                 'eval-analyst', job['config']['source_id'])
        check('failed rerun preserves previous committed data', Decimal(str(rows[0]['amount'])) == 100 and rows[0]['orders'] == 1)
        return dict(backend='postgresql' if dsn else 'sqlite',data='isolated synthetic fixtures',llm_calls=0,
            passed=sum(item['passed'] for item in checks),total=len(checks),checks=checks,
            metric_http_ms=dict(samples=len(times),p50=round(statistics.median(times),2),p95=round(sorted(times)[28],2)),
            metric_service_ms=dict(p50=round(statistics.median(service_times),2),p95=round(sorted(service_times)[28],2)),
            approved_production_roundtrip_ms=production_ms)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true',help='Requires disposable metric_pilot_acceptance DB and METRIC_PILOT_TEST_PG_DSN')
    args = parser.parse_args()
    dsn = os.environ.get('METRIC_PILOT_TEST_PG_DSN') if args.postgres else None
    if args.postgres and not dsn:
        raise SystemExit('METRIC_PILOT_TEST_PG_DSN is required; credentials are never printed')
    with tempfile.TemporaryDirectory(prefix='metric-pilot-platform-') as directory:
        # Set paths BEFORE importing the app so even an accidentally unpatched
        # consumer cannot read the regular platform DB. Also exercises clean CI.
        import backend.core as core
        core.DATA_DIR = Path(directory)
        core.PLATFORM_DB = core.DATA_DIR / 'platform.db'
        core.DEMO_DB = core.DATA_DIR / 'warehouse.db'
        core.db.__defaults__ = (core.PLATFORM_DB,)
        try:
            result = validate(Path(directory), dsn)
        finally:
            from backend.checkpoint import _connection
            _connection.close()
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__ == '__main__':
    main()
