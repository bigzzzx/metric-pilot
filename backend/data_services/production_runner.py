"""Approval-bound job runs, result publication and observed transformation lineage."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from backend.core import db
from backend.catalog.publications import require_platform_admin
from backend.tools.approvals import arguments_hash
from backend.tools.errors import ToolRuntimeError
from backend.data_services.production import get_job, verified_source
from backend.data_services.production_sql import materialize


def _finish(run_id, status, steps, result, code=None, message=None):
    con = db()
    try:
        con.execute('''UPDATE production_runs SET status=?,steps_json=?,result_json=?,error_code=?,error_message=?,
            completed_at=? WHERE id=?''', (status, json.dumps(steps, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False), code, message, datetime.now(UTC).isoformat(), run_id))
        con.commit()
    finally:
        con.close()


def _publish_artifact(job, source, run_id, payload, result, now):
    config = job['config']
    con = db()
    try:
        con.execute('BEGIN IMMEDIATE')
        previous = con.execute('SELECT run_id FROM asset_materializations WHERE asset_id=?', (job['target_asset_id'],)).fetchone()
        if previous and previous['run_id'] == run_id:
            con.commit()
            return  # Recovering a lost acknowledgement must not increment data_version twice.
        con.execute('''INSERT INTO catalog_assets(id,source_id,namespace,physical_name,display_name,domain_id,
            warehouse_layer,normalized_stage,description,data_grain,primary_key,owner_group_id,sensitivity,
            publish_status,certification_status,source_updated_at)
            VALUES (?,?,?,?,?,?,'DWS','summary',?,'一个自然日','stat_date',?,?,'draft','validated',?)
            ON CONFLICT(id) DO UPDATE SET source_updated_at=excluded.source_updated_at,updated_at=excluded.source_updated_at''',
            (job['target_asset_id'], config['source_id'], config['target_namespace'], config['target_table'],
             job['name'] + ' · 日汇总', source['domain_id'], '由已登记任务实际执行生成的已支付订单日汇总。',
             source['owner_group_id'], source['sensitivity'], now))
        fields = [('stat_date', '统计日期', 'DATE', 'time'), ('paid_amount', '已支付订单金额', 'NUMERIC', 'measure'),
                  ('order_count', '已支付订单数', 'INTEGER', 'measure'), ('processed_at', '数据处理时间', 'TEXT', 'time'),
                  ('production_run_id', '生成该分区的运行 ID', 'TEXT', 'attribute')]
        for ordinal, (name, title, kind, role) in enumerate(fields, 1):
            con.execute('''INSERT OR IGNORE INTO catalog_columns(id,asset_id,physical_name,display_name,description,data_type,
                ordinal,semantic_role,nullable,is_primary_key,sensitivity) VALUES (?,?,?,?,?,?,?,?,0,?,?)''',
                (job['target_asset_id'] + ':' + name, job['target_asset_id'], name, title, title, kind, ordinal, role,
                 int(name == 'stat_date'), source['sensitivity']))
        con.execute('''INSERT INTO asset_materializations(asset_id,run_id,data_version,data_updated_at,range_start,range_end)
            VALUES (?,?,1,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET run_id=excluded.run_id,
            data_version=asset_materializations.data_version+1,data_updated_at=excluded.data_updated_at,
            range_start=excluded.range_start,range_end=excluded.range_end''',
            (job['target_asset_id'], run_id, now, payload.start_date.isoformat(), payload.end_date.isoformat()))
        con.execute('''INSERT INTO catalog_quality_snapshots
            (id,run_id,asset_id,row_count,score,freshness_at,completeness,validity,status,details_json)
            VALUES (?,?,?,?,1,?,1,1,'healthy',?)''', (str(uuid.uuid4()), run_id, job['target_asset_id'],
            result['total_days'], result['latest_asset_date'], json.dumps({'scope': 'materialized_date_range',
                'range_start': payload.start_date.isoformat(), 'range_end': payload.end_date.isoformat(),
                'orders': result['orders'], 'amount': result['amount'], 'checks': ['order_key', 'amount_not_null', 'aggregate_reconciliation']})))
        # These edges exist because a run committed, not because a demo graph was seeded.
        anchor = con.execute('SELECT id FROM catalog_lineage_nodes WHERE asset_id=? AND node_type=\'table\' LIMIT 1',
                             (source['id'],)).fetchone()
        source_node = anchor['id'] if anchor else f'production:asset:{source["id"]}'
        nodes = [(source_node, 'table', source['display_name'], source['id']),
                 (f'production:job:{job["id"]}', 'transformation', job['name'], source['id']),
                 (f'production:asset:{job["target_asset_id"]}', 'table', job['name'] + ' · 日汇总', job['target_asset_id'])]
        for identifier, kind, title, asset_id in nodes:
            con.execute('''INSERT OR IGNORE INTO catalog_lineage_nodes
                (id,node_type,display_name,asset_id,source_id,description,sensitivity) VALUES (?,?,?,?,?,?,?)''',
                (identifier, kind, title, asset_id, config['source_id'], '来自实际生产运行 ' + run_id, source['sensitivity']))
        for index, (left, right) in enumerate(zip(nodes, nodes[1:])):
            con.execute('''INSERT INTO catalog_lineage_edges
                (id,from_node_id,to_node_id,edge_type,transform_summary,last_run_at) VALUES (?,?,?,'transforms',?,?)
                ON CONFLICT(id) DO UPDATE SET transform_summary=excluded.transform_summary,last_run_at=excluded.last_run_at''',
                (f'production:{job["id"]}:{index}', left[0], right[0],
                 json.dumps({'run_id': run_id, 'job_version': job['version'], 'operation': 'daily_paid_sales'}, ensure_ascii=False), now))
        con.execute('''INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json)
            VALUES (?,'production_materialized','asset',?,?)''',
            (source['actor_id'], job['target_asset_id'],
             json.dumps({'run_id': run_id, 'job_id': job['id']})))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def execute_production(payload, context):
    """Only a consumed approval with exactly these arguments can enter the writer."""
    arguments = payload.model_dump(mode='json')
    digest = arguments_hash(arguments)
    con = db()
    try:
        if not con.execute('SELECT 1 FROM users WHERE id=? AND is_active=1', (context.user_id,)).fetchone():
            raise ToolRuntimeError('permission_denied', '申请人的账号已停用，不能执行该生产任务。')
        approval = con.execute('''SELECT 1 FROM tool_approval_requests WHERE tool_call_id=? AND requested_by=?
            AND tool_id='etl_execute' AND status='consumed' AND args_hash=?''',
            (context.call_id, context.user_id, digest)).fetchone()
        if not approval:
            raise ToolRuntimeError('approval_required', 'ETL 写入必须经过本次操作的人工审批。')
    finally:
        con.close()
    try:
        job = get_job(payload.job_id, context.user_id, execute=True)
        source = verified_source(job['config'], context.user_id)
    except HTTPException as exc:
        raise ToolRuntimeError('permission_denied' if exc.status_code == 403 else 'source_unavailable', str(exc.detail)) from exc
    if job['version'] != payload.job_version:
        raise ToolRuntimeError('job_version_changed', '生产任务版本已变化，必须重新审批。')
    source['actor_id'] = context.user_id
    # Cosmetic fields are approval-bound but do not change the idempotency identity.
    run_hash = arguments_hash({k: arguments[k] for k in ('job_id', 'job_version', 'start_date', 'end_date')})
    run_id = str(uuid.uuid4())
    con = db()
    try:
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute('SELECT * FROM production_runs WHERE actor_id=? AND request_key=?',
                               (context.user_id, payload.request_key)).fetchone()
        if existing:
            if existing['arguments_hash'] != run_hash:
                raise ToolRuntimeError('idempotency_conflict', '同一请求键已用于不同任务或参数，请使用新的请求键。')
            if existing['status'] == 'succeeded':
                return dict(accepted=True, message='此请求已成功执行，返回原运行记录。', run_id=existing['id'],
                            job_id=job['id'], status='succeeded', idempotent=True)
            raise ToolRuntimeError('run_already_exists', '该请求已经执行或正在运行；失败重试请使用新的请求键并重新审批。')
        con.execute('''INSERT INTO production_runs
            (id,job_id,job_version,actor_id,request_key,arguments_hash,status,start_date,end_date)
            VALUES (?,?,?,?,?,?,'running',?,?)''', (run_id, job['id'], job['version'], context.user_id,
            payload.request_key, run_hash, payload.start_date.isoformat(), payload.end_date.isoformat()))
        owned = con.execute('SELECT 1 FROM asset_materializations WHERE asset_id=?', (job['target_asset_id'],)).fetchone() is not None
        con.commit()
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise ToolRuntimeError('job_running', '该任务存在运行中或待恢复的实例，请先完成运行或恢复元数据登记。') from exc
    finally:
        con.close()
    now = datetime.now(UTC).isoformat()
    steps = [{'name': '校验任务、权限及输入字段', 'status': 'succeeded', 'at': now}]
    result = {}
    committed = False
    try:
        result = materialize(job['config'], source, payload.start_date.isoformat(), payload.end_date.isoformat(),
                             now, run_id, target_owned=owned)
        committed = True
        steps.extend([{'name': '按日重算并提交汇总', 'status': 'succeeded', 'at': datetime.now(UTC).isoformat()},
                      {'name': '主键、金额与汇总对账', 'status': 'succeeded', 'details': {k: result[k] for k in ('days', 'orders', 'amount')}}])
        _publish_artifact(job, source, run_id, payload, result, now)
        steps.append({'name': '记录资产数据版本、质量和实际血缘', 'status': 'succeeded'})
        _finish(run_id, 'succeeded', steps, result)
        return dict(accepted=True, message=f"任务已完成：{result['days']} 个日期、{result['orders']} 笔订单。产物请在数据资产中审核发布。",
                    run_id=run_id, job_id=job['id'], status='succeeded', idempotent=False)
    except Exception as exc:
        code = exc.code if isinstance(exc, ToolRuntimeError) else 'production_failed'
        message = exc.message if isinstance(exc, ToolRuntimeError) else '生产任务执行失败，请检查输入结构及写入账号权限。'
        if committed:
            code, message = 'metadata_reconciliation_required', '数据已提交，但资产运行元数据未完成，请管理员核对该运行记录后恢复登记。'
        steps.append({'name': '登记元数据' if committed else '事务执行与质量检查', 'status': 'failed', 'code': code})
        _finish(run_id, 'needs_reconciliation' if committed else 'failed', steps, result, code, message)
        raise ToolRuntimeError(code, message) from exc


def reconcile_run(run_id, user_id):
    """Finish a metadata-only commit after a recorded data commit; never rerun SQL writes."""
    from types import SimpleNamespace
    from datetime import date
    from backend.data_services.production import _run_row
    require_platform_admin(user_id)
    con = db()
    try:
        row = con.execute('SELECT * FROM production_runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, '运行记录不存在。')
        run = _run_row(row)
        if run['status'] != 'needs_reconciliation' or not run['result']:
            raise HTTPException(409, '该运行没有待恢复的元数据提交。')
    finally:
        con.close()
    job = get_job(run['job_id'], user_id)
    source = verified_source(job['config'], user_id)
    source['actor_id'] = run['actor_id']
    payload = SimpleNamespace(start_date=date.fromisoformat(run['start_date']), end_date=date.fromisoformat(run['end_date']))
    _publish_artifact(job, source, run_id, payload, run['result'], run['started_at'])
    steps = [*run['steps'], {'name': '管理员恢复资产元数据登记', 'status': 'succeeded', 'at': datetime.now(UTC).isoformat()}]
    _finish(run_id, 'succeeded', steps, run['result'])
    return {'run_id':run_id, 'status':'succeeded', 'message':'已恢复资产、质量和血缘登记，未重复执行数据写入。'}
