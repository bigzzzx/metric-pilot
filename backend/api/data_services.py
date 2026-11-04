"""Authenticated shared metric queries and approved data production."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from backend.auth import require_user
from backend.catalog.publications import require_platform_admin
from backend.core import db
from backend.data_services.metrics import MetricServiceQuery, execute_metric_query, service_catalog
from backend.data_services.production import JobInput, RunRequest, create_job, get_job, get_run, list_jobs, list_runs, prepare_run
from backend.tools.approval_graph import start_approval_flow
from backend.tools.models import ToolExecutionContext

router = APIRouter(prefix='/api/data-services', tags=['data-services'])


@router.get('/metrics')
def metrics(user: dict = Depends(require_user)):
    return service_catalog(user['id'])


@router.post('/metrics/query')
def query_metric(payload: MetricServiceQuery, user: dict = Depends(require_user)):
    return execute_metric_query(payload, user['id'])


@router.get('/production/jobs')
def jobs(user: dict = Depends(require_user)):
    return list_jobs(user['id'])


@router.post('/production/jobs', status_code=201)
def register_job(payload: JobInput, user: dict = Depends(require_user)):
    return create_job(payload, user['id'])


class JobState(BaseModel):
    enabled: bool


@router.patch('/production/jobs/{job_id}')
def job_state(job_id: str, payload: JobState, user: dict = Depends(require_user)):
    require_platform_admin(user['id'])
    get_job(job_id, user['id'])
    con = db()
    try:
        con.execute('UPDATE production_jobs SET enabled=? WHERE id=?', (int(payload.enabled), job_id))
        con.commit()
    finally:
        con.close()
    return get_job(job_id, user['id'])


@router.post('/production/jobs/{job_id}/runs')
def request_run(job_id: str, payload: RunRequest, user: dict = Depends(require_user)):
    job, arguments = prepare_run(job_id, payload, user['id'])
    context = ToolExecutionContext(user_id=user['id'], agent_id='production-service',
        resource_type='asset', resource_id=job['source_asset_id'], domain_id=job['config']['domain_id'])
    outcome = start_approval_flow('etl_execute', arguments.model_dump(mode='json'), context)
    if outcome.status.value == 'denied':
        raise HTTPException(403, outcome.error.model_dump(mode='json'))
    return outcome.model_dump(mode='json')


@router.get('/production/runs')
def runs(job_id: str | None = None, limit: int = Query(default=50, ge=1, le=100),
         user: dict = Depends(require_user)):
    return list_runs(user['id'], job_id, limit)


@router.get('/production/runs/{run_id}')
def run_detail(run_id: str, user: dict = Depends(require_user)):
    return get_run(run_id, user['id'])


@router.post('/production/runs/{run_id}/reconcile')
def reconcile(run_id: str, user: dict = Depends(require_user)):
    from backend.data_services.production_runner import reconcile_run
    return reconcile_run(run_id, user['id'])
