"""Isolated crash/recovery tests: no live warehouse, provider or platform data."""
import json
import multiprocessing
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend.data_services import production, production_runner as runner, production_sql
from backend.tools.errors import ToolRuntimeError
from backend.tools.models import ToolExecutionContext, ToolStatus
from test.test_data_services import platform, job, begin, approve


def expire(factory, run_id):
    con=factory()
    con.execute("UPDATE production_runs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(run_id,))
    con.commit();con.close()


def raw(factory, run_id):
    con=factory()
    try:return dict(con.execute('SELECT * FROM production_runs WHERE id=?',(run_id,)).fetchone())
    finally:con.close()


def _crash_worker(directory, arguments, context, stop_stage, signal):
    """Separate OS process resumes an already approved synthetic operation."""
    from backend.catalog import publications
    from backend.data_services.production import EtlRunInput
    directory=Path(directory)
    def factory():
        con=sqlite3.connect(directory/'platform.db');con.row_factory=sqlite3.Row
        return con
    def halt(stage,run_id):
        if stage==stop_stage:
            signal.send(run_id)
            threading.Event().wait()
    with patch.object(runner,'db',factory),patch.object(production,'db',factory),patch.object(publications,'db',factory),patch.object(production_sql,'DEMO_DB',directory/'warehouse.db'),patch.object(runner,'_fault_hook',halt),patch.object(production_sql,'_fault_hook',halt):
        runner.execute_production(EtlRunInput.model_validate(arguments),ToolExecutionContext.model_validate(context))


@pytest.mark.parametrize('stage,committed',[
    ('after_run_registered',False),('before_warehouse_commit',False),('after_warehouse_commit',True),('after_metadata_publish',True)])
def test_real_process_termination_is_recovered_without_sql_replay(platform,stage,committed):
    factory,_,warehouse=platform
    item=job()
    arguments,context,pending=begin(platform,item)
    con=factory()
    con.execute("UPDATE tool_approval_requests SET status='consumed' WHERE id=?",(pending.approval['approval_id'],))
    con.commit();con.close()
    ctx=multiprocessing.get_context('spawn')
    parent,child=ctx.Pipe(duplex=False)
    proc=ctx.Process(target=_crash_worker,args=(str(warehouse.parent),arguments.model_dump(mode='json'),context.model_dump(mode='json'),stage,child))
    proc.start();child.close()
    try:
        assert parent.poll(45),f'Child did not reach {stage}; exit={proc.exitcode}'
        run_id=parent.recv()
    finally:
        if proc.is_alive():proc.terminate()
        proc.join(10);parent.close()
    assert proc.exitcode is not None and proc.exitcode!=0
    assert raw(factory,run_id)['status']=='running'
    with pytest.raises(HTTPException) as error:runner.recover_run(run_id,'eval-admin')
    assert error.value.status_code==409
    expire(factory,run_id)
    assert runner.mark_expired_runs()['marked']==1
    recovered=runner.recover_run(run_id,'eval-admin')
    assert recovered['status']==('succeeded' if committed else 'failed')
    physical=sqlite3.connect(warehouse)
    if committed:
        assert physical.execute('SELECT SUM(paid_amount),COUNT(*) FROM "'+item['config']['target_table']+'"').fetchone()==(100.0,1)
        con=factory();version=con.execute('SELECT data_version FROM asset_materializations').fetchone()[0];con.close()
        assert version==1
        assert runner.recover_run(run_id,'eval-admin')['status']=='succeeded'
        con=factory();assert con.execute('SELECT data_version FROM asset_materializations').fetchone()[0]==1;con.close()
    else:
        assert physical.execute('SELECT name FROM sqlite_master WHERE name=?',(item['config']['target_table'],)).fetchone() is None
        assert raw(factory,run_id)['error_code']=='interrupted_not_committed'
    physical.close()


class SimulatedDeath(BaseException):pass


def interrupted(platform,monkeypatch,*,stage='after_warehouse_commit',start='2026-08-01',end='2026-08-02'):
    item=job()
    def fail(point,rid):
        if point==stage:raise SimulatedDeath()
    with monkeypatch.context() as m:
        m.setattr(runner,'_fault_hook',fail)
        with pytest.raises(SimulatedDeath):approve(platform,*begin(platform,item,start=start,end=end))
    run_id=production.list_runs('eval-admin')[0]['id'];expire(platform[0],run_id)
    return item,run_id


def test_empty_result_has_transaction_receipt_and_recovers(platform,monkeypatch):
    item,rid=interrupted(platform,monkeypatch,start='2026-08-02',end='2026-08-03')
    assert runner.recover_run(rid,'eval-admin')['status']=='succeeded'
    result=production.get_run(rid,'eval-admin')['result']
    assert result['days']==0 and result['orders']==0
    physical=sqlite3.connect(platform[2]);assert physical.execute('SELECT COUNT(*) FROM "'+item['config']['target_table']+'_receipts"').fetchone()[0]==1;physical.close()


def test_unavailable_warehouse_remains_unknown_and_blocks_new_run(platform,monkeypatch):
    item,rid=interrupted(platform,monkeypatch)
    @contextmanager
    def offline(*args):raise OSError('synthetic network outage');yield
    with monkeypatch.context() as m:
        m.setattr(runner,'locked_warehouse',offline)
        assert runner.recover_run(rid,'eval-admin')['status']=='unknown'
    assert approve(platform,*begin(platform,item,key='blocked')).error.code=='job_running'
    assert runner.recover_run(rid,'eval-admin')['status']=='succeeded'


def test_recovery_uses_immutable_job_snapshot_and_hides_it_from_api(platform,monkeypatch):
    item,rid=interrupted(platform,monkeypatch)
    con=platform[0]();changed=dict(item['config']);changed['target_table']='mp_wrong_new_target'
    con.execute('UPDATE production_jobs SET version=?,config_json=? WHERE id=?',('changed',json.dumps(changed),item['id']))
    with pytest.raises(sqlite3.IntegrityError):con.execute('UPDATE production_runs SET snapshot_json=? WHERE id=?',('{}',rid))
    con.commit();con.close()
    assert runner.recover_run(rid,'eval-admin')['status']=='succeeded'
    con=platform[0]();assert con.execute('SELECT physical_name FROM catalog_assets WHERE id=?',(item['target_asset_id'],)).fetchone()[0]==item['config']['target_table'];con.close()
    response=production.get_run(rid,'eval-admin')
    assert not {'snapshot_json','snapshot_hash','execution_token'} & response.keys()


def test_changed_physical_source_and_receipt_tampering_fail_closed(platform,monkeypatch):
    item,rid=interrupted(platform,monkeypatch)
    con=platform[0]();con.execute("UPDATE data_sources SET endpoint='different://source' WHERE id='local-ecommerce-demo'");con.commit();con.close()
    assert runner.recover_run(rid,'eval-admin')['status']=='unknown'
    con=platform[0]();con.execute("UPDATE data_sources SET endpoint='local://demo.db' WHERE id='local-ecommerce-demo'");con.commit();con.close()
    physical=sqlite3.connect(platform[2]);physical.execute('UPDATE "'+item['config']['target_table']+'_receipts" SET snapshot_hash=?',('tampered',));physical.commit();physical.close()
    assert runner.recover_run(rid,'eval-admin')['status']=='unknown'


def test_late_old_writer_is_fenced_after_no_commit_recovery(platform,monkeypatch):
    item,rid=interrupted(platform,monkeypatch,stage='after_run_registered')
    record=raw(platform[0],rid);snapshot=json.loads(record['snapshot_json'])
    assert runner.recover_run(rid,'eval-admin')['status']=='failed'
    source=production.verified_source(item['config'],'eval-admin')
    with pytest.raises(ToolRuntimeError) as error:
        production_sql.materialize(item['config'],source,'2026-08-01','2026-08-02',snapshot['processed_at'],rid,target_owned=False,
            execution_proof=record,execution_guard=lambda:runner._guard(rid,record['execution_token']))
    assert error.value.code=='execution_fenced'
    # Only a new approval can write; an old request key is not replayed.
    assert approve(platform,*begin(platform,item)).error.code=='run_already_exists'
    assert approve(platform,*begin(platform,item,key='new-approval')).status is ToolStatus.SUCCESS


def test_late_metadata_writer_cannot_regress_new_materialization(platform,monkeypatch):
    item,rid=interrupted(platform,monkeypatch)
    record=raw(platform[0],rid);snapshot=json.loads(record['snapshot_json'])
    assert runner.recover_run(rid,'eval-admin')['status']=='succeeded'
    newer=approve(platform,*begin(platform,item,key='next-partition'))
    assert newer.status is ToolStatus.SUCCESS
    source=production.verified_source(item['config'],'eval-admin');source['actor_id']='eval-admin'
    result=production.get_run(rid,'eval-admin')['result']
    from datetime import date
    payload=SimpleNamespace(start_date=date(2026,8,1),end_date=date(2026,8,2))
    with pytest.raises(ToolRuntimeError) as error:runner._publish_artifact(item,source,rid,payload,result,snapshot['processed_at'],token=record['execution_token'])
    assert error.value.code=='execution_fenced'
    con=platform[0]();assert con.execute('SELECT run_id FROM asset_materializations').fetchone()[0]==newer.data['run_id'];con.close()


def test_commit_ack_loss_is_not_misreported_as_rollback(platform,monkeypatch):
    item=job();original=runner.locked_warehouse
    @contextmanager
    def lost_ack(config,source):
        with original(config,source) as con:yield con
    real=production_sql.locked_warehouse
    @contextmanager
    def after_commit(config,source):
        with real(config,source) as con:yield con
        raise OSError('synthetic lost commit acknowledgement')
    monkeypatch.setattr(production_sql,'locked_warehouse',after_commit)
    outcome=approve(platform,*begin(platform,item))
    assert outcome.error.code=='metadata_reconciliation_required'
    record=production.list_runs('eval-admin')[0]
    assert record['status']=='needs_reconciliation' and record['result']['orders']==1
    assert runner.recover_run(record['id'],'eval-admin')['status']=='succeeded'


def test_receipt_schema_conflict_is_not_silently_reused(platform):
    item=job();physical=sqlite3.connect(platform[2])
    physical.execute('CREATE TABLE "'+item['config']['target_table']+'_receipts" (run_id TEXT)');physical.commit();physical.close()
    outcome=approve(platform,*begin(platform,item))
    assert outcome.status is ToolStatus.FAILED
    assert production.list_runs('eval-admin')[0]['status']=='unknown'


def test_recovery_requires_platform_admin(platform,monkeypatch):
    _,rid=interrupted(platform,monkeypatch)
    with pytest.raises(HTTPException) as error:runner.recover_run(rid,'eval-analyst')
    assert error.value.status_code==403


def test_held_warehouse_lock_does_not_release_task_or_guess_rollback(platform,monkeypatch):
    item,rid=interrupted(platform,monkeypatch,stage='after_run_registered')
    blocker=sqlite3.connect(platform[2]);blocker.execute('BEGIN IMMEDIATE')
    try:assert runner.recover_run(rid,'eval-admin')['status']=='unknown'
    finally:blocker.rollback();blocker.close()
    assert raw(platform[0],rid)['status']=='unknown'
    assert runner.recover_run(rid,'eval-admin')['status']=='failed'


def test_legacy_running_without_receipt_protocol_remains_blocked(platform):
    item=job();con=platform[0]()
    con.execute("""INSERT INTO production_runs(id,job_id,job_version,actor_id,request_key,arguments_hash,status,start_date,end_date)
        VALUES ('legacy',?,?,'eval-admin','legacy','unknown','running','2026-08-01','2026-08-02')""",(item['id'],item['version']))
    con.commit();con.close()
    assert runner.mark_expired_runs()['marked']==1
    assert runner.recover_run('legacy','eval-admin')['status']=='unknown'
    assert approve(platform,*begin(platform,item,key='new-after-legacy')).error.code=='job_running'


def test_late_finish_cannot_acknowledge_success_after_recovery_takes_ownership(platform,monkeypatch):
    item=job()
    def scan_between_publish_and_finish(stage,rid):
        if stage=='after_metadata_publish':
            expire(platform[0],rid)
            assert runner.mark_expired_runs()['marked']==1
    monkeypatch.setattr(runner,'_fault_hook',scan_between_publish_and_finish)
    outcome=approve(platform,*begin(platform,item))
    assert outcome.status is ToolStatus.FAILED
    assert outcome.error.code=='execution_fenced'
    record=production.list_runs('eval-admin')[0]
    assert record['status']=='recovery_required' and record['completed_at'] is None
    assert runner.recover_run(record['id'],'eval-admin')['status']=='succeeded'
    con=platform[0]();assert con.execute('SELECT data_version FROM asset_materializations').fetchone()[0]==1;con.close()
