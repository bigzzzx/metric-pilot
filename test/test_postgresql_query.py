import datetime
from decimal import Decimal
import pytest
from fastapi import HTTPException
from backend.tools import sql_tool
from backend.catalog import service
from backend.data_sources import postgresql_query
from backend.query_quality import schema_lookup
from backend.query_quality.sql_preflight import SQLSchemaError
from backend.tools.models import ToolExecutionContext
from test.test_catalog_review import catalog


@pytest.fixture
def pg(catalog, monkeypatch):
    monkeypatch.setattr(sql_tool, 'db', catalog)
    monkeypatch.setattr(schema_lookup, 'db', catalog)
    con = catalog()
    con.execute("UPDATE catalog_assets SET publish_status='published' WHERE id='asset-source-test'")
    for name, sensitivity, kind in [('amount','internal','numeric'),('created_at','internal','timestamp'),('email','sensitive','text')]:
        con.execute("INSERT INTO catalog_columns(id,asset_id,physical_name,display_name,data_type,ordinal,description,sensitivity) VALUES (?,'asset-source-test',?,?,?,2,?,?)", (name,name,name,kind,name,sensitivity))
    con.commit(); con.close()
    calls=[]
    monkeypatch.setattr(sql_tool, 'execute_postgres', lambda source, sql: calls.append((source['id'],sql)) or [{'n': 7}])
    return catalog, calls


@pytest.mark.parametrize('sql', [
    'SELECT count(*) AS n FROM ods.orders',
    'SELECT o.order_id, sum(o.amount) FROM ods.orders o GROUP BY o.order_id',
    "SELECT date_trunc('month', created_at),sum(amount) FROM ods.orders GROUP BY 1",
    'SELECT DATE(created_at),count(*) FROM ods.orders GROUP BY 1',
    "SELECT sum(amount)-1 FROM ods.orders WHERE amount>=0 AND created_at>='2026-08-01'",
    'WITH x AS (SELECT amount FROM ods.orders) SELECT sum(amount) FROM x',
    'SELECT a.order_id FROM ods.orders a JOIN ods.orders b ON a.order_id=b.order_id',
    'SELECT o.order_id FROM ods.orders o WHERE EXISTS (SELECT 1 FROM ods.orders i WHERE i.order_id=o.order_id)',
])
def test_select_join_cte_and_aggregate(pg, sql):
    _, calls = pg
    assert sql_tool.run_readonly_sql(sql, 'admin', 'source-test') == [{'n':7}]
    assert calls[0][0] == 'source-test'
    assert '"ods"."orders"' in calls[0][1]


@pytest.mark.parametrize('sql', [
    'SELECT email FROM ods.orders', 'SELECT * FROM ods.orders',
    'SELECT order_id FROM ods.orders WHERE email IS NOT NULL',
    'WITH x AS (SELECT email AS x FROM ods.orders) SELECT x FROM x',
    'SELECT min(o.*) FROM ods.orders o',
    'SELECT o.order_id FROM ods.orders o WHERE EXISTS (SELECT 1 FROM ods.orders i WHERE o.email=i.email)',
    'SELECT pg_sleep(1) FROM ods.orders', 'SELECT public.evil() FROM ods.orders',
    'SELECT set_config(\'search_path\',\'public\',true) FROM ods.orders',
    'SELECT * INTO scratch FROM ods.orders',
    'WITH x AS (DELETE FROM ods.orders RETURNING *) SELECT * FROM x',
    'SELECT order_id FROM ods.orders; DELETE FROM ods.orders',
    'SELECT * FROM pg_catalog.pg_authid',
])
def test_unsafe_or_sensitive_queries_never_connect(pg, sql):
    _, calls = pg
    with pytest.raises(HTTPException):
        sql_tool.run_readonly_sql(sql, 'admin', 'source-test')
    assert calls == []


def test_schema_source_ambiguity_and_disabled_source(pg):
    factory, calls = pg
    with pytest.raises(HTTPException) as ambiguous:
        sql_tool.run_readonly_sql('SELECT order_id FROM orders', 'admin')
    assert ambiguous.value.code == 'source_ambiguous'
    with pytest.raises(HTTPException) as cross:
        sql_tool.run_readonly_sql('SELECT o.order_id FROM ods.orders o JOIN products p ON true','admin')
    assert cross.value.code == 'cross_source_query'
    with pytest.raises(SQLSchemaError):
        sql_tool.run_readonly_sql('SELECT missing FROM ods.orders','admin','source-test')
    con=factory(); con.execute("UPDATE data_sources SET enabled=0 WHERE id='source-test'"); con.commit(); con.close()
    assert not service.get_asset('admin','asset-source-test')['can_query']
    with pytest.raises(HTTPException):
        sql_tool.run_readonly_sql('SELECT order_id FROM ods.orders','admin','source-test')
    assert not calls


def test_schema_lookup_is_source_aware(pg):
    context = ToolExecutionContext(user_id='admin',agent_id='data-query-agent',call_id='test')
    assert not schema_lookup.lookup_schema(schema_lookup.SchemaLookupInput(tables=['orders']),context)['tables']
    table = schema_lookup.lookup_schema(schema_lookup.SchemaLookupInput(tables=['asset-source-test']),context)['tables'][0]
    assert (table['source_id'],table['namespace'],table['dialect']) == ('source-test','ods','postgresql')
    assert 'email' not in [c['name'] for c in table['columns']]


def test_pg_sample_masks_at_database_boundary(pg,monkeypatch):
    statements=[]
    monkeypatch.setattr(postgresql_query,'execute_postgres',lambda source,sql,limit: statements.append((sql,limit)) or [])
    service.sample_asset('admin','asset-source-test',100)
    sql,limit=statements[0]
    assert "'***' AS \"email\"" in sql
    assert limit == 5


def test_scalar_serialization():
    assert postgresql_query.json_value(Decimal('100.1200')) == '100.1200'
    assert postgresql_query.json_value(datetime.date(2026,9,9)) == '2026-09-09'


@pytest.mark.parametrize('exception,code,status', [
    (postgresql_query.psycopg.errors.QueryCanceled('private server detail'),'query_timeout',504),
    (postgresql_query.psycopg.OperationalError('private password detail'),'source_unavailable',503),
    (postgresql_query.psycopg.errors.InsufficientPrivilege('private schema detail'),'permission_denied',403),
])
def test_database_errors_are_classified_without_disclosing_details(monkeypatch,exception,code,status):
    class Connector:
        def _connect(self): raise exception
    monkeypatch.setattr(postgresql_query,'connector_from_row',lambda _: Connector())
    with pytest.raises(HTTPException) as caught:
        postgresql_query.execute_postgres({},'SELECT count(*) FROM ods.orders')
    assert caught.value.code == code and caught.value.status_code == status
    assert 'private' not in caught.value.detail
