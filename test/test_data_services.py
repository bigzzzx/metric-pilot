import asyncio
import json
import sqlite3
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend.data_services import production, production_runner, production_sql
from backend.data_services.metrics import MetricServiceQuery, execute_metric_query
from backend.data_services.production import JobInput, RunRequest, create_job, prepare_run
from backend.semantic_metrics.repository import metric_records
from backend.tools.approvals import decide_approval
from backend.tools.models import ToolExecutionContext, ToolStatus


@pytest.fixture
def platform(tmp_path, monkeypatch):
    from backend.evaluation.data_platform import data_platform_environment
    with data_platform_environment(tmp_path) as environment:
        yield environment


def query(version=None, **kwargs):
    metric = next(m for m in metric_records('eval-analyst') if m.id == 'metric-demo-paid')
    return MetricServiceQuery(metric_id=metric.id, version=version or metric.version,
        start_date='2026-08-01', end_date='2026-09-01', **kwargs)


def job():
    return create_job(JobInput(name='订单日汇总', source_asset_id='asset-orders', time_column='order_date',
        amount_column='amount', status_column='status', order_id_column='order_id'), 'eval-admin')


def begin(platform, item, key='run-1', start='2026-08-01', end='2026-08-02', actor='eval-admin'):
    _, runtime, _ = platform
    _, arguments = prepare_run(item['id'], RunRequest(start_date=start, end_date=end, request_key=key), actor)
    context = ToolExecutionContext(user_id=actor, agent_id='production-service', resource_type='asset',
                                   resource_id=item['source_asset_id'], domain_id='domain-ecommerce')
    pending = runtime.invoke('etl_execute', arguments.model_dump(mode='json'), context)
    return arguments, context, pending


def approve(platform, arguments, context, pending):
    factory, runtime, _ = platform
    identifier = pending.approval['approval_id']
    decide_approval(identifier, context.user_id, 'approved', factory)
    return runtime.invoke('etl_execute', arguments.model_dump(mode='json'), context, approval_id=identifier)


def test_metric_api_real_rows_same_sql_as_agent_and_no_model(platform, monkeypatch):
    from backend import agent
    from backend.semantic_metrics import nodes
    async def forbidden(*args, **kwargs):
        raise AssertionError('Structured API must not call a model')
    monkeypatch.setattr(agent, 'complete_structured', forbidden)
    result = execute_metric_query(query(), 'eval-analyst')
    planned = asyncio.run(nodes.metric_plan(nodes.metric_router(dict(message='2026年8月演示已支付金额',
        user_id='eval-analyst', route='data_query', history=[], conversation_id='metric-api'))))
    assert result['rows'] == [{'paid_amount': 150.0}]
    assert result['sql'] == planned['plan']['sql']
    assert result['freshness_status'] == 'unknown' and result['data_updated_at'] is None
    assert result['duration_ms'] >= 0


@pytest.mark.parametrize('mutation,status', [('stale',409), ('permission',403), ('disabled',403), ('uncertified',422)])
def test_metric_service_rejects_before_read(platform, mutation, status):
    factory, _, _ = platform
    payload = query()
    con = factory()
    if mutation == 'stale':
        payload.version = 'old-version'
    elif mutation == 'permission':
        con.execute("INSERT INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect) VALUES ('deny-api','user','eval-analyst','asset','asset-orders','query_data','deny')")
    elif mutation == 'disabled':
        con.execute("UPDATE data_sources SET enabled=0 WHERE id='local-ecommerce-demo'")
    else:
        con.execute("UPDATE catalog_metrics SET certification_status='draft' WHERE id='metric-demo-paid'")
    con.commit(); con.close()
    if mutation == 'uncertified':
        payload.version = next(m.version for m in metric_records('eval-analyst') if m.id == payload.metric_id)
    with pytest.raises(HTTPException) as error:
        execute_metric_query(payload, 'eval-analyst')
    assert error.value.status_code == status


def test_only_governed_dimension_filters_can_be_added(platform):
    with pytest.raises(HTTPException) as exc:
        execute_metric_query(query(filters=[{'dimension':'status','value':'REFUNDED'}]), 'eval-analyst')
    assert exc.value.status_code == 422
    result = execute_metric_query(query(dimensions=['date'], filters=[{'dimension':'date','operator':'>=','value':'2026-08-31'}]), 'eval-analyst')
    assert result['rows'] == [{'date':'2026-08-31','paid_amount':50.0}]


def test_job_requires_approval_then_materializes_draft_lineage_and_cache_version(platform):
    from backend.tools.cache import dependency_fingerprint
    factory, _, path = platform
    item = job()
    before = dependency_fingerprint(factory)
    arguments, context, pending = begin(platform, item)
    assert pending.status is ToolStatus.APPROVAL_REQUIRED
    physical = sqlite3.connect(path)
    assert physical.execute("SELECT 1 FROM sqlite_master WHERE name=?", (item['config']['target_table'],)).fetchone() is None
    physical.close()
    completed = approve(platform, arguments, context, pending)
    assert completed.status is ToolStatus.SUCCESS, completed.model_dump()
    physical = sqlite3.connect(path)
    assert physical.execute('SELECT stat_date,paid_amount,order_count FROM "'+item['config']['target_table']+'"').fetchall() == [('2026-08-01',100.0,1)]
    physical.close()
    con = factory()
    assert con.execute('SELECT publish_status FROM catalog_assets WHERE id=?', (item['target_asset_id'],)).fetchone()[0] == 'draft'
    assert con.execute("SELECT COUNT(*) FROM catalog_lineage_edges WHERE id LIKE 'production:%'").fetchone()[0] == 2
    assert con.execute('SELECT status FROM production_runs').fetchone()[0] == 'succeeded'
    con.close()
    assert before != dependency_fingerprint(factory)


def test_rerun_replaces_partition_and_duplicate_request_is_idempotent(platform):
    item = job()
    first = approve(platform, *begin(platform, item))
    duplicate = approve(platform, *begin(platform, item))
    assert duplicate.data['run_id'] == first.data['run_id'] and duplicate.data['idempotent']
    second = approve(platform, *begin(platform, item, key='run-2'))
    assert second.status is ToolStatus.SUCCESS
    physical = sqlite3.connect(platform[2])
    assert physical.execute('SELECT COUNT(*),SUM(paid_amount) FROM "'+item['config']['target_table']+'"').fetchone() == (1,100.0)
    physical.close()


def test_quality_failure_rolls_back_previous_result_and_records_failure(platform):
    item = job()
    assert approve(platform, *begin(platform, item)).status is ToolStatus.SUCCESS
    physical = sqlite3.connect(platform[2])
    physical.execute("UPDATE orders SET amount=NULL WHERE order_id=1")
    physical.commit(); physical.close()
    failed = approve(platform, *begin(platform, item, key='run-bad'))
    assert failed.status is ToolStatus.FAILED and failed.error.code == 'quality_failed'
    physical = sqlite3.connect(platform[2])
    assert physical.execute('SELECT paid_amount FROM "'+item['config']['target_table']+'"').fetchone()[0] == 100
    physical.close()
    assert any(r['status'] == 'failed' and r['error_code'] == 'quality_failed' for r in production.list_runs('eval-admin'))


def test_tamper_reject_and_permission_denial_never_write(platform):
    factory, runtime, _ = platform
    item = job()
    arguments, context, pending = begin(platform, item)
    identifier = pending.approval['approval_id']
    decide_approval(identifier, context.user_id, 'approved', factory)
    arguments.end_date = arguments.end_date.replace(day=3)
    result = runtime.invoke('etl_execute', arguments.model_dump(mode='json'), context, approval_id=identifier)
    assert result.error.code == 'approval_tampered'
    with pytest.raises(HTTPException) as error:
        begin(platform, item, actor='eval-analyst')
    assert error.value.status_code == 403 and 'etl.execute' in error.value.detail
    arguments, context, pending = begin(platform, item, key='rejected')
    decide_approval(pending.approval['approval_id'], context.user_id, 'rejected', factory)
    result = runtime.invoke('etl_execute', arguments.model_dump(mode='json'), context, approval_id=pending.approval['approval_id'])
    assert result.status is ToolStatus.REJECTED
    assert production.list_runs('eval-admin') == []


def test_existing_catalog_publish_flow_and_freshness_for_produced_metric(platform):
    from backend.catalog import publications
    from backend.catalog.retrieval import lineage_for_asset
    factory, _, _ = platform
    item = job()
    assert approve(platform, *begin(platform, item)).status is ToolStatus.SUCCESS
    with patch('backend.catalog.vector_index.ensure_catalog_index', return_value=False):
        assert publications.publish_asset(item['target_asset_id'], 'eval-admin', 1)['status'] == 'published'
    contract = dict(source_id='local-ecommerce-demo',asset_id=item['target_asset_id'],table=item['config']['target_table'],
        dialect='sqlite',measure_expression='SUM(paid_amount)',measure_alias='paid_amount',unit='元',time_column='stat_date',
        requires_time_range=True,dimensions={'date': {'column':'stat_date','kind':'time'}})
    con = factory()
    con.execute('''INSERT INTO catalog_metrics(id,name,display_name,domain_id,description,definition_sql,formula,grain,
        publish_status,certification_status,contract_json) VALUES ('produced','produced_sales','产物已支付金额','domain-ecommerce',
        '已支付日汇总','SUM(paid_amount)','SUM(paid_amount)','日','published','certified',?)''', (json.dumps(contract),))
    con.commit(); con.close()
    record = next(m for m in metric_records('eval-analyst') if m.id == 'produced')
    result = execute_metric_query(MetricServiceQuery(metric_id=record.id,version=record.version,
        start_date='2026-08-01',end_date='2026-08-02'), 'eval-analyst')
    assert result['rows'] == [{'paid_amount':100.0}] and result['unit'] == '元'
    assert result['freshness_status'] == 'known' and result['materialization']['data_version'] == 1
    assert len([e for e in lineage_for_asset('eval-analyst',item['target_asset_id'])['edges'] if e['id'].startswith('production:')]) >= 2


def test_http_auth_and_no_llm_service_requests(platform):
    from backend.main import app
    from backend.auth import require_user
    client = TestClient(app)
    assert client.get('/api/data-services/metrics').status_code == 401
    app.dependency_overrides[require_user] = lambda: {'id':'eval-analyst'}
    try:
        assert client.get('/api/data-services/metrics').status_code == 200
        response = client.post('/api/data-services/metrics/query',json=query().model_dump(mode='json'))
        assert response.status_code == 200 and response.json()['rows'] == [{'paid_amount':150.0}]
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_real_http_approval_roundtrip_produces_auditable_run(platform):
    from backend.main import app
    from backend.auth import require_user
    client = TestClient(app)
    app.dependency_overrides[require_user] = lambda: {'id':'eval-admin'}
    try:
        item = client.post('/api/data-services/production/jobs', json=dict(name='HTTP 汇总',source_asset_id='asset-orders',
            time_column='order_date',amount_column='amount',status_column='status',order_id_column='order_id')).json()
        pending = client.post(f'/api/data-services/production/jobs/{item["id"]}/runs', json=dict(
            start_date='2026-08-01',end_date='2026-08-02',request_key='http-acceptance')).json()
        assert pending['status'] == 'approval_required', pending
        approved = client.post(f'/api/tool-approvals/{pending["approval"]["approval_id"]}/decision',json={'decision':'approved'})
        assert approved.status_code == 200, approved.text
        result = approved.json()['tool_result']
        assert result['status'] == 'success', result
        run = client.get('/api/data-services/production/runs/' + result['data']['run_id']).json()
        assert run['status'] == 'succeeded' and run['result']['amount'] == '100.0'
        assert run['result']['parameters']['paid_value'] == 'PAID'
        assert len(run['steps']) == 4
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_metadata_commit_failure_is_recoverable_without_duplicate_data_write(platform, monkeypatch):
    item = job()
    original = production_runner._publish_artifact
    monkeypatch.setattr(production_runner, '_publish_artifact', lambda *args: (_ for _ in ()).throw(RuntimeError('metadata offline')))
    failed = approve(platform, *begin(platform, item))
    assert failed.error.code == 'metadata_reconciliation_required'
    run = production.list_runs('eval-admin')[0]
    assert run['status'] == 'needs_reconciliation'
    monkeypatch.setattr(production_runner, '_publish_artifact', original)
    assert production_runner.reconcile_run(run['id'], 'eval-admin')['status'] == 'succeeded'
    assert production.get_run(run['id'], 'eval-admin')['status'] == 'succeeded'
    physical = sqlite3.connect(platform[2])
    assert physical.execute('SELECT COUNT(*),SUM(paid_amount) FROM "'+item['config']['target_table']+'"').fetchone() == (1,100.0)
    physical.close()


def test_permission_withdrawal_after_approval_and_expiration_stop_write(platform):
    factory, runtime, _ = platform
    item = job()
    arguments, context, pending = begin(platform, item)
    decide_approval(pending.approval['approval_id'], context.user_id, 'approved', factory)
    con = factory(); con.execute("UPDATE data_sources SET enabled=0 WHERE id='local-ecommerce-demo'"); con.commit(); con.close()
    result = runtime.invoke('etl_execute', arguments.model_dump(mode='json'), context, approval_id=pending.approval['approval_id'])
    assert result.error.code == 'source_unavailable'
    con = factory(); con.execute("UPDATE data_sources SET enabled=1 WHERE id='local-ecommerce-demo'"); con.commit(); con.close()
    arguments, context, pending = begin(platform, item, key='expired')
    con = factory(); con.execute("UPDATE tool_approval_requests SET status='approved',expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (pending.approval['approval_id'],)); con.commit(); con.close()
    result = runtime.invoke('etl_execute', arguments.model_dump(mode='json'), context, approval_id=pending.approval['approval_id'])
    assert result.error.code == 'approval_expired'
    assert production.list_runs('eval-admin') == []


def test_request_key_conflict_and_run_visibility(platform):
    item = job()
    result = approve(platform, *begin(platform, item))
    assert result.status is ToolStatus.SUCCESS
    conflict = approve(platform, *begin(platform, item, end='2026-08-03'))
    assert conflict.error.code == 'idempotency_conflict'
    with pytest.raises(HTTPException) as error:
        production.get_run(result.data['run_id'], 'eval-analyst')
    assert error.value.status_code == 404


def test_postgres_writer_missing_or_cross_source_never_connects(platform, monkeypatch):
    from backend.tools.errors import ToolRuntimeError
    config = {'writer_env':'METRIC_PILOT_TEST_WRITER'}
    source = dict(source_kind='postgresql',endpoint='127.0.0.1:5433',catalog_name='allowed')
    monkeypatch.delenv(config['writer_env'], raising=False)
    with pytest.raises(ToolRuntimeError, match='服务端环境变量'):
        with production_sql.warehouse_connection(config, source): pass
    monkeypatch.setenv(config['writer_env'],'host=127.0.0.1 port=5433 dbname=another user=writer')
    with pytest.raises(ToolRuntimeError) as error:
        with production_sql.warehouse_connection(config, source): pass
    assert error.value.code == 'writer_source_mismatch'


def test_admin_reviews_external_production_and_preserves_original_actor(platform):
    from backend.tools.approvals import get_approval, list_approvals
    factory, runtime, _ = platform
    con = factory()
    con.execute("INSERT INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect) VALUES ('scheduler-etl','user','eval-analyst','platform','*','etl.execute','allow')")
    con.commit(); con.close()
    item = job()
    arguments, context, pending = begin(platform, item, actor='eval-analyst')
    identifier = pending.approval['approval_id']
    assert identifier in {r['id'] for r in list_approvals('eval-admin',connection_factory=factory)}
    decide_approval(identifier,'eval-admin','approved',factory)
    reviewed = get_approval(identifier,factory)
    assert reviewed['requested_by'] == 'eval-analyst' and reviewed['decided_by'] == 'eval-admin'
    result = runtime.invoke('etl_execute',arguments.model_dump(mode='json'),context,approval_id=identifier)
    assert result.status is ToolStatus.SUCCESS
    assert production.get_run(result.data['run_id'],'eval-admin')['actor_id'] == 'eval-analyst'


def test_cross_user_approval_is_not_extended_to_personal_sql(platform):
    from backend.tools.approvals import can_review_approval, create_approval, get_approval, list_approvals
    from backend.tools.errors import ToolApprovalError
    factory, runtime, _ = platform
    context = ToolExecutionContext(user_id='eval-analyst',agent_id='data-query-agent')
    pending = create_approval(runtime.registry.get('sql_write'),context,{'operation':'update'},
        {'summary':'personal SQL'},thread_id='personal-sql',connection_factory=factory)
    assert not can_review_approval(get_approval(pending['id'],factory),'eval-admin',factory)
    assert pending['id'] not in {r['id'] for r in list_approvals('eval-admin',connection_factory=factory)}
    with pytest.raises(ToolApprovalError):
        decide_approval(pending['id'],'eval-admin','approved',factory)


def test_account_disabled_after_approval_cannot_execute(platform):
    factory, runtime, _ = platform
    item = job()
    arguments, context, pending = begin(platform,item)
    decide_approval(pending.approval['approval_id'],'eval-admin','approved',factory)
    con = factory(); con.execute("UPDATE users SET is_active=0 WHERE id='eval-admin'"); con.commit(); con.close()
    result = runtime.invoke('etl_execute',arguments.model_dump(mode='json'),context,approval_id=pending.approval['approval_id'])
    assert result.error.code == 'permission_denied'
    assert production.list_runs('eval-admin') == []


def test_cli_acceptance_works_from_fresh_external_directory(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_data_platform.py'
    result = subprocess.run([sys.executable,str(script)],cwd=tmp_path,
        env={**os.environ,'PYTHONIOENCODING':'utf-8'},capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['passed'] == report['total'] == 27 and report['llm_calls'] == 0
