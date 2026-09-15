import asyncio
import json
from unittest.mock import patch
import pytest
from fastapi import HTTPException
from backend.evaluation.environment import evaluation_environment
from backend.semantic_metrics import nodes, wiki
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
    ('原始字段演示销售额','unmatched'),('2026年8月演示下单金额和演示已支付金额','ambiguous'),
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
    async def complete(*args): return wiki.WikiDraft(summary='已支付',content='模型新草稿，需要审核。')
    monkeypatch.setattr(wiki,'complete_structured',complete)
    asyncio.run(wiki.generate_draft('metric-demo-paid','eval-analyst'))
    docs=authorized_catalog_documents('eval-analyst')
    assert not any('模型新草稿' in d['content'] for d in docs)
    con=isolated();row=con.execute("SELECT * FROM catalog_metrics WHERE id='metric-demo-paid'").fetchone();con.close()
    payload=wiki.MetricReview(expected_version=row['catalog_version'],aliases=['演示已支付金额'],
                             contract=json.loads(row['contract_json']),wiki={'summary':'已审核','content':'模型新草稿，需要审核。'})
    wiki.publish('metric-demo-paid','eval-analyst',payload)
    assert before!=dependency_fingerprint(isolated)
    assert any('模型新草稿' in d['content'] for d in authorized_catalog_documents('eval-analyst'))
    with pytest.raises(HTTPException) as error: wiki.publish('metric-demo-paid','eval-analyst',payload)
    assert error.value.status_code==409


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
