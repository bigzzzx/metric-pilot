"""Read-only real PostgreSQL acceptance using an isolated catalog, not user drafts.

Run with --llm to additionally use the configured query model for two planning turns.
Credentials are read through the normal source connector and never printed/copied.
"""
import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    args = argparse.ArgumentParser()
    args.add_argument('--source-id')
    args.add_argument('--llm', action='store_true')
    options = args.parse_args()
    from backend.core import db, DATA_DIR
    from backend.data_sources.service import connector_from_row
    from backend.main import init_storage
    from backend.catalog import service, publications, rag_documents
    from backend.query_quality import schema_lookup
    from backend.tools import sql_tool
    from backend.data_sources import postgresql_query
    con = db()
    sources = con.execute("SELECT * FROM data_sources WHERE kind='postgresql' AND enabled=1").fetchall()
    con.close()
    sources = [s for s in sources if not options.source_id or s['id'] == options.source_id]
    if len(sources) != 1:
        raise SystemExit('Provide --source-id selecting exactly one enabled PostgreSQL source.')
    connector = connector_from_row(sources[0])
    discovery = connector.discover()
    with tempfile.TemporaryDirectory(prefix='postgres-acceptance-', dir=DATA_DIR) as folder, ExitStack() as connections:
        def factory():
            connection = sqlite3.connect(Path(folder) / 'catalog.db', check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connections.callback(connection.close)
            return connection
        init_storage(factory)
        con = factory()
        con.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('pg-check','pg-check','not-a-login',1)")
        con.execute("INSERT INTO catalog_user_roles(user_id,role_id) VALUES ('pg-check','role-platform-admin')")
        con.execute("INSERT INTO data_sources(id,name,kind,endpoint,catalog_name,auth_type,credential,enabled,username) VALUES ('pg-check-source','Acceptance','postgresql','unused','unused','none','',1,'unused')")
        con.commit(); con.close()
        with ExitStack() as stack:
            for module in (service, publications, rag_documents, schema_lookup, sql_tool):
                stack.enter_context(patch.object(module, 'db', factory))
            stack.enter_context(patch.object(postgresql_query, 'connector_from_row', lambda _row: connector))
            publications.import_discovery_as_draft('pg-check-source', discovery, 'pg-check')
            con = factory()
            con.execute("UPDATE catalog_assets SET publish_status='published' WHERE source_id='pg-check-source'")
            con.execute("UPDATE catalog_columns SET sensitivity='sensitive',masking_strategy='redact' WHERE physical_name IN ('customer_name','phone','email') AND asset_id IN (SELECT id FROM catalog_assets WHERE source_id='pg-check-source')")
            customers = con.execute("SELECT id FROM catalog_assets WHERE source_id='pg-check-source' AND namespace='ods' AND physical_name='customers'").fetchone()['id']
            con.commit(); con.close()
            cases = {
                'count': 'SELECT count(*) AS n FROM ods.orders',
                'join': 'SELECT c.region,count(*) AS n FROM ods.orders o JOIN ods.customers c ON o.customer_id=c.customer_id GROUP BY c.region',
                'cte': 'WITH x AS (SELECT order_amount FROM ods.orders) SELECT sum(order_amount) AS total FROM x',
                'dates': "SELECT date_trunc('month', order_time) AS month,count(*) AS n FROM ods.orders GROUP BY 1 ORDER BY 1",
                'row_limit': 'SELECT order_id FROM ods.orders ORDER BY order_id',
                'like_pattern': "SELECT count(*) AS n FROM ods.orders WHERE status LIKE 'PA%'",
                'three_table_paid_gmv': """SELECT p.category, COUNT(DISTINCT o.order_id) AS order_count,
                    SUM(oi.item_amount) AS item_gmv
                    FROM ods.orders o
                    JOIN ods.order_items oi ON oi.order_id=o.order_id
                    JOIN ods.products p ON p.product_id=oi.product_id
                    WHERE o.status='PAID'
                    GROUP BY p.category ORDER BY p.category""",
            }
            for name, sql in cases.items():
                rows = sql_tool.run_readonly_sql(sql, 'pg-check', 'pg-check-source')
                if name == 'count': assert rows == [{'n':1200}], rows
                if name == 'join': assert sum(row['n'] for row in rows) == 1200
                if name == 'row_limit': assert len(rows) == 100
                if name == 'three_table_paid_gmv':
                    assert rows == [
                        {'category':'家居','order_count':480,'item_gmv':'108000.00'},
                        {'category':'数码','order_count':480,'item_gmv':'108000.00'},
                        {'category':'服饰','order_count':480,'item_gmv':'120000.00'},
                        {'category':'食品','order_count':480,'item_gmv':'120000.00'},
                    ], rows
                print(json.dumps({'check':name,'rows':len(rows),'sample':rows[:2]}, ensure_ascii=False))
            sample = service.sample_asset('pg-check', customers)
            assert len(sample['rows']) == 5
            assert all(row['phone'] == row['email'] == row['customer_name'] == '***' for row in sample['rows'])
            print('PASS sample: five rows, sensitive fields masked in SQL')
            if options.llm:
                asyncio.run(check_model(factory))


async def check_model(factory):
    from backend import agent
    from backend.query_quality import schema_lookup
    from backend.catalog.rag_documents import published_catalog_documents
    from backend.tools.sql_tool import run_readonly_sql
    from backend.tools.models import ToolResult, ToolStatus
    con = factory()
    ids = [r['id'] for r in con.execute("SELECT id FROM catalog_assets WHERE source_id='pg-check-source' AND namespace='ods' AND physical_name IN ('orders','customers')")]
    con.close()
    docs = [d for d in published_catalog_documents() if d['kind']=='table' and set(d.get('asset_ids',[])) & set(ids)]
    class Tools:
        def invoke(self, tool_id, payload, context):
            if tool_id == 'catalog_schema':
                data = schema_lookup.lookup_schema(schema_lookup.SchemaLookupInput.model_validate(payload), context)
            elif tool_id == 'readonly_sql':
                data = {'rows':run_readonly_sql(payload['sql'],context.user_id,payload.get('source_id'))}
            else:
                raise AssertionError(tool_id)
            return ToolResult(status=ToolStatus.SUCCESS,tool_id=tool_id,call_id=context.call_id,data=data)
    history=[]
    with patch.object(agent,'tool_executor',Tools()):
        for question in ['请查询 PostgreSQL 测试数仓 ods.orders 的订单总行数，不需要指标口径，只统计表行数。', '继续用刚才的数据源和订单表，按客户 region 分组统计订单行数，关联 ods.customers。']:
            state = {'user_id':'pg-check','message':question,'history':history,'rag':docs,'route':'data_query','contextual_follow_up':bool(history)}
            plan = await agent.data_query_agent(state)
            assert plan['plan']['sql'] and plan['plan']['source_id']=='pg-check-source', plan['plan']
            result = agent.query_tool(plan)
            assert not result.get('query_error'), result.get('query_error')
            if not history:
                assert 1200 in result['result'][0].values()
            else:
                assert sum(value for row in result['result'] for value in row.values() if isinstance(value,int)) == 1200
            print(json.dumps({'check':'llm_plan_execute','source_id':plan['plan']['source_id'],'sql':result['sql'],'rows':len(result['result'])},ensure_ascii=False))
            history.extend([{'role':'user','content':question},{'role':'assistant','content':json.dumps({'source_id':plan['plan']['source_id'],'sql':result['sql'],'result':result['result']},ensure_ascii=False)}])


if __name__ == '__main__':
    main()
