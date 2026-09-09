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


def run_readonly_sql(sql: str, user_id: str = "", source_id: str | None = None) -> list[dict]:
    if user_id:
        tree, tables = parse_query(sql)
        con = db()
        try:
            assets = resolve_target(tables, con, user_id, source_id)
            selected = assets[0]
            if selected['source_kind'] == 'postgresql':
                statement = validate_postgres(tree, tables, assets, con)
                source = con.execute('SELECT * FROM data_sources WHERE id=? AND enabled=1', (selected['source_id'],)).fetchone()
                return execute_postgres(source, statement)
        finally:
            con.close()
    return _run_sqlite(sql, user_id)


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
