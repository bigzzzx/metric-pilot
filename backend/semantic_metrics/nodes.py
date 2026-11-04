"""Graph nodes reuse the existing schema and read-only SQL tool boundary."""
import asyncio
import json
from datetime import date
from backend.semantic_metrics.resolver import resolve_metric_query
from backend.semantic_metrics.models import MetricResolution
from backend.semantic_metrics.compiler import MetricRequest, fast_request, compile_metric_query
from backend.semantic_metrics.repository import metric_records
from backend.tools.models import ToolStatus
from backend.data_services.metrics import compile_verified_plan


def metric_router(state):
    if state.get('route')!='data_query' or not state.get('user_id'):
        return {**state,'query_mode':'text_to_sql','metric_resolution':{'status':'unmatched'}}
    resolution=resolve_metric_query(state['message'],state['user_id'])
    if resolution.status=='unmatched' and state.get('contextual_follow_up'):
        # Current explicit metric always wins; only the latest user topic may be inherited.
        recent=[m.get('content','') for m in state.get('history',[]) if m.get('role')=='user' and not m.get('content','').startswith('Tool result:')]
        context=recent[-1] if recent else state.get('conversation_summary','')[-800:]
        resolution=resolve_metric_query(context,state['user_id'])
    result={**state,'metric_resolution':resolution.model_dump(mode='json'),
            'query_mode':'text_to_sql' if resolution.status=='unmatched' else 'text_to_metrics'}
    if resolution.status not in ('matched','unmatched'):
        result['final']={'answer':resolution.message}
        if resolution.status=='denied':
            result['permission_error']={'code':'permission_denied','message':resolution.message}
    return result


def route_after_metric_router(state):
    status=state.get('metric_resolution',{}).get('status','unmatched')
    return 'metric' if status=='matched' else 'rag' if status=='unmatched' else 'finish'


async def metric_plan(state):
    from backend import agent
    resolution=MetricResolution.model_validate(state['metric_resolution'])
    metric=resolution.metrics[0]; contract=metric.contract
    def stop(answer):
        plan={'answer':answer,'sql':None}
        return {**state,'plan':plan,'final':plan}
    outcome=await asyncio.to_thread(agent.tool_executor.invoke,'catalog_schema',{'tables':[contract.asset_id]},
                                   agent._tool_context(state,'catalog_schema','data-query-agent'))
    tables=outcome.data.get('tables',[]) if outcome.status is ToolStatus.SUCCESS else []
    if len(tables)!=1:
        return stop('指标底层表未发布、数据源已停用或缺少 query_data 权限，请检查指标数据源。')
    table=tables[0]
    if (table.get('source_id'),table.get('namespace',''),table.get('name'),table.get('dialect')) != (
            contract.source_id,contract.namespace,contract.table,contract.dialect):
        return stop('指标契约与当前已发布表结构不一致，请重新审核指标。')
    request=fast_request(state['message'],metric)
    parameter_model_used=request is None
    if request is None:
        prompt=('Translate the current request and its active conversation context into metric parameters, NEVER SQL. '
                'Return start_date/end_date (ISO dates; end exclusive), dimensions (contract keys), intent (query/explain), clarification. '
                'Bind only this metric. If user asks another source, a different time field, multiple metrics, extra entity/status filters, '
                'unsupported dimensions, comparisons or transformations, return clarification in Chinese and do not discard any condition. '
                'Missing time must be clarified. Explain requests need no dates. Today: '+date.today().isoformat()+'\n'
                +json.dumps(metric.model_dump(mode='json'),ensure_ascii=False))
        request=await agent.complete_structured('data-query-agent',agent.messages(state,prompt),MetricRequest,state.get('model_id'))
    try:
        plan=compile_verified_plan(state['message'],resolution,request,table)
    except ValueError:
        return stop('指标契约引用了不可用字段或不支持的表达式，请重新审核指标。')
    schemas={'tables':tables,'unavailable':[]}
    quality=await asyncio.to_thread(agent.quality_context_for_schemas,state['user_id'],schemas)
    metric_evidence={'id':metric.document_id,'version':metric.version,'definition':metric.description,'required':True}
    rag=[{'id':metric.document_id,'kind':'metric','title':metric.display_name,'content':metric.description,
          'asset_ids':[contract.asset_id],'domain_id':metric.domain_id,'source_id':contract.source_id}]
    if metric.wiki.get('status')=='approved':
        rag.append({**rag[0],'id':'catalog:metric-wiki:'+metric.id,'kind':'metric_wiki',
                    'title':metric.display_name+' · 口径 Wiki','content':metric.wiki.get('content','')})
    return {**state,'plan':plan,'rag':rag,'query_metrics':[metric_evidence],'verified_schemas':schemas,
            'quality_risks':quality['risks'],'quality_notice':quality['notice'],
            'metric_parameter_model_used':parameter_model_used,
            'planner_source_id':state.get('planner_source_id'),
            'source_resolution_reason':'metric_contract_verified',
            **({'final':plan} if not plan.get('sql') else {})}


def current_contract_issue(state):
    if state.get('query_mode')!='text_to_metrics': return None
    expected=MetricResolution.model_validate(state['metric_resolution']).metrics[0]
    current=next((m for m in metric_records(state.get('user_id','')) if m.id==expected.id),None)
    if (not current or not current.can_query or current.version!=expected.version
            or current.contract!=expected.contract or current.certification_status!='certified'
            or not current.contract or current.contract.status!='active'):
        return '指标版本或查询权限已变化，请重新提问。'
    return None
