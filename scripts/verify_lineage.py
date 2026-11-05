"""Disposable HTTP/RAG/SQL lineage acceptance and optional synthetic live-model check."""
import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def verify(directory,live_config=None):
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.auth import create_session,SESSION_COOKIE
    from backend.evaluation.data_platform import data_platform_environment
    from backend.evaluation.lineage import prepare_lineage_fixture,GOLDEN_SQL,DOCUMENT
    from backend.lineage.catalog import assets_for
    from backend.lineage.sql_parser import parse_sql
    from backend.lineage import service,documents
    from backend.lineage.models import EvidenceInput
    from backend.catalog.rag_documents import authorized_catalog_documents
    from backend.tools.cache import dependency_fingerprint
    from backend.rag import retrieve
    checks=[]
    def check(name,value):
        checks.append(dict(name=name,passed=bool(value)))
        if not value: raise AssertionError(name)
    with data_platform_environment(directory) as (factory,_,warehouse),ExitStack() as stack:
        prepare_lineage_fixture(factory,warehouse)
        stack.enter_context(patch('backend.catalog.vector_index.ensure_catalog_index',return_value=False))
        admin,analyst=TestClient(app),TestClient(app)
        stack.callback(admin.close);stack.callback(analyst.close)
        admin.cookies.set(SESSION_COOKIE,create_session('eval-admin'))
        analyst.cookies.set(SESSION_COOKIE,create_session('eval-analyst'))
        payload=dict(source_kind='sql_import',title='地区销售汇总 / synthetic',content=GOLDEN_SQL,
            source_id='local-ecommerce-demo',source_uri='repo://synthetic/region-sales',source_version='1')
        started=time.perf_counter()
        response=admin.post('/api/catalog/evidence/import',json=payload)
        check('authenticated HTTP SQL evidence import',response.status_code==200)
        source=response.json();check('parser bound to real fixture schema',source['state']['analysis']['status']=='parsed')
        check('idempotent source import',admin.post('/api/catalog/evidence/import',json=payload).json()['idempotent'])
        candidates=admin.get('/api/catalog/evidence/candidates').json()
        value_edges=[e for e in candidates if e['kind']=='column_value']
        expected={('order_time','stat_date'),('region','region'),('order_amount','paid_amount')}
        check('exact three golden value origins',len(value_edges)==3 and {(e['from_column'],e['to_column']) for e in value_edges}==expected)
        check('join candidate only, not silently business approved',any(e['kind']=='join' and e['status']=='draft' and not e['review'] for e in candidates))
        check('unapproved candidates absent to analyst',analyst.get('/api/catalog/evidence/candidates').json()==[])
        check('raw source remains admin-only',analyst.get('/api/catalog/evidence/sources/'+source['id']).status_code==403)
        before=dependency_fingerprint(factory)
        for item in candidates:
            cid=item['id']
            review=admin.post('/api/catalog/evidence/candidates/'+cid+'/review',json=dict(expected_version=1,decision='approve',reason='合成黄金材料业务确认'))
            check('review '+item['kind']+' '+cid[-6:],review.status_code==200)
            published=admin.post('/api/catalog/evidence/candidates/'+cid+'/publish',json=dict(expected_version=2))
            check('explicit publish '+cid[-6:],published.status_code==200)
        lifecycle_ms=round((time.perf_counter()-started)*1000,2)
        check('publication changes shared cache identity',before!=dependency_fingerprint(factory))
        up=analyst.get('/api/catalog/assets/test-sales_by_region/field-lineage',params=dict(column='paid_amount')).json()
        check('field upstream value only',len(up['edges'])==1 and up['edges'][0]['from_column']=='order_amount')
        influence=analyst.get('/api/catalog/assets/test-sales_by_region/field-lineage',params=dict(column='paid_amount',include_influences=True)).json()
        check('filters remain indirect influence',any(e['kind']=='column_influence' and e['from_column']=='status' for e in influence['edges']))
        down=analyst.get('/api/catalog/assets/test-fact_orders/field-lineage',params=dict(column='order_amount',direction='downstream')).json()
        check('downstream impact target',any(e['to_column']=='paid_amount' for e in down['edges']))
        retrieved=retrieve('fact_orders 和 customers 通过哪个字段关联',8,user_id='eval-analyst')
        check('Agent RAG retrieves approved new join',any(e.get('lineage_candidate_id') and e['kind']=='relation' for e in retrieved))
        physical=factory(warehouse)
        check('import never writes the materialized table',physical.execute('SELECT COUNT(*) FROM sales_by_region').fetchone()[0]==0)
        physical.close()
        con=factory();assets=assets_for(con,'eval-admin');con.close()
        parse_times=[]
        for _ in range(30):
            start=time.perf_counter();parsed=parse_sql(GOLDEN_SQL,assets,dialect='sqlite');parse_times.append((time.perf_counter()-start)*1000)
            if parsed['status']!='parsed':raise AssertionError(parsed['errors'])
        http_times=[]
        for _ in range(30):
            start=time.perf_counter();response=analyst.get('/api/catalog/assets/test-sales_by_region/field-lineage',params=dict(column='paid_amount'));http_times.append((time.perf_counter()-start)*1000)
            if response.status_code!=200 or len(response.json()['edges'])!=1:raise AssertionError('field lineage benchmark correctness')
        # Withdraw while the vector refresh is deliberately unavailable.
        cid=value_edges[0]['id']
        check('withdraw HTTP',admin.post('/api/catalog/evidence/candidates/'+cid+'/withdraw',json=dict(expected_version=3)).status_code==200)
        check('withdrawn record filtered before vector refresh',not any(d.get('lineage_candidate_id')==cid for d in authorized_catalog_documents('eval-analyst')))
        model=dict(status='not_requested',scope='synthetic schema/material only')
        if live_config:
            stack.enter_context(patch('backend.model_client._configured_model',return_value=live_config))
            doc=service.import_evidence(EvidenceInput(title='Synthetic relationship specification',source_kind='business_document',content=DOCUMENT,
                source_id='local-ecommerce-demo',asset_ids=['test-fact_orders','test-customers','test-sales_by_region']),'eval-admin')
            start=time.perf_counter()
            async def bounded():
                async with asyncio.timeout(100):return await documents.generate_document_candidates(doc['id'],'eval-admin')
            try:
                result=asyncio.run(bounded())
                model=dict(provider=live_config['provider_name'],model=live_config['model_id'],duration_ms=round((time.perf_counter()-start)*1000,2),
                    scope='synthetic schema/material only',**result)
                generated=service.list_candidates('eval-admin',source_ref=doc['id'])
                model['candidates_remain_unpublished']=all(e['status']=='draft' for e in generated)
                model['exact_quote_validated']=all(e['locator'].get('quote') in DOCUMENT for e in generated)
                model['actual_bound_schema']=all(e['from_asset_id'] in assets and e['to_asset_id'] in assets for e in generated)
            except TimeoutError:
                model=dict(status='timeout',duration_ms=round((time.perf_counter()-start)*1000,2),scope='synthetic schema/material only')
        def timing(samples):return dict(samples=len(samples),p50=round(statistics.median(samples),2),p95=round(sorted(samples)[28],2))
        return dict(data='isolated synthetic fixtures',passed=sum(c['passed'] for c in checks),total=len(checks),checks=checks,
            parse_ms=timing(parse_times),field_lineage_http_asgi_ms=timing(http_times),all_candidate_review_lifecycle_ms=lifecycle_ms,
            live_model=model,limitation='ASGI timings exclude browser TCP. Golden correctness is not production-wide discovery accuracy.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live-model',action='store_true',help='Send ONLY synthetic schema/material to configured provider')
    parser.add_argument('--output',type=Path)
    options=parser.parse_args()
    import backend.core as core
    live_config=None
    configuration_error=None
    if options.live_model:
        from backend.model_client import _configured_model
        try:live_config=_configured_model('data-query-agent',None)
        except Exception as exc:configuration_error=type(exc).__name__
    os.environ.update(EMBEDDING_BACKEND='hash',VECTOR_STORE_BACKEND='sqlite',RERANK_BACKEND='none',MEMORY_VECTOR_BACKEND='disabled')
    if not options.live_model:
        from cryptography.fernet import Fernet
        os.environ['APP_ENCRYPTION_KEY']=Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory(prefix='metric-pilot-lineage-') as directory:
        core.DATA_DIR=Path(directory);core.PLATFORM_DB=core.DATA_DIR/'platform.db';core.DEMO_DB=core.DATA_DIR/'warehouse.db';core.db.__defaults__=(core.PLATFORM_DB,)
        try:result=verify(Path(directory),live_config)
        finally:
            from backend.checkpoint import _connection
            _connection.close()
    if configuration_error:result['live_model']=dict(status='configuration_unavailable',error_type=configuration_error)
    report=json.dumps(result,ensure_ascii=True,indent=2)
    if options.output:
        options.output.parent.mkdir(parents=True,exist_ok=True);options.output.write_text(report,encoding='utf-8')
    print(report)


if __name__=='__main__':main()
