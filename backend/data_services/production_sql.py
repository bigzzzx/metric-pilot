"""Fixed, bounded transactional materialization for SQLite and PostgreSQL."""
from __future__ import annotations

import os
import json
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


def target_name(config):
    return '.'.join(quote(x) for x in (config['target_namespace'], config['target_table']) if x)


@contextmanager
def locked_warehouse(config, source):
    """Execution and recovery use the SAME transaction barrier before platform CAS."""
    with warehouse_connection(config, source) as con:
        if source['source_kind'] == 'postgresql':
            con.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (target_name(config),))
        # SQLite's BEGIN IMMEDIATE is the cross-process warehouse barrier.
        yield con


def _receipt_name(config):
    return '.'.join(quote(x) for x in (config['target_namespace'], config['target_table'] + '_receipts') if x)


RECEIPT_COLUMNS = {'run_id', 'snapshot_hash', 'arguments_hash', 'execution_token', 'result_json', 'committed_at'}


def _fault_hook(stage, run_id):
    """No-op seam used only by isolated process termination tests."""


def _receipt_exists(con, config, pg):
    if pg:
        return con.execute('SELECT to_regclass(%s) AS name', (_receipt_name(config),)).fetchone()['name'] is not None
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (config['target_table'] + '_receipts',)).fetchone() is not None


def _check_receipt_schema(con, config, pg):
    if pg:
        # pg_attribute also handles search_path resolution consistently with to_regclass.
        rows = con.execute('''SELECT a.attname AS name,EXISTS(SELECT 1 FROM pg_index i
            WHERE i.indrelid=a.attrelid AND i.indisprimary AND a.attnum=ANY(i.indkey)) AS pk
            FROM pg_attribute a WHERE a.attrelid=to_regclass(%s) AND a.attnum>0 AND NOT a.attisdropped''',
            (_receipt_name(config),)).fetchall()
    else:
        rows = con.execute('PRAGMA table_info(' + quote(config['target_table'] + '_receipts') + ')').fetchall()
    if {r['name'] for r in rows} != RECEIPT_COLUMNS or {r['name'] for r in rows if r['pk']} != {'run_id'}:
        raise ToolRuntimeError('receipt_schema_conflict', '生产提交凭证表结构冲突，已停止执行，请管理员核对。')


def read_receipt(con, config, source, run_id):
    """Read while holding locked_warehouse. Never create a receipt during recovery."""
    pg = source['source_kind'] == 'postgresql'
    if not _receipt_exists(con, config, pg):
        return None
    _check_receipt_schema(con, config, pg)
    row = con.execute('SELECT * FROM ' + _receipt_name(config) + ' WHERE run_id=' + ('%s' if pg else '?'),
                      (run_id,)).fetchone()
    return dict(row) if row else None


def materialize(config, source, start, end, now, run_id, *, target_owned, execution_proof=None, execution_guard=None):
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
    with locked_warehouse(config, source) as con:
        if not execution_proof or execution_guard is None:
            raise ToolRuntimeError('execution_proof_required', '缺少已审批运行的执行凭证，已停止写入。')
        execution_guard()  # AFTER acquiring the warehouse barrier, never before it alone.
        if pg:
            exists = con.execute('SELECT to_regclass(%s) AS name', (target,)).fetchone()['name'] is not None
        else:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (config['target_table'],)).fetchone() is not None
        if exists and not target_owned:
            raise ToolRuntimeError('target_not_owned', '目标表已经存在且没有本平台成功运行记录，已停止写入。')
        receipt_table = _receipt_name(config)
        con.execute(f'''CREATE TABLE IF NOT EXISTS {receipt_table}(
            run_id TEXT PRIMARY KEY,snapshot_hash TEXT NOT NULL,arguments_hash TEXT NOT NULL,
            execution_token TEXT NOT NULL,result_json TEXT NOT NULL,committed_at TEXT NOT NULL)''')
        _check_receipt_schema(con, config, pg)
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
        outcome = dict(days=int(result['days']), orders=int(result['orders']), amount=str(result['amount']),
                latest_date=str(result['latest_date']) if result['latest_date'] else None,
                total_days=int(total['n']), latest_asset_date=str(total['latest']) if total['latest'] else None,
                sql_template=insert_sql, quality_status='healthy',
                parameters=dict(paid_value=config['paid_value'],range_start=start,range_end=end,
                                processed_at=now,production_run_id=run_id))
        # Receipt covers EMPTY partitions as well as rows; it commits or rolls back with data.
        con.execute(f'''INSERT INTO {receipt_table}
            (run_id,snapshot_hash,arguments_hash,execution_token,result_json,committed_at)
            VALUES ({','.join([marker]*6)})''', (run_id,execution_proof['snapshot_hash'],
            execution_proof['arguments_hash'],execution_proof['execution_token'],
            json.dumps(outcome,ensure_ascii=False),now))
        execution_guard()  # A lost platform lease must abort before warehouse commit.
        _fault_hook('before_warehouse_commit',run_id)
    return outcome
