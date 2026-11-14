"""Read-only SQL tool guarded by catalog permissions and SQLite authorizer."""
from __future__ import annotations

import re

from fastapi import HTTPException

from backend.catalog.retrieval import authorized_query_schema
from backend.core import DEMO_DB, db
from backend.data_sources.sqlite_query import execute_sqlite
from backend.data_sources.sql_validation import parse_query, validate_postgres
from backend.data_sources.query_catalog import resolve_target
from backend.data_sources.postgresql_query import execute_postgres

MAX_RESULT_ROWS = 100
LEGACY_ALLOWED_TABLES = {"users", "products", "orders"}


def _validate_shape(sql: str) -> tuple[str, set[str]]:
    statement = sql.strip().rstrip(";")
    lowered = statement.lower()
    if not lowered.startswith(("select", "with")) or ";" in statement:
        raise HTTPException(400, "Only one read-only SELECT/WITH statement is allowed")
    if re.search(r"\b(drop|delete|insert|update|alter|attach|pragma|vacuum|create|replace|load_extension)\b", lowered):
        raise HTTPException(400, "Unsafe SQL was rejected")
    _, physical_tables = parse_query(statement)
    tables = {table.name for table in physical_tables}
    if not tables:
        raise HTTPException(400, "SQL must reference a published catalog table")
    return statement, tables


def run_readonly_sql(sql: str, user_id: str = "", source_id: str | None = None, *,
                     capture_origin=None, connection_factory=None) -> list[dict]:
    source_revision = schema_hash = None
    capture_sql = sql
    pg_source = None
    view_definitions = {}
    if user_id:
        tree, tables = parse_query(sql)
        con = db()
        try:
            con.execute('BEGIN')
            assets = resolve_target(tables, con, user_id, source_id)
            selected = assets[0]
            source_id = selected['source_id']
            source = con.execute('SELECT * FROM data_sources WHERE id=? AND enabled=1', (source_id,)).fetchone()
            if not source:
                raise HTTPException(403, '数据源已停用。')
            if selected['source_kind'] == 'postgresql':
                from backend.lineage.views import query_view_definitions
                view_definitions = query_view_definitions(con,user_id,assets)
                capture_sql = validate_postgres(tree, tables, assets, con)
                pg_source = source
            if capture_origin:
                from backend.lineage.capture import query_schema_hash
                source_revision = source['lineage_source_revision']
                schema_hash = query_schema_hash(con, source_id)
        finally:
            con.close()
    rows = (execute_postgres(pg_source, capture_sql, view_definitions=view_definitions) if view_definitions else
            execute_postgres(pg_source, capture_sql) if pg_source is not None else _run_sqlite(sql, user_id))
    if capture_origin and user_id and source_id:
        from backend.lineage.capture import record_executed_query
        record_executed_query(capture_sql,user_id,source_id,capture_origin,connection_factory=connection_factory or db,
                              source_revision=source_revision,schema_hash=schema_hash)
    return rows


def _run_sqlite(sql: str, user_id: str = "") -> list[dict]:
    """Execute one catalog-authorized SELECT and cap the returned row count.

    The preliminary parser rejects write-shaped SQL. SQLite's authorizer then
    performs the non-bypassable check for every table/column actually read,
    including nested queries, views and SELECT * expansion.
    """
    statement, referenced = _validate_shape(sql)
    allowed = authorized_query_schema(user_id) if user_id else {name: set() for name in LEGACY_ALLOWED_TABLES}
    if not referenced.issubset(allowed):
        raise HTTPException(403, "SQL references a table outside your catalog query permission")
    return execute_sqlite(statement, allowed, user_id, db, DEMO_DB, MAX_RESULT_ROWS)
