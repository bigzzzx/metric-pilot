"""Read-only SQL tool guarded by catalog permissions and SQLite authorizer."""
from __future__ import annotations

import re
import sqlite3

from fastapi import HTTPException

from backend.catalog.retrieval import authorized_query_schema
from backend.core import DEMO_DB, db
from backend.query_quality.sql_preflight import prepare_readonly

MAX_RESULT_ROWS = 100
LEGACY_ALLOWED_TABLES = {"users", "products", "orders"}


def _validate_shape(sql: str) -> tuple[str, set[str]]:
    statement = sql.strip().rstrip(";")
    lowered = statement.lower()
    if not lowered.startswith(("select", "with")) or ";" in statement:
        raise HTTPException(400, "Only one read-only SELECT/WITH statement is allowed")
    if re.search(r"\b(drop|delete|insert|update|alter|attach|pragma|vacuum|create|replace|load_extension)\b", lowered):
        raise HTTPException(400, "Unsafe SQL was rejected")
    tables = set(re.findall(r"\b(?:from|join)\s+[\"`]?([a-zA-Z_][\w]*)", lowered))
    if not tables:
        raise HTTPException(400, "SQL must reference a published catalog table")
    return statement, tables


def run_readonly_sql(sql: str, user_id: str = "") -> list[dict]:
    """Execute one catalog-authorized SELECT and cap the returned row count.

    The preliminary parser rejects write-shaped SQL. SQLite's authorizer then
    performs the non-bypassable check for every table/column actually read,
    including nested queries, views and SELECT * expansion.
    """
    statement, referenced = _validate_shape(sql)
    allowed = authorized_query_schema(user_id) if user_id else {name: set() for name in LEGACY_ALLOWED_TABLES}
    if not referenced.issubset(allowed):
        raise HTTPException(403, "SQL references a table outside your catalog query permission")
    connection = db(DEMO_DB)

    def authorizer(action: int, table: str | None, column: str | None, _database: str | None, _trigger: str | None) -> int:
        if action == sqlite3.SQLITE_READ:
            if table not in allowed:
                return sqlite3.SQLITE_DENY
            # Legacy non-agent/test calls retain the old public demo behavior.
            if user_id and column and column not in allowed.get(table, set()):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    try:
        prepare_readonly(connection, statement)
        cursor = connection.execute(statement)
        rows = [dict(row) for row in cursor.fetchmany(MAX_RESULT_ROWS + 1)]
        if len(rows) > MAX_RESULT_ROWS:
            rows = rows[:MAX_RESULT_ROWS]
        return rows
    except sqlite3.DatabaseError as exc:
        detail = str(exc)
        if "not authorized" in detail.lower() or "prohibited" in detail.lower():
            raise HTTPException(403, "SQL reads a sensitive column or unapproved catalog resource") from exc
        raise HTTPException(400, f"SQL execution failed: {detail}") from exc
    finally:
        connection.set_authorizer(None)
        connection.close()
