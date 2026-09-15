"""Reproducible isolated SQL benchmark; --live sends only synthetic fixtures."""
import argparse
import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def stats(values):
    ordered=sorted(values)
    return dict(n=len(values),p50_ms=round(statistics.median(values),2),p95_ms=round(ordered[math.ceil(len(values)*.95)-1],2))


async def benchmark(rounds,live):
    from backend import agent
    from backend.evaluation.environment import evaluation_environment
    from backend.semantic_metrics.nodes import metric_router
    from backend.semantic_metrics import repository
    from backend.semantic_metrics.resolver import resolve_metric_query
    from backend.semantic_metrics.compiler import compile_metric_query
    from backend.schemas import RouteOutput, QueryPlanOutput
    question='查询2026年8月演示已支付金额'
    report={'question':question,'live_model':live,'storage':'isolated synthetic SQLite','cache':'disabled',
            'baseline':'forced pre-change Text-to-SQL path; same question/data; current RAG and router', 'runs':[]}
    with tempfile.TemporaryDirectory(prefix='metric-benchmark-') as directory:
        with evaluation_environment(Path(directory)):
            local=[]
            for _ in range(100):
                start=time.perf_counter()
                resolution=resolve_metric_query(question,'eval-analyst')
                plan=compile_metric_query(question,resolution,columns={'amount','status','order_date'})
                assert plan['sql']
                local.append((time.perf_counter()-start)*1000)
            report['resolve_compile']=stats(local)
            original_complete=agent.complete_structured
            original_stream=agent.stream_text
            for number in range(rounds):
                for mode in ('text_to_sql','text_to_metrics'):
                    calls=[]
                    async def complete(agent_id,messages,schema,model_id):
                        calls.append(schema.__name__)
                        if live: return await original_complete(agent_id,messages,schema,model_id)
                        if schema is RouteOutput: return schema(route='data_query')
                        if schema is QueryPlanOutput:
                            return schema(answer='fixture',sql='SELECT SUM(amount) AS paid_amount FROM orders WHERE status=\'PAID\' AND order_date>=\'2026-08-01\' AND order_date<\'2026-09-01\'',source_id='local-ecommerce-demo',metric_refs=[{'id':resolution.metrics[0].document_id,'version':resolution.metrics[0].version}])
                        return schema(answer='fixture result: 150',summary='150')
                    async def stream(*args,**kwargs):
                        calls.append('stream_text')
                        if live:
                            async for value in original_stream(*args,**kwargs): yield value
                        else: yield 'fixture result: 150'
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(agent,'agent_cache',None))
                        stack.enter_context(patch.object(agent,'complete_structured',complete))
                        stack.enter_context(patch.object(agent,'stream_text',stream))
                        if mode=='text_to_sql':
                            stack.enter_context(patch.object(agent,'metric_router',lambda s:{**s,'query_mode':'text_to_sql','metric_resolution':{'status':'unmatched'}}))
                            stack.enter_context(patch.object(repository,'metric_records',lambda *a,**k:[]))
                        start=time.perf_counter(); first=None; final={}
                        try:
                            async for event in agent.run_agent_stream(question,'data-query-agent',[],user_id='eval-analyst'):
                                if event['event']=='delta' and first is None: first=(time.perf_counter()-start)*1000
                                if event['event']=='done': final=event['state']
                            rows=final.get('result',[])
                            correct=len(rows)==1 and any(float(v)==150 for v in rows[0].values() if v is not None)
                            error=None
                        except Exception as exc:
                            correct=False; error=type(exc).__name__+': '+str(exc)
                        entry=dict(mode=mode,round=number+1,first_token_ms=round(first,2) if first is not None else None,
                                   total_ms=round((time.perf_counter()-start)*1000,2),correct=correct,model_calls=calls,
                                   nodes=final.get('node_trace',[]),error=error)
                        report['runs'].append(entry)
                        print(json.dumps({k:v for k,v in entry.items() if k!='nodes'},ensure_ascii=False),flush=True)
            report['summary']={mode:stats([r['total_ms'] for r in report['runs'] if r['mode']==mode]) for mode in ('text_to_sql','text_to_metrics')}
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--output',default='.data/evals/semantic-metrics-benchmark.json');args=parser.parse_args()
    report=asyncio.run(benchmark(max(1,args.rounds),args.live))
    target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'resolve_compile':report['resolve_compile'],'summary':report['summary']},ensure_ascii=False))
    raise SystemExit(0 if all(r['correct'] for r in report['runs']) else 1)
