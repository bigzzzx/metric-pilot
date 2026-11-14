"""Isolation, governance, and restart semantics of the automatic collectors."""
import json
import sqlite3
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend.catalog import publications, service as catalog, sync, vector_index
from backend.lineage import capture, metrics, projection, service, views, worker
from backend.semantic_metrics import wiki
from test.test_data_source_admin import seed_users


@pytest.fixture
def platform(tmp_path, monkeypatch):
    from backend.main import init_storage
    def factory():
        con = sqlite3.connect(tmp_path/'auto-lineage.db')
        con.row_factory = sqlite3.Row
        return con
    init_storage(factory)
    seed_users(factory)
    for module in (publications, catalog, sync, service, metrics, projection, wiki):
        monkeypatch.setattr(module, 'db', factory)
    monkeypatch.setattr(vector_index, 'ensure_catalog_index', lambda: False)
    monkeypatch.setattr(vector_index, 'refresh_catalog_assets', lambda _: False)
    con = factory()
    with con:
        con.execute("""INSERT INTO data_sources(id,name,kind,enabled,auto_lineage_interval_minutes)
            VALUES ('pg','Synthetic PG','postgresql',1,0)""")
    con.close()
    return factory


def snapshot(view_sql='SELECT amount AS sales FROM ods.orders WHERE status = \'PAID\'', *, amount_type='numeric'):
    def col(name, kind):
        return dict(name=name,type=kind,description='字段 '+name)
    return dict(view_scan_complete=True, table_count=2, relationships=[], relationship_count=0, tables=[
        dict(namespace='ods',name='orders',description='订单；业务域=电商；粒度=订单',
             columns=[col('amount', amount_type),col('other_amount','numeric'),col('status','text')]),
        dict(namespace='dws',name='sales',description='销售视图；业务域=电商；粒度=订单',
             asset_type='view',view_definition=view_sql,view_definition_status='available',columns=[col('sales','numeric')]),
    ])


def approve_discovery():
    for change in sync.list_changes('pg','admin'):
        sync.decide_change('pg', change['id'], 'admin', 'approve', '核对合成 Schema',refresh_index=False)


def publish_discovered(platform):
    con=platform()
    try:
        assets={row['physical_name']:row['id'] for row in con.execute("SELECT id,physical_name FROM catalog_assets WHERE source_id='pg'")}
    finally:
        con.close()
    assert publications.publish_assets([dict(asset_id=aid) for aid in assets.values()], 'admin')['published_count']==len(assets)
    return assets


def drain_views(platform):
    for _ in range(10):
        if not views.process_pending_view(platform):
            return
    pytest.fail('View backlog did not drain')


def metric_review(platform, metric_id='metric-demo-paid', **updates):
    con=platform()
    try:
        row=con.execute('SELECT * FROM catalog_metrics WHERE id=?',(metric_id,)).fetchone()
        contract={**json.loads(row['contract_json']), **updates}
        source=wiki.source_records(con, metric_id)[0]
    finally:
        con.close()
    return wiki.MetricReview(expected_version=row['catalog_version'],contract=contract,
        wiki=dict(summary='合成验收',content='已核对口径',claims=[dict(claim_type='definition',content='已核对',
            source_ref=source['id'],evidence=source['content'][:10])]))


def test_metric_versions_and_roles_are_not_guessed_from_wiki(platform):
    paid=metrics.metric_lineage('analyst','metric-demo-paid')
    assert paid['status']=='current'
    assert {d['role'] for d in paid['dependencies']}=={'dataset','value','required_filter','time','optional_dimension'}
    booked=metrics.metric_lineage('analyst','metric-demo-booked')
    assert not any(d['role']=='required_filter' for d in booked['dependencies'])
    down=projection.lineage('analyst','asset-orders','status','downstream')
    assert [m['metric_id'] for m in down['metric_impacts']]==['metric-demo-paid']
    published=wiki.publish('metric-demo-paid','admin',metric_review(platform,measure_expression='COUNT(*)',required_filters=[]))
    assert published['version']==paid['metric_version']+1
    latest=metrics.metric_lineage('analyst','metric-demo-paid')
    assert latest['metric_version']==published['version']
    assert 'row_count' in {d['role'] for d in latest['dependencies']}
    assert not projection.lineage('analyst','asset-orders','status','downstream')['metric_impacts']
    con=platform()
    assert con.execute("SELECT COUNT(*) FROM metric_lineage_revisions WHERE metric_id='metric-demo-paid'").fetchone()[0]==2
    con.close()


def test_view_chain_reverse_impact_and_definition_drift(platform):
    sync.record_discovery('pg',snapshot(),'admin')
    drain_views(platform)
    approve_discovery()
    ids=publish_discovered(platform)
    assert not projection.lineage('analyst',ids['sales'],'sales')['edges']
    drain_views(platform)
    visible=projection.lineage('analyst',ids['sales'],'sales',include_influences=True)
    assert {(e['from_column'], e['kind']) for e in visible['edges']}=={('amount','column_value'),('status','column_influence')}
    assert all(e['observation']=='database_view_definition' for e in visible['edges'])
    assert views.process_pending_view(platform) is False
    wiki.publish('metric-demo-paid','admin',metric_review(platform,source_id='pg',asset_id=ids['sales'],
        namespace='dws',table='sales',dialect='postgresql',measure_expression='SUM(sales)',required_filters=[],
        time_column=None,requires_time_range=False,dimensions={}))
    line=metrics.metric_lineage('analyst','metric-demo-paid')
    assert {e['from_column'] for e in line['upstream_edges']}=={'amount','status'}
    for column in ('amount','status'):
        result=projection.lineage('analyst',ids['orders'],column,'downstream')
        assert [m['metric_id'] for m in result['metric_impacts']]==['metric-demo-paid']
    con=platform()
    old_docs=projection.rag_documents(con)
    con.close()
    changed=snapshot('SELECT other_amount AS sales FROM ods.orders')
    sync.record_discovery('pg',changed,'admin')
    assert projection.lineage('analyst',ids['sales'],'sales')['view_status']=='waiting_review'
    assert not projection.lineage('analyst',ids['sales'],'sales')['edges']
    assert metrics.metric_lineage('analyst','metric-demo-paid')['status']=='unavailable'
    con=platform()
    assert old_docs and not any(projection.document_visible(con,d,'analyst') for d in old_docs)
    con.close()
    approve_discovery(); drain_views(platform)
    assert {e['from_column'] for e in metrics.metric_lineage('analyst','metric-demo-paid')['upstream_edges']}=={'other_amount'}
    assert not projection.lineage('analyst',ids['orders'],'amount','downstream')['metric_impacts']
    sync.record_discovery('pg',snapshot('SELECT other_amount AS sales FROM ods.orders',amount_type='integer'),'admin')
    assert projection.lineage('analyst',ids['sales'],'sales')['view_status']=='waiting_review'


def test_visibility_and_unresolved_view_do_not_allow_execution(platform):
    broken=snapshot('SELECT amount AS sales FROM ods.orders UNION SELECT amount FROM ods.orders')
    sync.record_discovery('pg',broken,'admin'); approve_discovery(); ids=publish_discovered(platform); drain_views(platform)
    assert projection.lineage('analyst',ids['sales'],'sales')['view_status']=='unresolved'
    con=platform()
    with pytest.raises(HTTPException) as error:
        views.assert_approved_for_query(con,dict(con.execute('SELECT * FROM catalog_assets WHERE id=?',(ids['sales'],)).fetchone()))
    assert error.value.status_code==409
    with con:
        con.execute("""INSERT INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect)
            VALUES ('deny-view','user','analyst','asset','asset-orders','view_metadata','deny')""")
    con.close()
    with pytest.raises(HTTPException) as error:
        metrics.metric_lineage('analyst','metric-demo-paid')
    assert error.value.status_code==404


def test_query_consumer_deduplicates_and_does_not_rebind_old_schemas(platform):
    before_sql='SELECT o.amount, p.category FROM orders o JOIN products p ON p.product_id=o.product_id'
    capture.record_executed_query(before_sql,'analyst','local-ecommerce-demo','test',connection_factory=platform)
    assert capture.process_pending_query(platform)
    con=platform()
    row=con.execute('SELECT * FROM lineage_query_history').fetchone()
    assert row['parse_status']=='parsed'
    with con:
        con.execute("UPDATE lineage_query_history SET parse_status='pending'")
    con.close()
    assert capture.process_pending_query(platform)
    assert not capture.process_pending_query(platform)
    candidates=service.list_candidates('admin',source_ref=row['evidence_ref'])
    assert candidates and all(c['status']=='draft' and c['kind']=='join' for c in candidates)
    capture.record_executed_query(before_sql,'analyst','local-ecommerce-demo','test',connection_factory=platform)
    con=platform()
    with con:
        con.execute("UPDATE catalog_assets SET catalog_version=catalog_version+1 WHERE id='asset-products'")
    con.close()
    assert capture.process_pending_query(platform)
    con=platform()
    assert con.execute("SELECT COUNT(*) FROM lineage_query_history WHERE parse_status='unresolved'").fetchone()[0]==1
    assert con.execute("SELECT COUNT(*) FROM lineage_sources WHERE source_kind='query_execution'").fetchone()[0]==2
    con.close()


def test_only_opted_in_source_scanned_and_unchanged_snapshot_reused(platform, monkeypatch):
    from backend.data_sources import service as source_service
    class Connector:
        def discover(self):
            return deepcopy(snapshot())
    monkeypatch.setattr(source_service,'connector_from_row',lambda row: Connector())
    assert not worker.scan_due_source(platform,clock=lambda:1000)
    con=platform()
    with con:
        con.execute("UPDATE data_sources SET auto_lineage_interval_minutes=5 WHERE id='pg'")
    con.close()
    assert worker.scan_due_source(platform,clock=lambda:1000)
    assert not worker.scan_due_source(platform,clock=lambda:1100)
    assert worker.scan_due_source(platform,clock=lambda:1301)
    con=platform()
    try:
        assert con.execute("SELECT COUNT(*) FROM catalog_sync_snapshots WHERE source_id='pg'").fetchone()[0]==1
        assert worker.status_view(con)['sources'][0]['status']=='completed'
    finally:
        con.close()


def test_background_retry_is_due_and_persistent(platform, monkeypatch):
    capture.record_executed_query('SELECT amount FROM orders','analyst','local-ecommerce-demo','test',connection_factory=platform)
    original=service.import_evidence
    def fail(*args,**kwargs):
        raise RuntimeError('injected temporary error')
    monkeypatch.setattr(service,'import_evidence',fail)
    assert capture.process_pending_query(platform,now=100)
    assert not capture.process_pending_query(platform,now=104)
    monkeypatch.setattr(service,'import_evidence',original)
    assert capture.process_pending_query(platform,now=105)
    con=platform()
    try:
        assert tuple(con.execute('SELECT parse_status,parse_attempts FROM lineage_query_history').fetchone())==('parsed',1)
    finally:
        con.close()


def test_view_reads_do_not_reuse_either_cache(platform, monkeypatch):
    from backend import turn_cache
    from backend.schemas import ChatResponse
    from backend.tools.builtins import register_builtin_tools
    from backend.tools.executor import ToolExecutor
    from backend.tools.models import ToolExecutionContext
    from backend.tools.registry import ToolRegistry

    sync.record_discovery('pg',snapshot(),'admin'); approve_discovery(); publish_discovered(platform)
    con=platform()
    with con:
        con.execute('''INSERT INTO agents(id,enabled,tools) VALUES ('data-query-agent',1,'["readonly_sql"]')
            ON CONFLICT(id) DO UPDATE SET enabled=1,tools=excluded.tools''')
    con.close()
    sql='SELECT SUM(sales) FROM dws.sales'
    class NoCacheAccess:
        def get(self,*args):
            pytest.fail('view execution must not read cached SQL rows')
        def set(self,*args):
            pytest.fail('view execution must not write cached SQL rows')
    registry=ToolRegistry();register_builtin_tools(registry)
    monkeypatch.setattr('backend.tools.builtins.run_readonly_sql',lambda *_a,**_kw:[{'amount':100}])
    result=ToolExecutor(registry,platform,NoCacheAccess()).invoke('readonly_sql',dict(sql=sql,source_id='pg'),
        ToolExecutionContext(user_id='admin',agent_id='data-query-agent'))
    assert result.status.value=='success' and not result.cache_hit, result
    monkeypatch.setattr(turn_cache,'db',platform)
    response=ChatResponse(answer='100',intent='DATA_QUERY',sql=sql)
    class OldAnswerCache:
        def get(self,*args): return response.model_dump(mode='json')
        def set(self,*args): pytest.fail('view answer must not be cached')
    monkeypatch.setattr(turn_cache,'_cache',OldAnswerCache())
    identity={'user_id':'admin'}
    assert turn_cache.load_cached_response(identity) is None
    assert not turn_cache.store_cached_response(identity,response,dict(route='data_query',plan={'source_id':'pg'},tool_result={'status':'success'}))
