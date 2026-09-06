"""SQLite prepare-only validation. Never runs the user's query or reads its rows."""
import sqlite3

from fastapi import HTTPException


class SQLSchemaError(HTTPException):
    code = "sql_schema_invalid"

    def __init__(self):
        # Do not disclose whether an inaccessible field exists or list alternatives.
        super().__init__(400, "查询所需字段不存在、存在歧义或与数据目录不一致。请确认字段及时间口径；当前无法按该条件查询，不会自动改用其他日期。")


def prepare_readonly(connection: sqlite3.Connection, statement: str) -> None:
    """Must run with the same catalog authorizer as the subsequent execution."""
    try:
        cursor = connection.execute("EXPLAIN " + statement)
        cursor.close()
    except sqlite3.DatabaseError as exc:
        message = str(exc).lower()
        if any(text in message for text in ("no such column", "no such table", "ambiguous column", "has no column", "no column named")):
            raise SQLSchemaError() from exc
        raise
