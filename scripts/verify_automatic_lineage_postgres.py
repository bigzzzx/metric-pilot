"""Isolated real PostgreSQL + authenticated HTTP acceptance; no model calls."""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def check(condition, label, results):
    if not condition:
        raise AssertionError(label)
    results.append(label)


def until(probe, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := probe():
            return result
        time.sleep(.2)
    raise AssertionError('Timed out waiting for automatic lineage background work')


def seed_postgres(dsn, password):
    import psycopg
    from psycopg import sql
    with psycopg.connect(dsn) as con:
        con.execute('CREATE SCHEMA mp_lineage')
        con.execute('CREATE TABLE mp_lineage.orders (amount numeric, alternate_amount numeric, status text)')
        con.execute("INSERT INTO mp_lineage.orders VALUES (100, 400, 'PAID'), (50, 700, 'REFUNDED')")
        con.execute('''CREATE VIEW mp_lineage.paid_sales AS
            SELECT amount AS sale FROM mp_lineage.orders WHERE status = 'PAID' ''')
        con.execute('''CREATE MATERIALIZED VIEW mp_lineage.sales_snapshot AS
            SELECT sale AS amount FROM mp_lineage.paid_sales''')
        con.execute('CREATE VIEW mp_lineage.hidden_view AS SELECT alternate_amount FROM mp_lineage.orders')
        for name, kind in [('orders','TABLE'),('paid_sales','VIEW'),('sales_snapshot','MATERIALIZED VIEW')]:
            con.execute(sql.SQL('COMMENT ON {} {} IS {}').format(sql.SQL(kind), sql.Identifier('mp_lineage',name),
                        sql.Literal('合成血缘验收；业务域=电商；粒度=订单')))
        for table, columns in [('orders',('amount','alternate_amount','status')),('paid_sales',('sale',)),('sales_snapshot',('amount',))]:
            for column in columns:
                con.execute(sql.SQL('COMMENT ON COLUMN {} IS {}').format(sql.Identifier('mp_lineage',table,column),sql.Literal('合成字段 '+column)))
        con.execute(sql.SQL('CREATE ROLE mp_lineage_reader LOGIN PASSWORD {}').format(sql.Literal(password)))
        con.execute('ALTER ROLE mp_lineage_reader SET default_transaction_read_only=on')
        con.execute('GRANT USAGE ON SCHEMA mp_lineage TO mp_lineage_reader')
        con.execute('GRANT SELECT ON mp_lineage.orders, mp_lineage.paid_sales, mp_lineage.sales_snapshot TO mp_lineage_reader')


def validate(dsn, settings):
    import psycopg
    from fastapi.testclient import TestClient
    from backend.auth import SESSION_COOKIE, create_session
    from backend.core import db
    from backend.main import app
    from backend.lineage.worker import scan_due_source
    from backend.lineage.metrics import metric_lineage
    from backend.tools.sql_tool import run_readonly_sql

    results, performance = [], {}
    seed_postgres(dsn, settings['password'])
    source_config = dict(name='合成 PostgreSQL 血缘验收', kind='postgresql',
        endpoint=settings['host']+':'+settings['port'], catalog_name=settings['dbname'],
        username='mp_lineage_reader',credential=settings['password'],enabled=True,auto_lineage_interval_minutes=5)
    with TestClient(app) as admin:
        registered=admin.post('/api/auth/register',json={'username':'lineage_acceptance_admin','password':uuid4().hex})
        check(registered.status_code in (200,201),'真实会话注册',results)
        user_id=admin.get('/api/auth/me').json()['id']
        response=admin.post('/api/data-sources',json=source_config)
        check(response.status_code==200 and response.json()['auto_lineage_interval_minutes']==5,'平台管理员开启单源自动采集',results)
        source_id=response.json()['id']
        def changes():
            return admin.get(f'/api/data-sources/{source_id}/changes').json()
        def initial_scan():
            collector=admin.get('/api/catalog/evidence/collector').json()
            if any(item['status']=='failed' for item in collector['sources']):
                manual=admin.post(f'/api/data-sources/{source_id}/sync')
                raise AssertionError(f"Initial automatic scan failed: {collector['sources'][0]['last_error']}; manual status {manual.status_code}")
            return changes()
        initial=until(initial_scan)
        check({item['entity_key'] for item in initial}=={'mp_lineage.orders','mp_lineage.paid_sales','mp_lineage.sales_snapshot'},
              '后台自动发现表、普通视图、物化视图；无 SELECT 权限的视图不采集',results)
        check(all('view_definition' not in (item.get('after') or {}) for item in initial),'审核列表不返回视图正文',results)
        pending_view=next(item for item in initial if item['entity_key']=='mp_lineage.paid_sales')
        review_url=f'/api/catalog/admin/data-sources/{source_id}/view-definition'
        check('amount AS sale' in admin.get(review_url,params={'change_id':pending_view['id']}).json()['definition'],
              '管理员可按变更版本核对真实视图正文',results)

        def apply_changes(items):
            response=admin.post(f'/api/data-sources/{source_id}/changes/batch-decision',json={
                'decision':'approve','reason':'核对隔离 PostgreSQL 合成定义','change_ids':[c['id'] for c in items]})
            if response.status_code!=200:
                raise AssertionError('Metadata approval failed')
        apply_changes(initial)
        assets={item['physical_name']:item for item in admin.get('/api/catalog/assets',params={'source_id':source_id}).json()}
        response=admin.post('/api/catalog/admin/assets/batch-publish',json={'items':[
            {'asset_id':item['id'],'version':item['catalog_version']} for item in assets.values()]})
        check(response.status_code==200 and response.json()['published_count']==3,'已有目录审批/发布链路保留',results)
        orders, view, mat=(assets[name]['id'] for name in ('orders','paid_sales','sales_snapshot'))
        def field(aid, column, **extra):
            response=admin.get(f'/api/catalog/assets/{aid}/field-lineage',params={'column':column,**extra})
            assert response.status_code==200
            return response.json()
        view_line=until(lambda: (item if (item:=field(view,'sale',include_influences=True))['view_status']=='parsed' else None))
        until(lambda: field(mat,'amount')['view_status']=='parsed')
        check({(e['from_column'],e['kind']) for e in view_line['edges']}=={('amount','column_value'),('status','column_influence')},
              '后台从真实视图定义区分数值与过滤影响',results)
        check(any(e['from_asset']['id']==view for e in field(mat,'amount')['edges']),'物化视图连接上游普通视图',results)

        metric=next(m for m in admin.get('/api/catalog/metrics').json() if m['id']=='metric-demo-paid')
        source=metric['wiki_sources'][0]
        contract=dict(source_id=source_id,asset_id=view,namespace='mp_lineage',table='paid_sales',dialect='postgresql',
                      measure_expression='SUM(sale)',measure_alias='paid_amount',requires_time_range=False)
        response=admin.put('/api/catalog/admin/metrics/metric-demo-paid/publish',json=dict(expected_version=metric['catalog_version'],
            contract=contract,wiki=dict(summary='合成订单指标',content='仅支付订单',claims=[dict(claim_type='definition',
                content='合成指标',source_ref=source['id'],evidence=source['content'][:8])])) )
        check(response.status_code==200 and response.json()['lineage_count']==2,'发布认证指标同时生成版本化血缘',results)
        metric_url='/api/catalog/metrics/metric-demo-paid/lineage'
        started=time.perf_counter()
        line=admin.get(metric_url).json()
        performance['metric_lineage_http_ms']=round((time.perf_counter()-started)*1000,2)
        check(line['status']=='current' and {e['from_column'] for e in line['upstream_edges']}=={'amount','status'},
              '真实端到端：源字段 → 视图字段 → 已认证指标',results)
        check(field(orders,'status',direction='downstream')['metric_impacts'][0]['metric_id']=='metric-demo-paid',
              '反向可定位固定过滤影响的指标',results)
        service_metric=next(m for m in admin.get('/api/data-services/metrics').json() if m['metric_id']=='metric-demo-paid')
        request=dict(metric_id='metric-demo-paid',version=service_metric['version'])
        answer=admin.post('/api/data-services/metrics/query',json=request)
        check(answer.status_code==200 and Decimal(answer.json()['rows'][0]['paid_amount'])==100,'审核后的视图实际取数正确',results)
        check(Decimal(run_readonly_sql('SELECT sum(amount) AS n FROM mp_lineage.sales_snapshot',user_id,source_id)[0]['n'])==100,
              '物化视图只读定义校验兼容 PostgreSQL 16',results)

        con=db()
        with con:
            con.execute("INSERT INTO users(id,username,password_hash) VALUES ('no-view-upstream','limited','unused')")
            con.execute("INSERT INTO catalog_group_memberships VALUES ('group-ecommerce-analysts','no-view-upstream',CURRENT_TIMESTAMP)")
            con.execute('''INSERT INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect)
                VALUES ('limited-orders','user','no-view-upstream','asset',?,'query_data','deny')''',(orders,))
        con.close()
        limited=TestClient(app)
        limited.cookies.set(SESSION_COOKIE,create_session('no-view-upstream'))
        check(limited.get(review_url,params={'change_id':pending_view['id']}).status_code==403,'普通用户无法读取原始定义',results)
        check(limited.post('/api/data-services/metrics/query',json=request).status_code==403,'视图查询不能绕过上游 query_data 权限',results)
        until(lambda: admin.get('/api/catalog/evidence/collector').json()['queries'].get('parsed',0)>=1)
        check(any(s['source_kind']=='query_execution' for s in admin.get('/api/catalog/evidence/sources').json()),
              '成功查询由实际后台线程自动解析，无需手动点击',results)

        with psycopg.connect(dsn) as pg:
            pg.execute("CREATE OR REPLACE VIEW mp_lineage.paid_sales AS SELECT alternate_amount AS sale FROM mp_lineage.orders WHERE status='PAID'")
        check(admin.post('/api/data-services/metrics/query',json=request).status_code==409,'DDL 变更未扫描前，实时查询也不会使用旧审核',results)
        # Trigger next scheduled tick without waiting five wall-clock minutes.
        check(scan_due_source(db,clock=lambda:time.time()+301),'定时增量采集第二个真实快照',results)
        pending=until(changes)
        check(len(pending)==1 and pending[0]['summary']['view_definition_changed'],'只把变更视图送入审核队列',results)
        check(not field(view,'sale')['edges'] and admin.get(metric_url).json()['status']=='unavailable','旧边和指标血缘立即撤出当前视图',results)
        apply_changes(pending)
        until(lambda: (item if (item:=metric_lineage(user_id,'metric-demo-paid'))['status']=='current' else None))
        changed=admin.get(metric_url).json()
        check({e['from_column'] for e in changed['upstream_edges']}=={'alternate_amount','status'},'审核新定义后生成新血缘，不复用旧字段',results)
        answer=admin.post('/api/data-services/metrics/query',json=request)
        check(answer.status_code==200 and Decimal(answer.json()['rows'][0]['paid_amount'])==400,'重新审核后按新定义返回黄金值',results)
    return dict(passed=len(results),total=len(results),checks=results,llm_calls=0,postgresql='disposable synthetic',performance=performance)


def main():
    from cryptography.fernet import Fernet
    from psycopg.conninfo import conninfo_to_dict
    import backend.core as core
    dsn=os.environ.get('METRIC_PILOT_TEST_PG_DSN','')
    settings=conninfo_to_dict(dsn)
    if (settings.get('dbname')!='metric_pilot_acceptance' or settings.get('user')!='mp_owner'
            or settings.get('host')!='127.0.0.1' or not settings.get('password') or not settings.get('port')):
        raise SystemExit('Require a disposable metric_pilot_acceptance PostgreSQL via local tunnel')
    with tempfile.TemporaryDirectory(prefix='mp-auto-lineage-') as directory:
        core.DATA_DIR=Path(directory)
        core.PLATFORM_DB=core.DATA_DIR/'platform.db'
        core.DEMO_DB=core.DATA_DIR/'warehouse.db'
        core.db.__defaults__=(core.PLATFORM_DB,)
        os.environ.update(APP_ENCRYPTION_KEY=Fernet.generate_key().decode(),VECTOR_STORE_BACKEND='sqlite',
                          EMBEDDING_BACKEND='hash',RERANK_BACKEND='none',LINEAGE_COLLECTOR_ENABLED='1',
                          PRODUCTION_RECOVERY_MONITOR_ENABLED='0',CATALOG_INDEX_WORKER_ENABLED='0')
        os.environ.pop('DEEPSEEK_API_KEY',None)
        try:
            result=validate(dsn,settings)
        finally:
            from backend.checkpoint import _connection
            _connection.close()
            for handler in list(logging.getLogger('data_agent').handlers):
                logging.getLogger('data_agent').removeHandler(handler)
                handler.close()
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__ == '__main__':
    main()
