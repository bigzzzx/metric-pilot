"""Golden fixture checks, not claims about production lineage accuracy."""
import asyncio
import hashlib
import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from backend.lineage import service, documents
from backend.lineage.models import EvidenceInput, ReviewInput, SourceStateInput, DocumentClaims
from backend.lineage.sql_parser import parse_sql
from backend.lineage.validation import validate_candidate
from backend.lineage.projection import lineage, rag_documents, document_visible
from backend.lineage.events import EventInput, import_event
from backend.lineage.capture import collect_available, record_executed_query


GOLDEN = """INSERT INTO dws.sales_by_region(stat_date,region,paid_amount)
 SELECT CAST(o.order_time AS DATE),c.region,SUM(o.order_amount)
 FROM ods.orders o JOIN ods.customers c ON o.customer_id=c.customer_id
 WHERE o.status='PAID' GROUP BY CAST(o.order_time AS DATE),c.region"""


def schemas():
    def asset(name,namespace,fields):
        return dict(id=name,physical_name=name,namespace=namespace,source_id='warehouse',catalog_name='warehouse',
            source_kind='postgresql',columns={f:dict(data_type=t) for f,t in fields})
    return {a['id']:a for a in [asset('orders','ods',[('order_id','INT'),('customer_id','INT'),('order_time','TIMESTAMP'),('order_amount','NUMERIC'),('status','TEXT')]),
        asset('customers','ods',[('customer_id','INT'),('region','TEXT')]),asset('sales_by_region','dws',[('stat_date','DATE'),('region','TEXT'),('paid_amount','NUMERIC')])]}


def values(result):
    return {(c['from_asset_id'],c['from_column'],c['to_asset_id'],c['to_column']) for c in result['candidates'] if c['kind']=='column_value'}


def test_lineage_golden_positional_aggregate_join_and_influences():
    result = parse_sql(GOLDEN,schemas())
    assert result['status']=='parsed',result
    assert values(result)=={('orders','order_time','sales_by_region','stat_date'),('customers','region','sales_by_region','region'),('orders','order_amount','sales_by_region','paid_amount')}
    join = [c for c in result['candidates'] if c['kind']=='join']
    assert len(join)==1 and join[0]['from_column']==join[0]['to_column']=='customer_id'
    assert {'filter','group','join'} <= {c['context']['influence_role'] for c in result['candidates'] if c['kind']=='column_influence'}
    assert ('orders','status','sales_by_region','paid_amount') not in values(result)


@pytest.mark.parametrize('sql',[
    'INSERT INTO dws.sales_by_region SELECT CAST(order_time AS DATE),status,order_amount FROM ods.orders',
    'CREATE TABLE dws.sales_by_region AS SELECT CAST(order_time AS DATE) AS stat_date,status AS region,order_amount AS paid_amount FROM ods.orders',
    'CREATE VIEW dws.sales_by_region AS SELECT CAST(order_time AS DATE) AS stat_date,status AS region,order_amount AS paid_amount FROM ods.orders',
    'WITH q AS (SELECT order_time,status,order_amount*2 AS money FROM ods.orders) INSERT INTO dws.sales_by_region SELECT CAST(order_time AS DATE),status,money FROM q',
    'INSERT INTO dws.sales_by_region SELECT CAST(q.order_time AS DATE),q.status,q.money FROM (SELECT order_time,status,order_amount+1 AS money FROM ods.orders) q',
])
def test_lineage_cte_subquery_ctas_view_position(sql):
    result = parse_sql(sql,schemas())
    assert result['status']=='parsed',result['errors']
    assert len(values(result))==3
    assert ('orders','order_amount','sales_by_region','paid_amount') in values(result)


def test_lineage_schema_star_and_count_rows_not_fake_value():
    assets=schemas()
    assets['copy']={**assets['orders'],'id':'copy','physical_name':'orders_copy'}
    result=parse_sql('INSERT INTO ods.orders_copy SELECT * FROM ods.orders',assets)
    assert result['status']=='parsed' and len(values(result))==5,result
    assets['counter']={**assets['orders'],'id':'counter','physical_name':'counts','columns':{'n':{'data_type':'INT'}}}
    result=parse_sql('INSERT INTO ods.counts SELECT COUNT(*) AS n FROM ods.orders',assets)
    assert result['status']=='parsed' and not values(result),result
    assert any(c['context'].get('influence_role')=='row_count' and c['from_column']=='' for c in result['candidates'])


@pytest.mark.parametrize('sql,code',[
    ('SELECT customer_id FROM ods.orders o JOIN ods.customers c ON o.customer_id=c.customer_id','sql_unresolved'),
    ('SELECT missing_column FROM ods.orders','sql_unresolved'),
    ('EXECUTE dynamic_sql','unsupported_statement'),
    ('DELETE FROM ods.orders','unsupported_statement'),
    ('SELECT order_id FROM ods.orders UNION SELECT customer_id FROM ods.customers','unsupported_statement'),
    ('SELECT wrong.orders.order_id FROM ods.orders','qualifier_mismatch'),
    ('INSERT INTO dws.sales_by_region SELECT order_id FROM ods.orders','output_mapping_unresolved'),
])
def test_lineage_fail_closed_ambiguous_unknown_dynamic(sql,code):
    result=parse_sql(sql,schemas())
    assert result['status']=='unresolved' and not result['candidates'],result
    assert result['errors'][0]['code']==code


def test_lineage_select_is_usage_only_and_cross_source_names_ambiguous():
    assets=schemas()
    result=parse_sql('SELECT c.region,SUM(o.order_amount) AS amount FROM ods.orders o JOIN ods.customers c ON o.customer_id=c.customer_id GROUP BY c.region',assets)
    assert result['status']=='parsed'
    assert {c['kind'] for c in result['candidates']}=={'join'}
    assert result['statements'][0]['query_outputs']
    assets['second-orders']={**assets['orders'],'id':'second-orders','source_id':'other'}
    result=parse_sql('SELECT order_id FROM ods.orders',assets)
    assert result['errors'][0]['code']=='table_ambiguous' and not result['candidates']


@pytest.fixture
def evidence_platform(tmp_path):
    from backend.evaluation.data_platform import data_platform_environment
    with data_platform_environment(tmp_path) as environment:
        factory,_,path=environment
        from backend.evaluation.lineage import prepare_lineage_fixture
        prepare_lineage_fixture(factory,path)
        yield environment


def imported(sql=None,kind='sql_import',**kwargs):
    return service.import_evidence(EvidenceInput(title='地区汇总黄金证据',source_kind=kind,content=sql or GOLDEN.replace('dws.','').replace('ods.orders','fact_orders').replace('ods.',''),
        source_id='local-ecommerce-demo',**kwargs),'eval-admin')


def choose(kind='column_value'):
    return next(c for c in service.list_candidates('eval-admin') if c['kind']==kind)


def publish_item(item):
    review=service.review_candidate(item['id'],ReviewInput(expected_version=item['version'],decision='approve',reason='对照原始证据确认业务语义'),'eval-admin')
    return service.change_candidate_status(item['id'],review['version'],'eval-admin',publish=True)


def test_lineage_import_immutable_idempotent_and_never_executes(evidence_platform):
    factory,_,path=evidence_platform
    a=imported(source_uri='repo://job',source_version='v1');b=imported(source_uri='repo://job',source_version='v1')
    assert a['id']==b['id'] and b['idempotent'] and a['content_hash']==hashlib.sha256(service.get_source(a['id'],'eval-admin')['content'].encode()).hexdigest()
    with pytest.raises(HTTPException) as error: imported('SELECT order_id FROM fact_orders',source_uri='repo://job',source_version='v1')
    assert error.value.status_code==409
    con=factory()
    with pytest.raises(sqlite3.IntegrityError): con.execute('UPDATE lineage_sources SET content=? WHERE id=?',('tamper',a['id']))
    con.rollback();con.close()
    bad=imported('DELETE FROM fact_orders; DROP TABLE customers;')
    assert bad['state']['analysis']['status']=='unresolved'
    physical=sqlite3.connect(path)
    assert physical.execute('SELECT COUNT(*) FROM fact_orders').fetchone()[0]==2
    assert physical.execute('SELECT COUNT(*) FROM sales_by_region').fetchone()[0]==0
    physical.close()


def test_lineage_review_publish_version_rag_and_withdraw(evidence_platform):
    factory,_,_=evidence_platform
    from backend.tools.cache import dependency_fingerprint
    imported();item=choose('join')
    assert service.list_candidates('eval-analyst')==[] and rag_documents(factory())==[]
    before=dependency_fingerprint(factory)
    published=publish_item(item)
    assert before!=dependency_fingerprint(factory)
    rows=service.list_candidates('eval-analyst'); assert len(rows)==1 and rows[0]['status']=='published'
    con=factory();docs=rag_documents(con);con.close()
    assert len(docs)==1 and 'lineage_candidate_id' in docs[0]
    with pytest.raises(HTTPException) as error: service.review_candidate(item['id'],ReviewInput(expected_version=1,decision='reject',reason='过期版本审核'),'eval-admin')
    assert error.value.status_code==409
    service.change_candidate_status(item['id'],published['version'],'eval-admin')
    con=factory();assert not document_visible(con,docs[0],'eval-analyst') and not rag_documents(con);con.close()


@pytest.mark.parametrize('mutation',['schema','disabled','unpublished','permission','sensitive','source'])
def test_lineage_stale_permission_and_source_immediately_hide_rag(evidence_platform,mutation):
    factory,_,_=evidence_platform
    source=imported();item=choose('column_value');publish_item(item)
    con=factory();doc=rag_documents(con)[0]
    if mutation=='schema': con.execute("UPDATE catalog_assets SET catalog_version=catalog_version+1 WHERE id=?",(item['from_asset_id'],))
    elif mutation=='disabled': con.execute("UPDATE data_sources SET enabled=0 WHERE id='local-ecommerce-demo'")
    elif mutation=='unpublished': con.execute("UPDATE catalog_assets SET publish_status='draft' WHERE id=?",(item['to_asset_id'],))
    elif mutation=='sensitive': con.execute("UPDATE catalog_columns SET sensitivity='restricted' WHERE asset_id=? AND physical_name=?",(item['from_asset_id'],item['from_column']))
    elif mutation=='permission': con.execute("INSERT INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect) VALUES ('deny-evidence','user','eval-analyst','asset',?,'view_metadata','deny')",(item['from_asset_id'],))
    con.commit();con.close()
    if mutation=='source': service.change_source_status(source['id'],SourceStateInput(expected_version=1,status='invalidated',reason='源资料撤回'),'eval-admin')
    con=factory();assert not document_visible(con,doc,'eval-analyst');con.close()
    assert service.list_candidates('eval-analyst')==[]


def test_lineage_revalidate_does_not_auto_reapprove_or_publish(evidence_platform):
    factory,_,_=evidence_platform
    imported();item=choose();published=publish_item(item)
    service.change_candidate_status(item['id'],published['version'],'eval-admin')
    con=factory();con.execute("UPDATE catalog_assets SET catalog_version=catalog_version+1 WHERE id=?",(item['from_asset_id'],));con.commit();con.close()
    current=next(c for c in service.list_candidates('eval-admin') if c['id']==item['id'])
    validated=validate_candidate(item['id'],current['version'],'eval-admin')
    assert validated['status']=='draft' and not service.list_candidates('eval-analyst')
    with pytest.raises(HTTPException):service.change_candidate_status(item['id'],validated['version'],'eval-admin',publish=True)


def test_lineage_bounded_profile_not_business_confirmation(evidence_platform):
    imported();item=choose('join')
    result=validate_candidate(item['id'],item['version'],'eval-admin',profile=True)
    assert result['checks']['profile']['status']=='sampled',result
    assert result['checks']['profile']['sample_limit']==1000
    assert result['checks']['profile']['distinct_key_coverage']==1
    assert result['status']=='draft' and result['checks']['business_semantics']=='requires_human_review'


def test_lineage_type_mismatch_blocks_publication(evidence_platform):
    factory,_,_=evidence_platform
    con=factory();con.execute("UPDATE catalog_columns SET data_type='TEXT' WHERE asset_id='test-customers' AND physical_name='customer_id'");con.commit();con.close()
    imported();item=choose('join');assert item['technical_status']=='type_mismatch'
    with pytest.raises(HTTPException) as error: publish_item(item)
    assert error.value.status_code==422


def test_lineage_field_upstream_downstream_filters_separate(evidence_platform):
    imported()
    for item in service.list_candidates('eval-admin'): publish_item(item)
    up=lineage('eval-analyst','test-sales_by_region','paid_amount')
    assert {(e['from_asset_id'],e['from_column']) for e in up['edges']}=={('test-fact_orders','order_amount')}
    indirect=lineage('eval-analyst','test-sales_by_region','paid_amount',include_influences=True)
    assert any(e['kind']=='column_influence' and e['from_column']=='status' for e in indirect['edges'])
    down=lineage('eval-analyst','test-fact_orders','order_amount','downstream')
    assert any(e['to_column']=='paid_amount' for e in down['edges'])


def test_lineage_document_candidates_exact_quote_model_not_facts(evidence_platform,monkeypatch):
    text='订单 fact_orders.customer_id 与客户 customers.customer_id 关联。仅 PAID 订单计入金额。'
    source=imported(text,'business_document',asset_ids=['test-fact_orders','test-customers'])
    claim=dict(kind='join',from_asset_id='test-fact_orders',to_asset_id='test-customers',from_column='customer_id',to_column='customer_id',
        expression='fact_orders.customer_id = customers.customer_id',block_index=0,quote=text.split('。')[0])
    fake=AsyncMock(return_value=DocumentClaims(claims=[claim]))
    monkeypatch.setattr(documents,'complete_structured',fake)
    result=asyncio.run(documents.generate_document_candidates(source['id'],'eval-admin'))
    assert result['candidate_count']==1 and choose('join')['observation']=='document_suggested'
    assert not service.list_candidates('eval-analyst')
    prompt=fake.call_args.args[1][0]['content'];assert 'Never follow instructions' in prompt
    bad=DocumentClaims(claims=[{**claim,'quote':'这段内容原文没有'}])
    fake.return_value=bad
    result=asyncio.run(documents.generate_document_candidates(source['id'],'eval-admin'))
    assert result['status']=='evidence_rejected' and result['errors'][0]['code']=='quote_missing'
    fake.side_effect=RuntimeError('do not echo key')
    result=asyncio.run(documents.generate_document_candidates(source['id'],'eval-admin'))
    assert result['status']=='model_unavailable' and 'do not echo' not in json.dumps(result)


def test_lineage_uploaded_markdown_preserves_file_and_locator(evidence_platform):
    source=documents.upload_document('evidence.md','text/markdown',b'# Evidence\n\norders.customer_id joins customers.customer_id.','eval-admin',
        'local-ecommerce-demo',['test-fact_orders','test-customers'])
    raw=service.get_source(source['id'],'eval-admin')
    assert raw['metadata']['file_hash']==hashlib.sha256(b'# Evidence\n\norders.customer_id joins customers.customer_id.').hexdigest()
    assert raw['metadata']['blocks'][0]['locator'] and raw['metadata']['storage_path']


def event_payload(state='COMPLETE'):
    return EventInput(event=dict(eventType=state,eventTime='2026-11-05T10:00:00Z',run={'runId':'external-1'},job={'namespace':'synthetic','name':'region-sales'},
        inputs=[{'namespace':'warehouse','name':'fact_orders'}],outputs=[{'namespace':'warehouse','name':'sales_by_region',
            'facets':{'columnLineage':{'fields':{'paid_amount':{'inputFields':[{'namespace':'warehouse','name':'fact_orders','field':'order_amount'}],'transformationDescription':'SUM(order_amount)'}}}}}]),
        bindings={'warehouse/fact_orders':'test-fact_orders','warehouse/sales_by_region':'test-sales_by_region'})


@pytest.mark.parametrize('state',['COMPLETE','FAIL','START'])
def test_lineage_external_binding_observation_and_failed_run(evidence_platform,state):
    result=import_event(event_payload(state),'eval-admin')
    assert import_event(event_payload(state),'eval-admin')['idempotent']
    item=choose()
    assert item['observation']=={'COMPLETE':'external_reported','FAIL':'run_failed','START':'design'}[state]
    if state=='FAIL':
        with pytest.raises(HTTPException):publish_item(item)
    else: publish_item(item)
    raw=service.get_source(result['id'],'eval-admin');assert raw['metadata']['run_id']=='external-1'


def test_lineage_platform_query_collection_and_cache_skip(evidence_platform):
    factory,runtime,_=evidence_platform
    record_executed_query('SELECT o.order_id,c.region FROM fact_orders o JOIN customers c ON o.customer_id=c.customer_id','eval-analyst','local-ecommerce-demo','golden')
    result=collect_available('eval-admin')
    assert result['history_rows']==1 and not result['errors'],result
    assert choose('join')['observation']=='query_observed'
    assert collect_available('eval-admin')['sources'][0]['idempotent']


def test_lineage_actual_query_cache_hit_does_not_become_second_execution(evidence_platform):
    from backend.tools.cache import CacheEntry
    from backend.tools.executor import ToolExecutor
    from backend.tools.models import ToolExecutionContext
    factory,runtime,_=evidence_platform
    con=factory()
    con.execute('''INSERT INTO agents(id,enabled,tools) VALUES ('data-query-agent',1,'["readonly_sql"]')
        ON CONFLICT(id) DO UPDATE SET enabled=1,tools=excluded.tools''')
    con.commit();con.close()
    class Memo:
        backend_name='isolated-memory'
        value=None
        def get(self,*_):return CacheEntry('key',self.value) if self.value is not None else None
        def set(self,_s,_a,_c,data):self.value=data
    cached_runtime=ToolExecutor(runtime.registry,factory,Memo())
    args=dict(sql='SELECT COUNT(*) AS n FROM orders',source_id='local-ecommerce-demo')
    for i in range(2):
        result=cached_runtime.invoke('readonly_sql',args,ToolExecutionContext(user_id='eval-analyst',agent_id='data-query-agent',resource_type='asset',resource_id='asset-orders'))
        assert result.status.value=='success',result
        assert result.cache_hit==bool(i)
    con=factory();assert con.execute('SELECT COUNT(*) FROM lineage_query_history').fetchone()[0]==1;con.close()


def test_lineage_constraints_pin_actual_snapshot_not_business_defaults(evidence_platform):
    from backend.lineage.storage import canonical,digest
    factory,_,_=evidence_platform
    relation=dict(from_namespace='',from_table='fact_orders',from_column='customer_id',to_namespace='',to_table='customers',to_column='customer_id')
    con=factory()
    con.execute('''INSERT INTO catalog_technical_state(source_id,entity_type,entity_key,version,content_hash,payload_json,updated_by)
        VALUES ('local-ecommerce-demo','relationships','__all__',1,?,?,'eval-admin')''',(digest([relation]),canonical([relation])))
    con.commit();con.close()
    collected=collect_available('eval-admin');assert not collected['errors'],collected
    item=choose('join');assert item['observation']=='catalog_declared'
    assert not item['review'] and 'cardinality' not in item['context']
    publish_item(item)
    from backend.tools.cache import dependency_fingerprint
    before=dependency_fingerprint(factory)
    con=factory();con.execute("UPDATE catalog_technical_state SET content_hash='changed',version=2 WHERE source_id='local-ecommerce-demo'");con.commit();con.close()
    assert dependency_fingerprint(factory)!=before
    assert not service.list_candidates('eval-analyst')


def test_lineage_reject_and_source_optimistic_conflict(evidence_platform):
    source=imported();item=choose('join')
    rejected=service.review_candidate(item['id'],ReviewInput(expected_version=1,decision='reject',reason='业务不适用'),'eval-admin')
    with pytest.raises(HTTPException):service.change_candidate_status(item['id'],rejected['version'],'eval-admin',publish=True)
    service.change_source_status(source['id'],SourceStateInput(expected_version=1,status='invalidated',reason='业务不适用'),'eval-admin')
    with pytest.raises(HTTPException) as error:service.change_source_status(source['id'],SourceStateInput(expected_version=1,status='active',reason='重复状态更新'),'eval-admin')
    assert error.value.status_code==409


def test_lineage_discovery_snapshot_not_catalog_baseline_is_database_declaration(evidence_platform):
    from backend.lineage.storage import canonical,digest
    factory,_,_=evidence_platform
    relation=dict(from_namespace='',from_table='fact_orders',from_column='customer_id',to_namespace='',to_table='customers',to_column='customer_id')
    con=factory()
    # Minimal recorded connector snapshot; not a guessed relation by equal names.
    con.execute('''INSERT INTO catalog_sync_snapshots(id,run_id,source_id,version,snapshot_hash,payload_json)
        VALUES ('snapshot-golden','run-golden','local-ecommerce-demo',1,?,?)''',(digest([relation]),canonical({'relationships':[relation]})))
    con.commit();con.close()
    collected=collect_available('eval-admin');assert not collected['errors'],collected
    item=choose('join');assert item['observation']=='database_declared'
    source=service.get_source(item['source_ref'],'eval-admin')
    assert source['source_kind']=='database_constraint' and source['metadata']['discovery_id']=='snapshot-golden'


def test_lineage_reconciled_run_keeps_failed_evidence_and_adds_complete_revision(evidence_platform):
    from test.test_data_services import job,begin,approve
    from backend.data_services.production_runner import reconcile_run
    factory,_,_=evidence_platform
    item=job()
    with patch('backend.data_services.production_runner._publish_artifact',side_effect=RuntimeError('simulated metadata outage')):
        result=approve(evidence_platform,*begin(evidence_platform,item))
    assert result.status.value=='failed'
    con=factory();run=con.execute("SELECT * FROM production_runs WHERE status='needs_reconciliation'").fetchone();con.close()
    assert run
    reconcile_run(run['id'],'eval-admin')
    sources=[s for s in service.list_sources('eval-admin') if s['source_kind']=='production_run']
    assert {s['metadata']['run']['state'] for s in sources}=={'FAIL','COMPLETE'}
    success=next(s for s in sources if s['metadata']['run']['state']=='COMPLETE')
    assert success['state']['analysis']['candidate_count']>0
    assert all(c['observation']=='run_observed' for c in service.list_candidates('eval-admin',source_ref=success['id']))


@pytest.mark.parametrize('mutation',['inputs','facets','fields','inputFields','run'])
def test_lineage_malformed_external_event_returns_422_not_internal_error(evidence_platform,mutation):
    payload=event_payload()
    if mutation=='inputs':payload.event['inputs']='not a list'
    elif mutation=='run':payload.event['run']=[]
    elif mutation=='facets':payload.event['outputs'][0]['facets']=[]
    elif mutation=='fields':payload.event['outputs'][0]['facets']['columnLineage']['fields']=[]
    else:payload.event['outputs'][0]['facets']['columnLineage']['fields']['paid_amount']['inputFields']=None
    with pytest.raises(HTTPException) as exc:import_event(payload,'eval-admin')
    assert exc.value.status_code==422


def test_lineage_quoted_identifiers_and_nested_value_expressions():
    assets=schemas()
    result=parse_sql('''INSERT INTO "dws"."sales_by_region"("stat_date","region","paid_amount")
        SELECT CAST(o.order_time AS DATE),COALESCE(c.region,'unknown'),
        SUM(CASE WHEN o.status='PAID' THEN o.order_amount*2 ELSE 0 END)
        FROM "ods"."orders" o JOIN "ods"."customers" c ON o.customer_id=c.customer_id''',assets)
    assert result['status']=='parsed',result['errors']
    assert ('orders','status','sales_by_region','paid_amount') in values(result)
    assert ('orders','order_amount','sales_by_region','paid_amount') in values(result)
    amount=next(c for c in result['candidates'] if c['kind']=='column_value' and c['to_column']=='paid_amount')
    assert 'CASE' in amount['expression'] and '* 2' in amount['expression']


def test_lineage_postgres_mixed_case_table_requires_quotes():
    assets=schemas();assets['orders']['physical_name']='Orders'
    assert parse_sql('SELECT order_id FROM ods.orders',assets)['status']=='unresolved'
    assert parse_sql('SELECT order_id FROM ods."Orders"',assets)['status']=='parsed'


def test_lineage_internal_production_success_and_failed_evidence(evidence_platform):
    from test.test_data_services import job,begin,approve
    factory,_,path=evidence_platform
    item=job();result=approve(evidence_platform,*begin(evidence_platform,item))
    assert result.status.value=='success',result
    sources=service.list_sources('eval-admin')
    raw=service.get_source(next(s['id'] for s in sources if s['source_kind']=='production_run'),'eval-admin')
    assert raw['metadata']['run']['state']=='COMPLETE' and raw['metadata']['run']['job_id']==item['id']
    candidates=service.list_candidates('eval-admin',source_ref=raw['id'])
    assert candidates and {c['observation'] for c in candidates}=={'run_observed'},raw['state']['analysis']
    assert any(c['kind']=='column_value' and c['to_column']=='paid_amount' and c['from_column']=='amount' for c in candidates)
    assert any(c['kind']=='column_influence' and c['context'].get('influence_role')=='row_count' and c['to_column']=='order_count' for c in candidates)
    physical=sqlite3.connect(path);physical.execute("UPDATE orders SET amount=NULL WHERE status='PAID'");physical.commit();physical.close()
    failed=approve(evidence_platform,*begin(evidence_platform,item,key='failure-lineage'))
    assert failed.status.value=='failed'
    raw_sources=service.list_sources('eval-admin')
    fail=next(s for s in raw_sources if s['source_kind']=='production_run' and s['metadata']['run']['state']=='FAIL')
    assert fail['state']['analysis']['status']=='unresolved'
    assert not service.list_candidates('eval-admin',source_ref=fail['id'])


def test_lineage_http_authorization_and_schema_validation(evidence_platform):
    from backend.main import app
    from backend.auth import require_user
    from fastapi.testclient import TestClient
    app.dependency_overrides[require_user]=lambda:{'id':'eval-admin'}
    try:
        with patch('backend.lineage.service.run_index_job',lambda *_:None),TestClient(app) as client:
            response=client.post('/api/catalog/evidence/import',json=EvidenceInput(title='API',source_id='local-ecommerce-demo',content='SELECT o.order_id,c.region FROM fact_orders o JOIN customers c ON o.customer_id=c.customer_id').model_dump())
            assert response.status_code==200,response.text
            cid=client.get('/api/catalog/evidence/candidates').json()[0]['id']
            response=client.post('/api/catalog/evidence/candidates/'+cid+'/review',json=dict(expected_version=1,decision='approve',reason='业务确认'))
            assert response.status_code==200,response.text
            response=client.post('/api/catalog/evidence/candidates/'+cid+'/publish',json=dict(expected_version=2))
            assert response.status_code==200,response.text
            app.dependency_overrides[require_user]=lambda:{'id':'eval-analyst'}
            assert client.get('/api/catalog/evidence/sources').status_code==403
            assert client.post('/api/catalog/evidence/import',json=EvidenceInput(title='denied',content='SELECT 1').model_dump()).status_code==403
            assert len(client.get('/api/catalog/evidence/candidates').json())==1
    finally:app.dependency_overrides.clear()
