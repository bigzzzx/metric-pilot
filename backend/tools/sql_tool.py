"""The only read-query tool exposed to the data agent."""
import re
import sqlite3

from fastapi import HTTPException

from backend.core import DEMO_DB, db

ALLOWED_TABLES = {"users", "products", "orders"}


def run_readonly_sql(sql: str) -> list[dict]:
    statement = sql.strip().rstrip(";")
    lowered = statement.lower()
    if not lowered.startswith(("select", "with")) or ";" in statement:
        raise HTTPException(400, "Only one read-only SELECT/WITH statement is allowed")
    if re.search(r"\b(drop|delete|insert|update|alter|attach|pragma|vacuum|create|replace)\b", lowered):
        raise HTTPException(400, "Unsafe SQL was rejected")
    tables = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", lowered))
    if not tables or not tables.issubset(ALLOWED_TABLES):
        raise HTTPException(400, "SQL references a table outside the catalog allowlist")
    connection = db(DEMO_DB)
    try:
        return [dict(row) for row in connection.execute(statement).fetchall()]
    except sqlite3.Error as exc:
        raise HTTPException(400, f"SQL execution failed: {exc}") from exc
    finally:
        connection.close()
