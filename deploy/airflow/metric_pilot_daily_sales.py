"""Optional Airflow 3 DAG; each scheduled request still needs human approval.

Connection metric_pilot: host=https://your-api, login/password=dedicated platform
user; extra={"job_id":"registered-job-id"}. Credentials never enter XCom.
"""
import hashlib
from datetime import datetime, timezone
from contextlib import contextmanager

from airflow.sdk import dag, task, Connection, get_current_context, PokeReturnValue
import httpx


@contextmanager
def authenticated_client():
    connection = Connection.get('metric_pilot')
    with httpx.Client(base_url=connection.host.rstrip('/'), timeout=25) as client:
        response = client.post('/api/auth/login',json={'username':connection.login,'password':connection.password})
        response.raise_for_status()
        try:
            yield client, connection.extra_dejson['job_id']
        finally:
            client.post('/api/auth/logout')


@dag(schedule='@daily', start_date=datetime(2026,11,1,tzinfo=timezone.utc), catchup=False,
     max_active_runs=1, tags=['metric-pilot','approved-production'])
def metric_pilot_daily_sales():
    @task(retries=0)
    def request_run():
        context = get_current_context()
        with authenticated_client() as (client, job_id):
            response = client.post(f'/api/data-services/production/jobs/{job_id}/runs',json={
                'start_date':context['data_interval_start'].date().isoformat(),
                'end_date':context['data_interval_end'].date().isoformat(),
                'request_key':hashlib.sha256(context['run_id'].encode()).hexdigest()})
            response.raise_for_status()
            result = response.json()
            if result['status'] != 'approval_required':
                raise RuntimeError('Production request did not enter human approval')
            return result['approval']['approval_id']

    @task.sensor(poke_interval=30, timeout=900, mode='reschedule')
    def wait_for_human_approval_and_result(approval_id):
        with authenticated_client() as (client, _):
            response = client.get('/api/tool-approvals/'+approval_id)
            response.raise_for_status()
            approval = response.json()
        if approval['status'] in {'expired','rejected','cancelled'}:
            raise RuntimeError('Production approval '+approval['status'])
        result = approval.get('result')
        if result:
            if result['status'] != 'success':
                raise RuntimeError('Approved production execution failed; inspect Metric Pilot run logs')
            return PokeReturnValue(is_done=True,xcom_value=result['data']['run_id'])
        return PokeReturnValue(is_done=False)

    wait_for_human_approval_and_result(request_run())


metric_pilot_daily_sales()
