import sqlite3
import subprocess
import sys
import threading

import pytest

from backend import index_worker as worker


@pytest.fixture
def index_db(tmp_path):
    path = tmp_path / 'platform.db'
    def factory():
        con = sqlite3.connect(path, timeout=2)
        con.row_factory = sqlite3.Row
        return con
    con = factory()
    con.execute('''CREATE TABLE metric_wiki_index_jobs(id TEXT PRIMARY KEY,metric_id TEXT,
        metric_version INTEGER,status TEXT DEFAULT 'pending',error TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,completed_at TEXT)''')
    con.execute('''CREATE TABLE lineage_index_jobs(id TEXT PRIMARY KEY,status TEXT DEFAULT 'pending',
        error TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,completed_at TEXT)''')
    con.commit()
    con.close()
    worker.init_schema(factory)
    return factory


def add(factory, kind='lineage', identifier='job'):
    con = factory()
    con.execute(f'INSERT INTO {worker.TABLES[kind]}(id) VALUES (?)', (identifier,))
    con.commit()
    con.close()
    return identifier


@pytest.mark.parametrize('kind',['lineage','wiki','catalog'])
def test_index_backoff_budget_and_explicit_retry(index_db,kind):
    identifier = add(index_db,kind)
    clock = [1000.0]
    calls = []
    for attempt in range(1,6):
        result = worker.run_job(kind,identifier,connection_factory=index_db,
            refresh=lambda: calls.append(1) or False,now=lambda:clock[0])
        assert result['attempts']==attempt
        assert result['status']==('failed' if attempt==5 else 'pending')
        if attempt<5:
            skipped = worker.run_job(kind,identifier,connection_factory=index_db,
                refresh=lambda:pytest.fail('not due'),now=clock[0])
            assert skipped['claimed'] is False
        clock[0]=result['next_attempt_at']
    assert len(calls)==5
    assert worker.run_once(connection_factory=index_db,refresh=lambda:pytest.fail('terminal'),now=99999)==[]
    con=index_db()
    reset=worker.reset_job(con,kind,identifier,now=100000)
    con.commit();con.close()
    assert reset['attempts']==0 and reset['status']=='pending' and reset['next_attempt_at']==0
    result=worker.run_once(connection_factory=index_db,refresh=lambda:True,now=100000)
    assert result[0]['status']=='completed' and result[0]['attempts']==1


def test_expired_running_reclaimed_but_live_owner_not_stolen(index_db):
    add(index_db)
    con=index_db()
    con.execute("UPDATE lineage_index_jobs SET status='running',attempts=1,lease_token='dead',lease_until=100")
    con.commit();con.close()
    assert worker.run_once(connection_factory=index_db,refresh=lambda:pytest.fail('live owner'),now=99)==[]
    con=index_db()
    assert worker.reset_job(con,'lineage','job',now=99)['retry_blocked']
    con.close()
    result=worker.run_once(connection_factory=index_db,refresh=lambda:True,now=101)
    assert result[0]['status']=='completed' and result[0]['attempts']==2


def test_stale_acknowledgement_cannot_complete_reclaimed_job(index_db):
    add(index_db)
    def paused_old_writer():
        con=index_db()
        con.execute("UPDATE lineage_index_jobs SET lease_token='new-owner',lease_until=300,attempts=2")
        con.commit();con.close()
        return True
    result=worker.run_job('lineage','job',connection_factory=index_db,refresh=paused_old_writer,now=100)
    assert result['stale_acknowledgement']
    assert result['status']=='running' and result['lease_token']=='new-owner'


def test_concurrent_claim_only_executes_one_refresh(index_db):
    add(index_db)
    entered, release = threading.Event(), threading.Event()
    results=[]
    def refresh():
        entered.set()
        assert release.wait(5)
        return True
    thread=threading.Thread(target=lambda:results.append(worker.run_job('lineage','job',
        connection_factory=index_db,refresh=refresh,now=100)))
    thread.start()
    try:
        assert entered.wait(5)
        denied=worker.run_job('lineage','job',connection_factory=index_db,
            refresh=lambda:pytest.fail('duplicate claim'),now=101)
        assert denied['claimed'] is False
    finally:
        release.set();thread.join(5)
    assert results[0]['status']=='completed'


def test_outbox_is_transactional_and_safe_pending_coalescence(index_db):
    con=index_db()
    con.execute('BEGIN IMMEDIATE')
    first=worker.enqueue_catalog(con)
    assert worker.enqueue_catalog(con)==first
    con.rollback()
    assert con.execute('SELECT COUNT(*) FROM catalog_index_jobs').fetchone()[0]==0
    identifier=worker.enqueue_catalog(con)
    con.commit()
    con.execute("UPDATE catalog_index_jobs SET status='running',lease_until=1000")
    assert worker.enqueue_catalog(con)!=identifier  # An in-flight snapshot cannot cover a later change.
    con.commit();con.close()


def test_writer_lock_is_process_scoped_and_released_after_kill(index_db):
    con=index_db()
    filename=con.execute('PRAGMA database_list').fetchone()[2]
    con.close()
    code='''import sqlite3,sys,time
con=sqlite3.connect(sys.argv[1]+'.catalog-index-lock',timeout=0)
con.execute('BEGIN EXCLUSIVE')
print('locked',flush=True)
time.sleep(60)
'''
    process=subprocess.Popen([sys.executable,'-u','-c',code,filename],stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='locked'
        with pytest.raises(sqlite3.OperationalError):
            with worker.writer_lock(index_db):pytest.fail('concurrent writer')
    finally:
        process.kill();process.communicate(timeout=5)
    with worker.writer_lock(index_db):
        pass  # No timestamp override/unlock of a potentially live writer was required.


def test_killed_consumer_job_is_reclaimed_by_fresh_process_state(index_db):
    add(index_db)
    con=index_db();filename=con.execute('PRAGMA database_list').fetchone()[2];con.close()
    code='''import sqlite3,sys,time
from backend.index_worker import run_job
def factory():
    con=sqlite3.connect(sys.argv[1]);con.row_factory=sqlite3.Row;return con
def refresh():
    print('claimed',flush=True)
    time.sleep(60)
    return True
run_job('lineage','job',connection_factory=factory,refresh=refresh,now=100,lease_seconds=10)
'''
    process=subprocess.Popen([sys.executable,'-u','-c',code,filename],stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='claimed'
    finally:
        process.kill();process.communicate(timeout=5)
    assert worker.run_once(connection_factory=index_db,refresh=lambda:pytest.fail('unexpired'),now=109)==[]
    result=worker.run_once(connection_factory=index_db,refresh=lambda:True,now=111)
    assert result[0]['status']=='completed' and result[0]['attempts']==2


def test_full_projection_rechecks_durable_marker_and_never_uses_process_cache(index_db,monkeypatch):
    from backend.catalog import vector_index as vi
    state={'version':'old','content':'first','rows':[]}
    class Client:
        def has_collection(self,**kwargs):return True
        def query(self,**kwargs):
            if kwargs.get('filter')=='id == "catalog-index-state"':return [{'version':state['version']}]
            return []
        def upsert(self,**kwargs):
            state['rows'].extend(kwargs['data'])
            for row in kwargs['data']:
                if row['id']=='catalog-index-state':state['version']=row['version']
    monkeypatch.setattr(vi,'db',index_db)
    monkeypatch.setattr(vi,'using_milvus',lambda:True)
    monkeypatch.setattr(vi,'milvus_client',Client)
    monkeypatch.setattr(vi,'_remove_legacy_shared_metadata',lambda _c:0)
    monkeypatch.setattr(vi,'_embed_batched',lambda texts:[[0.0]*512 for _ in texts])
    monkeypatch.setattr(vi,'published_catalog_documents',lambda:[dict(id='a',kind='table',title='A',content=state['content'])])
    assert vi.ensure_catalog_index()
    vi._indexed_version=state['version']
    state['version']='written-by-another-process'
    state['content']='current approved definition'
    assert vi.ensure_catalog_index()
    assert [row for row in state['rows'] if row['id']=='a'][-1]['content']=='current approved definition'


def test_worker_restart_and_health_use_persisted_jobs(index_db):
    add(index_db,'wiki','wiki-old')
    add(index_db,'lineage','lineage-old')
    assert worker.job_health(index_db)['pending']==2
    # A new consumer object has no private in-memory queue to recover.
    results=worker.run_once(connection_factory=index_db,refresh=lambda:True,now=100)
    assert len(results)==2 and all(row['status']=='completed' for row in results)
    assert worker.job_health(index_db)['pending']==0


def test_migration_requeues_legacy_running_once_not_live_new_lease(tmp_path):
    path=tmp_path/'legacy.db'
    def factory():
        con=sqlite3.connect(path);con.row_factory=sqlite3.Row;return con
    con=factory()
    con.execute('''CREATE TABLE lineage_index_jobs(id TEXT PRIMARY KEY,status TEXT,error TEXT,
                   created_at TEXT DEFAULT CURRENT_TIMESTAMP,completed_at TEXT)''')
    con.execute("INSERT INTO lineage_index_jobs(id,status) VALUES ('old','running')")
    con.commit();con.close()
    worker.init_schema(factory)
    con=factory()
    assert con.execute('SELECT status FROM lineage_index_jobs').fetchone()[0]=='pending'
    con.execute("UPDATE lineage_index_jobs SET status='running',lease_token='live',lease_until=999999")
    con.commit();con.close()
    worker.init_schema(factory)
    con=factory()
    assert con.execute('SELECT lease_token FROM lineage_index_jobs').fetchone()[0]=='live'
    con.close()


@pytest.mark.parametrize('backend,requested,enabled,reason',[
    ('sqlite','1',False,'vector_backend_disabled'),
    ('milvus','0',False,'worker_disabled'),
    ('milvus','1',True,None),
])
def test_worker_starts_only_for_enabled_vector_backend(index_db,monkeypatch,backend,requested,enabled,reason):
    monkeypatch.setenv('VECTOR_STORE_BACKEND',backend)
    monkeypatch.setenv('CATALOG_INDEX_WORKER_ENABLED',requested)
    marker=object()
    monkeypatch.setattr(worker.IndexWorker,'start',lambda self:marker)
    assert worker.start_worker(index_db)==(marker if enabled else None)
    health=worker.job_health(index_db)
    assert health['worker_enabled'] is enabled and health['backend']==backend
    assert health['disabled_reason']==reason
