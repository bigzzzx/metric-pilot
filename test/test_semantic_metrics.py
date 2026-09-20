import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi import HTTPException
from backend.evaluation.environment import evaluation_environment
from backend.semantic_metrics import nodes, source_files, wiki
from backend.semantic_metrics.resolver import resolve_metric_query
from backend.semantic_metrics.compiler import compile_metric_query, fast_request, MetricRequest
from backend.catalog.rag_documents import authorized_catalog_documents
from backend.tools.cache import dependency_fingerprint


@pytest.fixture
def isolated(tmp_path):
    with evaluation_environment(tmp_path) as factory:
        yield factory


def state(message):
    return dict(message=message,user_id='eval-analyst',route='data_query',history=[],model_id=None,conversation_id='metric-test')


@pytest.mark.parametrize('question,status',[
    ('2026年8月演示已支付金额','matched'),('2026年8月演示销售额','ambiguous'),
    ('查询 orders 有几行','unmatched'),('GMV','unsupported'),
    ('原始字段演示销售额','ambiguous'),('2026年8月演示下单金额和演示已支付金额','ambiguous'),
])
def test_resolution(question,status,isolated):
    assert resolve_metric_query(question,'eval-analyst').status==status


def test_permission_denial_and_hidden_domain(isolated):
    con=isolated()
    con.execute("INSERT INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect) VALUES ('deny-test','user','eval-analyst','asset','asset-orders','query_data','deny')")
    con.commit(); con.close()
    assert resolve_metric_query('演示已支付金额','eval-analyst').status=='denied'
    assert resolve_metric_query('营销 ROI','eval-analyst').status=='unmatched'


@pytest.mark.parametrize('question,expected',[
    ('2026年8月演示已支付金额',150),('2026年8月演示下单金额',425),
    ('2026年9月演示已支付金额',300),('2026年6月演示已支付金额',None),
])
def test_metric_graph_executes_real_sql_without_planner(question,expected,isolated,monkeypatch):
    from backend import agent
    async def router(s): return {**s,'route':'data_query','memory_retrieval':'none'}
    async def forbidden(*args,**kwargs): raise AssertionError('Metric fast path must not call LLM/RAG')
    monkeypatch.setattr(agent,'router',router)
    monkeypatch.setattr(agent,'complete_structured',forbidden)
    monkeypatch.setattr(agent,'rag_retrieve',forbidden)
    result=asyncio.run(agent.run_agent(question,'data-query-agent',[],user_id='eval-analyst'))
    assert result['query_mode']=='text_to_metrics'
    assert list(result['result'][0].values())==[expected]
    assert not result['metric_parameter_model_used']
    assert [n['node'] for n in result['node_trace']]==['router','metric_router','metric_plan','query','data_analysis','visualization']


@pytest.mark.parametrize('suffix',['按地区','只看华东','不排除退款','和去年同比','2026年9月','按支付日期','PostgreSQL 测试数仓'])
def test_extra_conditions_never_silently_disappear(suffix,isolated):
    metric=resolve_metric_query('演示已支付金额','eval-analyst').metrics[0]
    assert fast_request('2026年8月演示已支付金额'+suffix,metric) is None


def test_invalid_date_missing_time_and_expression(isolated):
    resolved=resolve_metric_query('演示已支付金额','eval-analyst')
    assert compile_metric_query('演示已支付金额',resolved)['sql'] is None
    assert compile_metric_query('2026-02-31演示已支付金额',resolved)['sql'] is None
    resolved.metrics[0].contract.measure_expression='SUM(amount); DELETE FROM orders'
    with pytest.raises(ValueError):
        compile_metric_query('2026-08演示已支付金额',resolved,columns={'amount','order_date','status'})


def test_followup_current_metric_and_date_win(isolated,monkeypatch):
    s=state('那2026年9月呢'); s.update(contextual_follow_up=True,history=[{'role':'user','content':'2026年8月演示已支付金额'}])
    result=nodes.metric_router(s)
    assert result['metric_resolution']['metrics'][0]['id']=='metric-demo-paid'
    s['message']='2026年9月演示下单金额'
    assert nodes.metric_router(s)['metric_resolution']['metrics'][0]['id']=='metric-demo-booked'


def test_model_parameters_still_use_fixed_contract(isolated,monkeypatch):
    from backend import agent
    async def complete(*args): return MetricRequest(start_date='2026-08-01',end_date='2026-09-01',dimensions=['date'])
    monkeypatch.setattr(agent,'complete_structured',complete)
    result=asyncio.run(nodes.metric_plan(nodes.metric_router(state('帮我看看2026年8月演示已支付金额每天走势'))))
    assert result['metric_parameter_model_used']
    assert 'GROUP BY' in result['plan']['sql'] and "'PAID'" in result['plan']['sql']


def test_source_disabled_and_contract_changed_stop(isolated):
    from backend import agent
    s=asyncio.run(nodes.metric_plan(nodes.metric_router(state('2026年8月演示已支付金额'))))
    con=isolated(); con.execute("UPDATE catalog_metrics SET catalog_version=catalog_version+1 WHERE id='metric-demo-paid'");con.commit();con.close()
    result=agent.query_tool(s)
    assert result['query_terminal'] and '变化' in result['query_error']
    con=isolated();con.execute("UPDATE data_sources SET enabled=0 WHERE id='local-ecommerce-demo'");con.commit();con.close()
    s=asyncio.run(nodes.metric_plan(nodes.metric_router(state('2026年8月演示已支付金额'))))
    assert s['plan']['sql'] is None


def test_governed_errors_are_not_rewritten(isolated):
    from backend import agent
    assert agent.route_after_query({'query_mode':'text_to_metrics','query_error':'bad schema'})=='query_failed'


def test_wiki_draft_review_version_rag_and_cache(isolated,monkeypatch):
    con=isolated();con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','eval-analyst')");con.commit();con.close()
    before=dependency_fingerprint(isolated)
    source_ref='wiki-source:metric-demo-paid:catalog-v1'
    evidence='仅统计 PAID 状态订单金额，排除取消和退款状态。'
    async def complete(*args): return wiki.WikiDraft(
        summary='已支付',content='模型新草稿，需要审核。',
        claims=[{'claim_type':'definition','content':'只统计已支付订单','source_ref':source_ref,'evidence':evidence}],
    )
    monkeypatch.setattr(wiki,'complete_structured',complete)
    asyncio.run(wiki.generate_draft('metric-demo-paid','eval-analyst'))
    docs=authorized_catalog_documents('eval-analyst')
    assert not any('模型新草稿' in d['content'] for d in docs)
    con=isolated();row=con.execute("SELECT * FROM catalog_metrics WHERE id='metric-demo-paid'").fetchone();con.close()
    payload=wiki.MetricReview(expected_version=row['catalog_version'],aliases=['演示已支付金额'],
                             contract=json.loads(row['contract_json']),wiki={'summary':'已审核','content':'模型新草稿，需要审核。',
                             'claims':[{'claim_type':'definition','content':'只统计已支付订单','source_ref':source_ref,'evidence':evidence}]})
    published=wiki.publish('metric-demo-paid','eval-analyst',payload)
    assert published['index_status']=='pending'
    assert before!=dependency_fingerprint(isolated)
    document=next(d for d in authorized_catalog_documents('eval-analyst') if d['id']=='catalog:metric-wiki:metric-demo-paid')
    assert '模型新草稿' in document['content'] and source_ref in document['content']
    from backend.catalog import vector_index
    monkeypatch.setattr(vector_index,'ensure_catalog_index',lambda: True)
    assert wiki.run_index_job(published['index_job_id'])['status']=='completed'
    con=isolated();job=con.execute('SELECT status FROM metric_wiki_index_jobs WHERE id=?',(published['index_job_id'],)).fetchone();con.close()
    assert job['status']=='completed'
    with pytest.raises(HTTPException) as error: wiki.publish('metric-demo-paid','eval-analyst',payload)
    assert error.value.status_code==409


def test_wiki_sources_are_content_addressed_and_immutable(isolated):
    con=isolated();con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','eval-analyst')");con.commit();con.close()
    payload=wiki.WikiSourceInput(source_type='meeting_record',title='口径评审会',
        source_uri='meeting://2026-09-15',content='结论：有效订单只统计 PAID 状态。')
    first=wiki.add_source('metric-demo-paid','eval-analyst',payload)
    second=wiki.add_source('metric-demo-paid','eval-analyst',payload)
    assert first['id']==second['id'] and len(first['content_hash'])==64
    con=isolated()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute('UPDATE metric_wiki_sources SET title=? WHERE id=?',('changed',first['id']))
    con.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute('DELETE FROM metric_wiki_sources WHERE id=?',(first['id'],))
    con.close()


def test_uploaded_metric_source_keeps_raw_file_and_deduplicates(isolated, monkeypatch, tmp_path):
    con = isolated()
    con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','eval-analyst')")
    con.commit()
    con.close()
    root = tmp_path / 'metric-sources'
    monkeypatch.setattr(source_files, 'METRIC_SOURCE_ROOT', root)
    raw = '正式口径：只统计 PAID 状态订单金额。'.encode('utf-8')
    first = wiki.upload_source_file('metric-demo-paid', 'eval-analyst', 'GMV口径.txt', 'text/plain', raw)
    second = wiki.upload_source_file('metric-demo-paid', 'eval-analyst', 'GMV口径.txt', 'text/plain', raw)
    assert first['id'] == second['id']
    assert first['source_type'] == 'uploaded_document'
    assert first['original_filename'] == 'GMV口径.txt'
    assert first['parser_name'] == 'text'
    assert first['byte_size'] == len(raw)
    assert (root.parent / first['storage_path']).read_bytes() == raw
    con = isolated()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute('DELETE FROM metric_wiki_sources WHERE id=?', (first['id'],))
    con.close()
def test_wiki_generation_repairs_non_verbatim_model_evidence(isolated,monkeypatch):
    con=isolated();con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','eval-analyst')");con.commit();con.close()
    calls=[]
    async def complete(*args):
        calls.append(args)
        evidence='概括后的证据' if len(calls)==1 else '仅统计 PAID 状态订单金额，排除取消和退款状态。'
        return wiki.WikiDraft(summary='已支付',content='待审核',
            claims=[{'claim_type':'definition','content':'只统计已支付订单',
                     'source_ref':'wiki-source:metric-demo-paid:catalog-v1','evidence':evidence}])
    monkeypatch.setattr(wiki,'complete_structured',complete)
    draft=asyncio.run(wiki.generate_draft('metric-demo-paid','eval-analyst'))
    assert len(calls)==2
    assert draft['claims'][0]['evidence']=='仅统计 PAID 状态订单金额，排除取消和退款状态。'


def test_failed_wiki_index_job_can_be_retried(isolated,monkeypatch):
    con=isolated();con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','eval-analyst')");con.commit()
    row=con.execute("SELECT * FROM catalog_metrics WHERE id='metric-demo-paid'").fetchone();con.close()
    review=wiki.MetricReview(expected_version=row['catalog_version'],aliases=[],
        contract=json.loads(row['contract_json']),wiki={'summary':'已审核','content':'已审核',
        'claims':[{'claim_type':'definition','content':'只统计已支付订单',
        'source_ref':'wiki-source:metric-demo-paid:catalog-v1',
        'evidence':'仅统计 PAID 状态订单金额，排除取消和退款状态。'}]})
    published=wiki.publish('metric-demo-paid','eval-analyst',review)
    from backend.catalog import vector_index
    monkeypatch.setattr(vector_index,'ensure_catalog_index',lambda: False)
    assert wiki.run_index_job(published['index_job_id'])['status']=='failed'
    assert wiki.retry_index('metric-demo-paid','eval-analyst')['status']=='pending'
    monkeypatch.setattr(vector_index,'ensure_catalog_index',lambda: True)
    assert wiki.run_index_job(published['index_job_id'])['status']=='completed'


def test_wiki_rejects_non_verbatim_or_unknown_evidence(isolated):
    con=isolated();con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','eval-analyst')");con.commit()
    row=con.execute("SELECT * FROM catalog_metrics WHERE id='metric-demo-paid'").fetchone();con.close()
    payload=wiki.MetricReview(expected_version=row['catalog_version'],aliases=[],
        contract=json.loads(row['contract_json']),wiki={'summary':'bad','content':'bad',
        'claims':[{'claim_type':'definition','content':'错误声明','source_ref':'wiki-source:missing','evidence':'并不存在'}]})
    with pytest.raises(HTTPException) as error:
        wiki.publish('metric-demo-paid','eval-analyst',payload)
    assert error.value.status_code==422


def test_metric_wiki_schema_files_are_versioned():
    root=Path(__file__).parents[1]/'backend'/'semantic_metrics'/'schema'
    assert 'schema_version: 1' in (root/'metric-wiki.schema.yaml').read_text(encoding='utf-8')
    assert 'append-only' in (root/'ingestion-rules.md').read_text(encoding='utf-8')
    assert 'verbatim' in (root/'publication-rules.md').read_text(encoding='utf-8')


def test_wiki_generation_requires_management(isolated):
    with pytest.raises(HTTPException) as error:
        asyncio.run(wiki.generate_draft('metric-demo-paid','eval-analyst'))
    assert error.value.status_code==403


def test_stream_and_normal_both_use_metric_path(isolated,monkeypatch):
    from backend import agent
    async def router(s): return {**s,'route':'data_query','memory_retrieval':'none'}
    monkeypatch.setattr(agent,'router',router)
    async def collect(): return [e async for e in agent.run_agent_stream('2026年8月演示已支付金额','data-query-agent',[],user_id='eval-analyst')]
    result=asyncio.run(collect())
    assert result[-1]['state']['result']==[{'paid_amount':150.0}]
    assert any(e['event']=='delta' for e in result)


def test_semantic_rag_match_is_recompiled_not_model_sql(isolated,monkeypatch):
    from backend import agent
    from backend.schemas import QueryPlanOutput
    from backend.query_quality.evidence import fingerprint
    docs=authorized_catalog_documents('eval-analyst')
    metric=next(d for d in docs if d['id']=='catalog:metric:metric-demo-paid')
    async def complete(_id,messages,schema,_model):
        if schema is QueryPlanOutput:
            return schema(answer='candidate',sql='SELECT SUM(amount) FROM orders',source_id='local-ecommerce-demo',
                          metric_refs=[{'id':metric['id'],'version':fingerprint(metric)}])
        return MetricRequest(start_date='2026-08-01',end_date='2026-09-01')
    monkeypatch.setattr(agent,'complete_structured',complete)
    monkeypatch.setattr(agent,'agent_cache',None)
    s=state('八月份成功成交的金额');s['rag']=[metric,*[d for d in docs if d['id']=='catalog:asset:asset-orders']]
    result=asyncio.run(agent.data_query_agent(s))
    assert result['query_mode']=='text_to_metrics'
    assert "'PAID'" in result['plan']['sql']


def test_postgresql_compilation_uses_same_contract(isolated):
    resolved=resolve_metric_query('演示已支付金额','eval-analyst')
    resolved.metrics[0].contract.dialect='postgresql'
    resolved.metrics[0].contract.namespace='ods'
    plan=compile_metric_query('2026-08演示已支付金额按日期',resolved,columns={'amount','status','order_date'})
    assert '"ods"."orders"' in plan['sql'] and 'CAST("order_date" AS DATE)' in plan['sql']


def test_denied_related_metric_hides_wiki_explanation(isolated):
    con=isolated()
    c=json.loads(con.execute("SELECT contract_json FROM catalog_metrics WHERE id='metric-demo-booked'").fetchone()[0])
    c['asset_id']='asset-products'
    con.execute("UPDATE catalog_metrics SET contract_json=? WHERE id='metric-demo-booked'",(json.dumps(c),))
    con.execute("INSERT INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect) VALUES ('deny-related','user','eval-analyst','asset','asset-products','discover','deny')")
    con.commit();con.close()
    assert not any(d['id']=='catalog:metric-wiki:metric-demo-paid' for d in authorized_catalog_documents('eval-analyst'))
