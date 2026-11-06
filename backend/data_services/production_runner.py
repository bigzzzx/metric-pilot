"""Approval-bound job runs, result publication and observed transformation lineage."""
from __future__ import annotations

import json
import sqlite3
import uuid
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from backend.core import db
from backend.catalog.publications import require_platform_admin
from backend.tools.approvals import arguments_hash
from backend.tools.errors import ToolRuntimeError
from backend.data_services.production import get_job, verified_source
from backend.data_services.production_sql import materialize, locked_warehouse, read_receipt

LEASE_SECONDS = 120
ACTIVE_STATUSES = ('running', 'needs_reconciliation', 'recovery_required', 'unknown')


def _now():
    return datetime.now(UTC)


def _fault_hook(stage, run_id):
    """No-op injection seam for isolated process-kill tests, not an environment switch."""


def _guard(run_id, token):
    con = db()
    try:
        row = con.execute('SELECT status,execution_token,lease_expires_at FROM production_runs WHERE id=?', (run_id,)).fetchone()
        if not row or row['status'] != 'running' or row['execution_token'] != token or not row['lease_expires_at'] or row['lease_expires_at'] <= _now().isoformat():
            raise ToolRuntimeError('execution_fenced', '此运行的执行租约已失效，旧执行进程不能继续写入。')
    finally:
        con.close()


@contextmanager
def _heartbeat(run_id, token):
    stop = threading.Event()
    def beat():
        while not stop.wait(10):
            con = None
            try:
                con = db()
                now = _now()
                con.execute("UPDATE production_runs SET heartbeat_at=?,lease_expires_at=? WHERE id=? AND execution_token=? AND status='running' AND lease_expires_at>?",
                    (now.isoformat(), (now+timedelta(seconds=LEASE_SECONDS)).isoformat(),run_id,token,now.isoformat()))
                con.commit()
            except Exception:
                # A DB outage must not grant a longer in-memory lease.
                pass
            finally:
                if con is not None: con.close()
    thread = threading.Thread(target=beat,daemon=True,name='production-heartbeat')
    thread.start()
    try: yield
    finally:
        stop.set(); thread.join(timeout=2)


def mark_expired_runs(*, connection_factory=None):
    """Cheap startup/poll scan; expiry is UNKNOWN, never proof of a rollback."""
    con = (connection_factory or db)()
    try:
        changed = con.execute("""UPDATE production_runs SET status='recovery_required',
            error_code='execution_lease_expired',error_message='执行心跳已过期，请管理员核对提交凭证；未自动重试。'
            WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<=?)""", (_now().isoformat(),)).rowcount
        con.commit()
        return {'marked': changed}
    finally: con.close()


def _finish(run_id, status, steps, result, code=None, message=None, *, token=None):
    con = db()
    try:
        sql = '''UPDATE production_runs SET status=?,steps_json=?,result_json=?,error_code=?,error_message=?,
            completed_at=?,lease_expires_at=NULL WHERE id=?'''
        params = [status,json.dumps(steps,ensure_ascii=False),json.dumps(result,ensure_ascii=False),
                  code,message,_now().isoformat(),run_id]
        if token is not None:
            sql += " AND execution_token=? AND status='running'"
            params.append(token)
        changed = con.execute(sql,params).rowcount
        con.commit()
        return bool(changed)
    finally:
        con.close()


def _publish_artifact(job, source, run_id, payload, result, now, *, token=None, recovering=False):
    config = job['config']
    con = db()
    try:
        con.execute('BEGIN IMMEDIATE')
        owner = con.execute('SELECT status,execution_token FROM production_runs WHERE id=?',(run_id,)).fetchone()
        allowed = ACTIVE_STATUSES if recovering else ('running',)
        if not token or not owner or owner['execution_token']!=token or owner['status'] not in allowed:
            raise ToolRuntimeError('execution_fenced','旧执行者不能覆盖已恢复或已结束运行的资产元数据。')
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
    token = str(uuid.uuid4())
    now = _now().isoformat()
    snapshot = {'format': 1, 'job': {k:job[k] for k in ('id','name','source_asset_id','target_asset_id','version','config')},
        'source_identity': {k:source[k] for k in ('source_id','source_kind','endpoint','catalog_name','namespace','physical_name')},
        'arguments': arguments, 'processed_at': now}
    snapshot_hash = arguments_hash(snapshot)
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
            (id,job_id,job_version,actor_id,request_key,arguments_hash,status,start_date,end_date,
             snapshot_json,snapshot_hash,execution_token,heartbeat_at,lease_expires_at)
            VALUES (?,?,?,?,?,?,'running',?,?,?,?,?,?,?)''', (run_id, job['id'], job['version'], context.user_id,
            payload.request_key,run_hash,payload.start_date.isoformat(),payload.end_date.isoformat(),
            json.dumps(snapshot,ensure_ascii=False,sort_keys=True),snapshot_hash,token,now,
            (_now()+timedelta(seconds=LEASE_SECONDS)).isoformat()))
        owned = con.execute('SELECT 1 FROM asset_materializations WHERE asset_id=?', (job['target_asset_id'],)).fetchone() is not None
        con.commit()
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise ToolRuntimeError('job_running', '该任务存在运行中或待恢复的实例，请先完成运行或恢复元数据登记。') from exc
    finally:
        con.close()
    _fault_hook('after_run_registered', run_id)
    steps = [{'name': '校验任务、权限及输入字段', 'status': 'succeeded', 'at': now}]
    result = {}
    committed = False
    try:
        with _heartbeat(run_id, token):
            result = materialize(job['config'], source, payload.start_date.isoformat(), payload.end_date.isoformat(),
                now,run_id,target_owned=owned,execution_proof=dict(snapshot_hash=snapshot_hash,
                    arguments_hash=run_hash,execution_token=token),execution_guard=lambda:_guard(run_id,token))
            committed = True
            _fault_hook('after_warehouse_commit',run_id)
            steps.extend([{'name': '按日重算并提交汇总', 'status': 'succeeded', 'at': _now().isoformat()},
                          {'name': '主键、金额与汇总对账', 'status': 'succeeded', 'details': {k: result[k] for k in ('days', 'orders', 'amount')}}])
            _publish_artifact(job, source, run_id, payload, result, now,token=token)
            _fault_hook('after_metadata_publish',run_id)
            steps.append({'name': '记录资产数据版本、质量和实际血缘', 'status': 'succeeded'})
            if not _finish(run_id,'succeeded',steps,result,token=token):
                raise ToolRuntimeError('execution_fenced','数据可能已提交，但运行状态已由恢复流程接管；请核对运行记录，不能确认本执行成功。')
        from backend.lineage.capture import safe_capture_production
        safe_capture_production(run_id,connection_factory=db)
        return dict(accepted=True, message=f"任务已完成：{result['days']} 个日期、{result['orders']} 笔订单。产物请在数据资产中审核发布。",
                    run_id=run_id, job_id=job['id'], status='succeeded', idempotent=False)
    except Exception as exc:
        code = exc.code if isinstance(exc, ToolRuntimeError) else 'production_failed'
        message = exc.message if isinstance(exc, ToolRuntimeError) else '生产任务执行失败，请检查输入结构及写入账号权限。'
        if committed and code != 'execution_fenced':
            code, message = 'metadata_reconciliation_required', '数据已提交，但资产运行元数据未完成，请管理员核对该运行记录后恢复登记。'
        steps.append({'name': '登记元数据' if committed else '事务执行与质量检查', 'status': 'failed', 'code': code})
        if committed:
            _finish(run_id,'needs_reconciliation',steps,result,code,message,token=token)
        else:
            # commit() may have succeeded remotely even when its acknowledgement was lost.
            # Only a fresh transaction holding the same warehouse lock can prove absence.
            try:
                with locked_warehouse(job['config'],source) as con:
                    receipt = read_receipt(con,job['config'],source,run_id)
                    if receipt:
                        result = _validated_receipt(receipt,dict(snapshot_hash=snapshot_hash,
                            arguments_hash=run_hash,execution_token=token),run_id)
                        code,message = 'metadata_reconciliation_required','仓库提交凭证确认数据已提交，请恢复元数据登记；不会重复写入。'
                        _finish(run_id,'needs_reconciliation',steps,result,code,message,token=token)
                    else:
                        _finish(run_id,'failed',steps,result,code,message,token=token)
            except Exception:
                code,message = 'production_outcome_unknown','无法确认仓库是否提交，已阻止重试，请管理员核对提交凭证。'
                _finish(run_id,'unknown',steps,result,code,message,token=token)
        from backend.lineage.capture import safe_capture_production
        safe_capture_production(run_id,connection_factory=db)
        raise ToolRuntimeError(code, message) from exc


def reconcile_run(run_id, user_id):
    """Finish a metadata-only commit after a recorded data commit; never rerun SQL writes."""
    return recover_run(run_id,user_id)


def _validated_receipt(receipt, run, run_id):
    if receipt['run_id'] != run_id or any(receipt[k] != run[k] for k in
            ('snapshot_hash','arguments_hash','execution_token')):
        raise ToolRuntimeError('receipt_mismatch','提交凭证与已审批执行快照不一致，不能自动恢复。')
    result = json.loads(receipt['result_json'])
    if not isinstance(result,dict) or not {'days','orders','amount','total_days','latest_asset_date','parameters','sql_template'} <= result.keys() or result['parameters'].get('production_run_id') != run_id:
        raise ToolRuntimeError('receipt_mismatch','提交凭证结果不完整，不能自动恢复。')
    return result


def _unknown(run_id, token, message):
    con = db()
    try:
        changed = con.execute("""UPDATE production_runs SET status='unknown',error_code='production_outcome_unknown',
            error_message=?,lease_expires_at=NULL WHERE id=? AND execution_token=?
            AND status IN ('running','needs_reconciliation','recovery_required','unknown')""",(message,run_id,token)).rowcount
        if changed:
            # System classification, not a new business write or approval.
            con.execute("""INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json)
                VALUES ('system','production_outcome_unknown','production_run',?,'{}')""",(run_id,))
        else:
            current=con.execute('SELECT status FROM production_runs WHERE id=?',(run_id,)).fetchone()
            if current:return dict(run_id=run_id,status=current['status'],message='此运行已由其他执行或恢复请求处理。')
        con.commit()
    finally: con.close()
    return dict(run_id=run_id,status='unknown',message=message)


def recover_run(run_id,user_id):
    """No business SQL replay: prove commit or exclude every old writer before unlock."""
    from datetime import date
    from types import SimpleNamespace
    require_platform_admin(user_id)
    con = db()
    try:
        row = con.execute('SELECT * FROM production_runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise HTTPException(404,'运行记录不存在。')
        run = dict(row)
        if run['status'] not in ACTIVE_STATUSES:
            return dict(run_id=run_id,status=run['status'],message='此运行已结束，无需再次恢复。')
        if run['status']=='running' and run['lease_expires_at'] and run['lease_expires_at']>_now().isoformat():
            raise HTTPException(409,'执行租约仍有效，不能认定进程中断，请稍后检查。')
    finally: con.close()
    try:
        snapshot = json.loads(run['snapshot_json'])
        if snapshot.get('format') != 1 or arguments_hash(snapshot)!=run['snapshot_hash']:
            return _unknown(run_id,run['execution_token'],'旧运行没有可验证的执行快照，请管理员人工核对，未解除任务阻塞。')
        job = snapshot['job']
        if job['id'] != run['job_id'] or job['version'] != run['job_version']:
            raise ValueError('snapshot identity')
        args = snapshot['arguments']
        if arguments_hash({k:args[k] for k in ('job_id','job_version','start_date','end_date')}) != run['arguments_hash']:
            raise ValueError('snapshot arguments')
        source = verified_source(job['config'],user_id)
        if any(source[k] != v for k,v in snapshot['source_identity'].items()):
            raise ValueError('source identity changed')
        source['actor_id'] = run['actor_id']
        # Holding the barrier excludes a process paused inside its write transaction.
        # A process paused BEFORE it must pass _guard AFTER acquiring it.
        with locked_warehouse(job['config'],source) as physical:
            con = db()
            try:
                current = con.execute('SELECT * FROM production_runs WHERE id=?',(run_id,)).fetchone()
                if current['status'] not in ACTIVE_STATUSES:
                    return dict(run_id=run_id,status=current['status'],message='此运行已由其他恢复请求处理。')
                if current['execution_token'] != run['execution_token']:
                    raise ValueError('execution owner changed')
            finally: con.close()
            receipt = read_receipt(physical,job['config'],source,run_id)
            if receipt is None:
                # Rotate the token WHILE holding the exact same warehouse barrier.
                # A delayed original executor cannot pass its post-lock guard afterwards.
                con = db()
                try:
                    con.execute("""UPDATE production_runs SET status='failed',execution_token=?,
                        lease_expires_at=NULL,completed_at=?,error_code='interrupted_not_committed',
                        error_message='已持有仓库互斥锁核对，无提交凭证；旧执行已隔离。重试需重新审批。'
                        WHERE id=? AND execution_token=? AND status IN ('running','needs_reconciliation','recovery_required','unknown')""",
                        (str(uuid.uuid4()),_now().isoformat(),run_id,run['execution_token']))
                    con.execute("""INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json)
                        VALUES (?,'production_recovered_no_commit','production_run',?,'{}')""",(user_id,run_id))
                    con.commit()
                finally: con.close()
                return dict(run_id=run_id,status='failed',message='已确认本次未提交，旧执行已隔离；如需重试，请重新申请审批。')
            result = _validated_receipt(receipt,run,run_id)
            payload = SimpleNamespace(start_date=date.fromisoformat(args['start_date']),end_date=date.fromisoformat(args['end_date']))
            _publish_artifact(job,source,run_id,payload,result,snapshot['processed_at'],token=run['execution_token'],recovering=True)
            steps = [*json.loads(run['steps_json']),dict(name='管理员按事务提交凭证恢复元数据（未重写业务数据）',status='succeeded',at=_now().isoformat())]
            _finish(run_id,'succeeded',steps,result)
            con = db()
            try:
                con.execute("""INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json)
                    VALUES (?,'production_recovered_commit','production_run',?,'{}')""",(user_id,run_id))
                con.commit()
            finally: con.close()
        from backend.lineage.capture import safe_capture_production
        safe_capture_production(run_id,connection_factory=db)
        return dict(run_id=run_id,status='succeeded',message='已按提交凭证恢复资产、质量与血缘，未重复执行数据写入。')
    except Exception:
        return _unknown(run_id,run['execution_token'],'提交状态无法确认（连接、互斥锁、执行快照或提交凭证异常），任务仍阻塞；请管理员核对后再恢复。')
