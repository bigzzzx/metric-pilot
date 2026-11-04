"""Registration and access control for a deliberately small production job catalog."""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Literal
from fastapi import HTTPException
from pydantic import Field, model_validator

from backend.catalog.publications import require_platform_admin
from backend.catalog.service import DISCOVER, QUERY_DATA, ETL_EXECUTE, can_access, is_platform_admin
from backend.core import db
from backend.query_quality.evidence import fingerprint
from backend.tools.models import StrictModel, HighRiskOperationInput, HighRiskOperationOutput

IDENTIFIER = r'^[A-Za-z_][A-Za-z0-9_]{0,62}$'


class JobInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    source_asset_id: str = Field(min_length=1, max_length=128)
    time_column: str = Field(pattern=IDENTIFIER)
    amount_column: str = Field(pattern=IDENTIFIER)
    status_column: str = Field(pattern=IDENTIFIER)
    order_id_column: str = Field(pattern=IDENTIFIER)
    paid_value: str = Field(default='PAID', min_length=1, max_length=60)
    target_namespace: str = Field(default='', pattern=r'^([A-Za-z_][A-Za-z0-9_]{0,62})?$')
    writer_env: str = Field(default='', pattern=r'^([A-Z][A-Z0-9_]{0,100})?$')


class RunRequest(StrictModel):
    start_date: date
    end_date: date
    request_key: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def bounded_range(self):
        if not 0 < (self.end_date - self.start_date).days <= 366:
            raise ValueError('使用起始日包含、结束日不包含的日期区间，最多 366 天。')
        return self


class ProductionChatPlan(StrictModel):
    job_id: str = ''
    start_date: date | None = None
    end_date: date | None = None
    clarification: str = ''


class EtlRunInput(HighRiskOperationInput, RunRequest):
    operation: Literal['execute'] = 'execute'
    job_id: str = Field(min_length=1, max_length=128)
    job_version: str = Field(min_length=1, max_length=128)


class EtlRunOutput(HighRiskOperationOutput):
    run_id: str
    job_id: str
    status: str
    idempotent: bool = False


def _job_row(row):
    item = dict(row)
    item['config'] = json.loads(item.pop('config_json'))
    return item


def get_job(job_id, user_id, *, execute=False):
    con = db()
    try:
        row = con.execute('SELECT * FROM production_jobs WHERE id=?', (job_id,)).fetchone()
        if not row or not can_access(user_id, DISCOVER, asset_id=row['source_asset_id'], connection=con):
            raise HTTPException(404, '生产任务不存在或没有 discover 权限。')
        if execute and not can_access(user_id, ETL_EXECUTE, asset_id=row['source_asset_id'], connection=con):
            raise HTTPException(403, '缺少生产任务的 etl.execute 权限，请向管理员申请。')
        if execute and not row['enabled']:
            raise HTTPException(409, '生产任务已停用。')
        result = _job_row(row)
        result['can_execute'] = can_access(user_id, ETL_EXECUTE, asset_id=row['source_asset_id'], connection=con)
        return result
    finally:
        con.close()


def list_jobs(user_id):
    con = db()
    try:
        ids = [r['id'] for r in con.execute('SELECT id FROM production_jobs ORDER BY created_at DESC,id')]
    finally:
        con.close()
    result = []
    for identifier in ids:
        try:
            result.append(get_job(identifier, user_id))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    return result


def verified_source(config, user_id):
    """Revalidate the live catalog every time, including at execution time."""
    con = db()
    try:
        row = con.execute('''SELECT a.*,s.kind AS source_kind,s.enabled AS source_enabled,
                s.endpoint,s.catalog_name,s.username FROM catalog_assets a JOIN data_sources s ON s.id=a.source_id
                WHERE a.id=?''', (config['source_asset_id'],)).fetchone()
        if not row or not all(can_access(user_id, action, asset_id=row['id'], connection=con)
                              for action in (DISCOVER, QUERY_DATA)):
            raise HTTPException(403, '缺少输入资产的 discover 或 query_data 权限。')
        if row['publish_status'] != 'published' or row['lifecycle_status'] != 'active' or not row['source_enabled']:
            raise HTTPException(409, '输入资产未发布、已归档或数据源已停用。')
        if row['source_kind'] not in {'local-sqlite', 'postgresql'}:
            raise HTTPException(422, '生产任务当前支持 SQLite 演示数仓和 PostgreSQL。')
        if row['source_kind'] == 'local-sqlite' and row['source_id'] != 'local-ecommerce-demo':
            raise HTTPException(422, 'SQLite 生产任务仅支持本地演示数仓。')
        if 'source_id' in config and (row['source_id'], row['namespace'], row['physical_name']) != (
                config['source_id'], config['source_namespace'], config['source_table']):
            raise HTTPException(409, '输入资产与任务登记时的数据源或物理表不一致，请重新登记任务。')
        columns = {c['physical_name']: dict(c) for c in con.execute(
            'SELECT * FROM catalog_columns WHERE asset_id=?', (row['id'],))}
        for name in ('time_column', 'amount_column', 'status_column', 'order_id_column'):
            column = columns.get(config[name])
            if not column or column['sensitivity'] in {'sensitive', 'restricted'}:
                raise HTTPException(403, '任务字段不存在或缺少字段 query_data 权限：' + config[name])
        if not any(t in columns[config['amount_column']]['data_type'].lower()
                   for t in ('real', 'float', 'double', 'int', 'numeric', 'decimal')):
            raise HTTPException(422, '金额字段必须是数值类型。')
        return dict(row)
    finally:
        con.close()


def create_job(payload: JobInput, user_id):
    require_platform_admin(user_id)
    config = payload.model_dump()
    source = verified_source(config, user_id)
    if source['source_kind'] == 'local-sqlite' and (payload.target_namespace or payload.writer_env):
        raise HTTPException(422, 'SQLite 使用本地演示数仓，不设置命名空间或写入凭据。')
    if source['source_kind'] == 'postgresql' and not payload.writer_env:
        raise HTTPException(422, 'PostgreSQL 任务必须指定服务端写入凭据的环境变量名。')
    identifier = str(uuid.uuid4())
    table = 'mp_sales_daily_' + identifier.replace('-', '')[:12]
    config.update(target_table=table, target_namespace=payload.target_namespace or source['namespace'],
                  source_id=source['source_id'], source_namespace=source['namespace'],
                  source_table=source['physical_name'], domain_id=source['domain_id'])
    target_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"data-agent:{source['source_id']}:{config['target_namespace']}:{table}"))
    version = fingerprint(config)
    con = db()
    try:
        con.execute('''INSERT INTO production_jobs
            (id,name,source_asset_id,target_asset_id,config_json,version,created_by) VALUES (?,?,?,?,?,?,?)''',
            (identifier, payload.name, payload.source_asset_id, target_id,
             json.dumps(config, ensure_ascii=False), version, user_id))
        con.execute('''INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json)
            VALUES (?,'production_job_registered','production_job',?,?)''',
            (user_id, identifier, json.dumps({'version': version, 'source_asset_id': payload.source_asset_id})))
        con.commit()
    finally:
        con.close()
    return get_job(identifier, user_id)


def prepare_run(job_id, request: RunRequest, user_id):
    job = get_job(job_id, user_id, execute=True)
    verified_source(job['config'], user_id)
    arguments = EtlRunInput(job_id=job_id, job_version=job['version'], **request.model_dump(),
        operation='execute', summary=f"按日重算「{job['name']}」：{request.start_date} 至 {request.end_date}（结束日不含）",
        target='.'.join(value for value in (job['config']['target_namespace'],job['config']['target_table']) if value),
        estimated_effects=['重算该日期区间的汇总数据；失败时回滚', '首次产物保存为待审核资产，不自动发布指标口径'])
    return job, arguments


def list_runs(user_id, job_id=None, limit=50):
    visible = {j['id'] for j in list_jobs(user_id)}
    con = db()
    try:
        admin = is_platform_admin(user_id, con)
        rows = con.execute('''SELECT * FROM production_runs WHERE (? IS NULL OR job_id=?)
            AND (actor_id=? OR ?) ORDER BY started_at DESC,id DESC LIMIT ?''',
            (job_id, job_id, user_id, int(admin), min(max(limit, 1), 100))).fetchall()
        return [_run_row(r) for r in rows if r['job_id'] in visible]
    finally:
        con.close()


def get_run(run_id, user_id):
    con = db()
    try:
        row = con.execute('SELECT * FROM production_runs WHERE id=?', (run_id,)).fetchone()
        if not row or (row['actor_id'] != user_id and not is_platform_admin(user_id, con)):
            raise HTTPException(404, '运行记录不存在或无权查看。')
        result = _run_row(row)
    finally:
        con.close()
    get_job(result['job_id'], user_id)
    return result


def _run_row(row):
    item = dict(row)
    item['steps'] = json.loads(item.pop('steps_json'))
    item['result'] = json.loads(item.pop('result_json'))
    return item
