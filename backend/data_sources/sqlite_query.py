"""SQLite adapter: engine-level table/column checks and bounded execution."""
import sqlite3
import time
from fastapi import HTTPException
from backend.query_quality.sql_preflight import prepare_readonly
from backend.data_sources.query_catalog import QueryTargetError


def execute_sqlite(statement, allowed, user_id, connection_factory, database_path, limit=100):
    connection = connection_factory(database_path)
    deadline = time.monotonic() + 10
    def authorizer(action, table, column, _database, _trigger):
        if action == sqlite3.SQLITE_READ:
            if table not in allowed or (user_id and column and column not in allowed.get(table, set())):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorizer)
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        prepare_readonly(connection, statement)
        return [dict(row) for row in connection.execute(statement).fetchmany(limit)]
    except sqlite3.DatabaseError as exc:
        detail = str(exc)
        if 'interrupted' in detail.lower():
            raise QueryTargetError('query_timeout', '数据库查询超时，请缩小范围后重试。', 504) from exc
        if 'not authorized' in detail.lower() or 'prohibited' in detail.lower():
            raise HTTPException(403, 'SQL reads a sensitive column or unapproved catalog resource') from exc
        raise HTTPException(400, f'SQL execution failed: {detail}') from exc
    finally:
        connection.set_authorizer(None)
        connection.set_progress_handler(None, 0)
        connection.close()
