"""Bounded PostgreSQL query execution and sanitized operational failures."""
import datetime
from decimal import Decimal
from uuid import UUID
import psycopg
from backend.data_sources.base import ConnectorUnavailable
from backend.data_sources.service import connector_from_row
from backend.data_sources.query_catalog import QueryTargetError
from backend.query_quality.sql_preflight import SQLSchemaError


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)  # Preserve monetary precision across JSON/model boundaries.
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def execute_postgres(source, statement, limit=100):
    try:
        connector = connector_from_row(source)
        with connector._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
                cursor.execute('SET LOCAL search_path = pg_catalog')
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute("SET LOCAL lock_timeout = '2s'")
                cursor.execute('EXPLAIN ' + statement)
                bounded_limit = max(1, min(int(limit), 100))
                # The SQL was AST-validated; the only appended value is a bounded integer.
                # No DBAPI placeholders, so LIKE patterns and modulo retain literal '%'.
                cursor.execute('SELECT * FROM (' + statement + f') AS data_agent_result LIMIT {bounded_limit}')
                return [{key: json_value(value) for key, value in row.items()} for row in cursor.fetchmany(bounded_limit)]
    except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as exc:
        raise QueryTargetError('query_timeout', '数据库查询超时，请缩小时间范围或简化查询后重试。', 504) from exc
    except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable, psycopg.errors.AmbiguousColumn) as exc:
        raise SQLSchemaError() from exc
    except psycopg.errors.InsufficientPrivilege as exc:
        raise QueryTargetError('permission_denied', '数据源只读账号缺少所请求表或字段的 SELECT 权限。', 403) from exc
    except (ConnectorUnavailable, psycopg.OperationalError) as exc:
        raise QueryTargetError('source_unavailable', '无法连接 PostgreSQL 数据源，请检查服务、地址及 SSH 隧道。', 503) from exc
    except psycopg.Error as exc:
        raise QueryTargetError('sql_error', 'PostgreSQL 查询执行失败，请检查 SQL 与字段类型。') from exc
