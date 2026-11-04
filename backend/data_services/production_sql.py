"""Fixed, bounded transactional materialization for SQLite and PostgreSQL."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal

from backend.core import DEMO_DB
from backend.semantic_metrics.compiler import quote
from backend.tools.errors import ToolRuntimeError


@contextmanager
def warehouse_connection(config, source):
    if source['source_kind'] == 'local-sqlite':
        con = sqlite3.connect(DEMO_DB, timeout=5)
        con.row_factory = sqlite3.Row
        deadline = time.monotonic() + 15
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 5000)
    else:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.conninfo import conninfo_to_dict
        from backend.data_sources.postgresql import parse_endpoint
        dsn = os.environ.get(config['writer_env'], '')
        if not dsn:
            raise ToolRuntimeError('writer_not_configured',
                '缺少 PostgreSQL 任务写入账号配置，请管理员设置服务端环境变量 ' + config['writer_env'] + '。')
        settings = conninfo_to_dict(dsn)
        host, port = parse_endpoint(source['endpoint'])
        if (settings.get('host'), str(settings.get('port', '5432')), settings.get('dbname')) != (
                host, str(port), source['catalog_name']):
            raise ToolRuntimeError('writer_source_mismatch', '任务写入账号必须连接到已登记输入数据源的同一地址和数据库。')
        try:
            con = psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row,
                application_name='metric-pilot-production', options='-c statement_timeout=15000 -c lock_timeout=2000')
        except psycopg.Error as exc:
            raise ToolRuntimeError('writer_unavailable', '无法连接任务写入账号，请检查服务端凭据与数据库权限。') from exc
    try:
        if source['source_kind'] == 'local-sqlite':
            con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def materialize(config, source, start, end, now, run_id, *, target_owned):
    pg = source['source_kind'] == 'postgresql'
    marker = '%s' if pg else '?'
    source_table = '.'.join(quote(x) for x in (config['source_namespace'], config['source_table']) if x)
    target = '.'.join(quote(x) for x in (config['target_namespace'], config['target_table']) if x)
    time_field, amount, status, order = (quote(config[k]) for k in
        ('time_column', 'amount_column', 'status_column', 'order_id_column'))
    predicate = f'{status}={marker} AND {time_field}>={marker} AND {time_field}<{marker}'
    params = (config['paid_value'], start, end)
    group_date = f'CAST({time_field} AS DATE)' if pg else f'DATE({time_field})'
    insert_sql = f'''INSERT INTO {target}(stat_date,paid_amount,order_count,processed_at,production_run_id)
        SELECT {group_date},SUM({amount}),COUNT(*),{marker},{marker}
        FROM {source_table} WHERE {predicate} GROUP BY {group_date}'''
    with warehouse_connection(config, source) as con:
        if pg:
            # A transaction-scoped DB lock also excludes writers on another app host.
            con.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (target,))
            exists = con.execute('SELECT to_regclass(%s) AS name', (target,)).fetchone()['name'] is not None
        else:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (config['target_table'],)).fetchone() is not None
        if exists and not target_owned:
            raise ToolRuntimeError('target_not_owned', '目标表已经存在且没有本平台成功运行记录，已停止写入。')
        stats = dict(con.execute(f'''SELECT COUNT(*) AS n,COUNT(DISTINCT {order}) AS unique_orders,
            SUM(CASE WHEN {amount} IS NULL OR {amount}<0 THEN 1 ELSE 0 END) AS invalid_amounts,
            SUM({amount}) AS amount FROM {source_table} WHERE {predicate}''', params).fetchone())
        if stats['n'] != stats['unique_orders'] or stats['invalid_amounts']:
            raise ToolRuntimeError('quality_failed', '输入质量检查失败：订单主键为空/重复，或已支付金额为空/负值。数据未提交。')
        if not pg:
            invalid_types = con.execute(f'''SELECT COUNT(*) FROM {source_table} WHERE {predicate}
                AND TYPEOF({amount}) NOT IN ('integer','real')''', params).fetchone()[0]
            if invalid_types:
                raise ToolRuntimeError('quality_failed', '输入质量检查失败：金额含非数值，数据未提交。')
        con.execute(f'''CREATE TABLE IF NOT EXISTS {target}(
            stat_date {'DATE' if pg else 'TEXT'} PRIMARY KEY,paid_amount {'NUMERIC' if pg else 'REAL'} NOT NULL,
            order_count INTEGER NOT NULL,processed_at TEXT NOT NULL,production_run_id TEXT NOT NULL)''')
        con.execute(f'DELETE FROM {target} WHERE stat_date>={marker} AND stat_date<{marker}', (start, end))
        con.execute(insert_sql, (now, run_id, *params))
        result = dict(con.execute(f'''SELECT COUNT(*) AS days,COALESCE(SUM(order_count),0) AS orders,
            COALESCE(SUM(paid_amount),0) AS amount,MAX(stat_date) AS latest_date FROM {target}
            WHERE stat_date>={marker} AND stat_date<{marker}''', (start, end)).fetchone())
        if (int(result['orders']) != stats['n'] or abs(Decimal(str(result['amount'])) -
                Decimal(str(stats['amount'] or 0))) > Decimal('0.000001')):
            raise ToolRuntimeError('quality_failed', '汇总对账失败，数据已回滚。')
        if pg and source['username']:
            con.execute(f'GRANT SELECT ON {target} TO {quote(source["username"])}')
        total = dict(con.execute(f'SELECT COUNT(*) AS n,MAX(stat_date) AS latest FROM {target}').fetchone())
    return dict(days=int(result['days']), orders=int(result['orders']), amount=str(result['amount']),
                latest_date=str(result['latest_date']) if result['latest_date'] else None,
                total_days=int(total['n']), latest_asset_date=str(total['latest']) if total['latest'] else None,
                sql_template=insert_sql, quality_status='healthy',
                parameters=dict(paid_value=config['paid_value'],range_start=start,range_end=end,
                                processed_at=now,production_run_id=run_id))
