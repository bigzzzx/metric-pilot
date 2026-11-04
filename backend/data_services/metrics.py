"""Deterministic metric service; no model, routing or retrieval dependency."""
from __future__ import annotations

import time
import uuid
from fastapi import HTTPException
from pydantic import Field

from backend.core import db
from backend.query_quality.schema_lookup import SchemaLookupInput, lookup_schema
from backend.semantic_metrics.compiler import MetricRequest, compile_metric_query
from backend.semantic_metrics.models import MetricResolution
from backend.semantic_metrics.repository import metric_records
from backend.tools.models import ToolExecutionContext
from backend.tools.sql_tool import run_readonly_sql


class MetricServiceQuery(MetricRequest):
    metric_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)


def service_catalog(user_id):
    return [dict(metric_id=m.id, version=m.version, name=m.display_name, description=m.description,
                 can_query=m.can_query and m.certification_status == 'certified' and bool(m.contract and m.contract.status == 'active'),
                 unit=m.contract.unit if m.contract else '',
                 dimensions={k: d.model_dump() for k, d in m.contract.dimensions.items()} if m.contract else {},
                 requires_time_range=m.contract.requires_time_range if m.contract else True)
            for m in metric_records(user_id)]


def resolve_service_metric(user_id, metric_id, version):
    metric = next((m for m in metric_records(user_id) if m.id == metric_id), None)
    if metric is None:
        raise HTTPException(404, '指标不存在、未发布或当前没有 discover 权限。')
    if not metric.can_query:
        raise HTTPException(403, '缺少指标或底层资产的 query_data 权限，请向管理员申请。')
    if metric.version != version:
        raise HTTPException(409, '指标版本已变化，请重新获取指标目录并确认口径。')
    if metric.certification_status != 'certified' or not metric.contract or metric.contract.status != 'active':
        raise HTTPException(422, '指标尚无已认证且启用的执行契约。')
    return MetricResolution(status='matched', metrics=[metric])


def compile_verified_plan(question, resolution, request, table):
    """One compiler boundary for Agent and structured API requests."""
    contract = resolution.metrics[0].contract
    if (table.get('source_id'), table.get('namespace', ''), table.get('name'), table.get('dialect')) != (
            contract.source_id, contract.namespace, contract.table, contract.dialect):
        raise ValueError('指标契约与当前已发布表结构不一致，请重新审核指标。')
    return compile_metric_query(question, resolution, request=request,
                                columns={c['name'] for c in table['columns']})


def execute_metric_query(payload: MetricServiceQuery, user_id: str):
    started = time.perf_counter()
    query_id = str(uuid.uuid4())
    status, error_code, row_count = 'failed', None, 0
    try:
        resolution = resolve_service_metric(user_id, payload.metric_id, payload.version)
        metric = resolution.metrics[0]
        schema = lookup_schema(SchemaLookupInput(tables=[metric.contract.asset_id]),
                               ToolExecutionContext(user_id=user_id, agent_id='data-service'))
        if len(schema['tables']) != 1:
            raise HTTPException(403, '底层资产未发布、数据源已停用或缺少 query_data 权限。')
        request = MetricRequest.model_validate(payload.model_dump(exclude={'metric_id', 'version'}))
        try:
            plan = compile_verified_plan('', resolution, request, schema['tables'][0])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not plan.get('sql'):
            raise HTTPException(422, plan['answer'])
        # Recheck version and authorization immediately before the existing read-only boundary.
        resolve_service_metric(user_id, payload.metric_id, payload.version)
        rows = run_readonly_sql(plan['sql'], user_id, plan['source_id'])
        row_count, status = len(rows), 'success'
        con = db()
        try:
            materialization = con.execute('SELECT * FROM asset_materializations WHERE asset_id=?',
                                          (metric.contract.asset_id,)).fetchone()
            quality = con.execute('''SELECT status,observed_at FROM catalog_quality_snapshots
                WHERE asset_id=? ORDER BY observed_at DESC,id DESC LIMIT 1''', (metric.contract.asset_id,)).fetchone()
        finally:
            con.close()
        return dict(query_id=query_id, rows=rows, sql=plan['sql'], metric_id=metric.id, version=metric.version,
                    definition=metric.description, unit=metric.contract.unit, source_id=metric.contract.source_id,
                    asset_id=metric.contract.asset_id, time_column=metric.contract.time_column,
                    row_limit=100, data_updated_at=materialization['data_updated_at'] if materialization else None,
                    freshness_status='known' if materialization else 'unknown',
                    materialization=dict(materialization) if materialization else None,
                    quality=dict(quality) if quality else None,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2))
    except HTTPException as exc:
        error_code = str(exc.status_code)
        raise
    finally:
        con = db()
        try:
            con.execute('''INSERT INTO metric_service_queries
                (id,user_id,metric_id,metric_version,status,row_count,duration_ms,error_code) VALUES (?,?,?,?,?,?,?,?)''',
                (query_id, user_id, payload.metric_id, payload.version, status, row_count,
                 round((time.perf_counter() - started) * 1000, 2), error_code))
            con.commit()
        finally:
            con.close()
