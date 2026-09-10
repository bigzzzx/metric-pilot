"""Parse one SELECT, resolve physical scopes and enforce PostgreSQL column ACLs."""
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from backend.data_sources.query_catalog import QueryTargetError
from backend.query_quality.sql_preflight import SQLSchemaError

SAFE_FUNCTIONS = set('COUNT SUM AVG MIN MAX COALESCE NULLIF ABS ROUND CEIL CEILING FLOOR LOWER UPPER LENGTH CHAR_LENGTH CONCAT CONCAT_WS SUBSTRING SUBSTR TRIM LTRIM RTRIM REPLACE LEFT RIGHT DATE_TRUNC TIMESTAMP_TRUNC DATE_PART EXTRACT TO_CHAR TO_DATE TO_TIMESTAMP CURRENT_DATE CURRENT_TIMESTAMP NOW CAST TRY_CAST CASE IF ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE NTH_VALUE NTILE PERCENT_RANK CUME_DIST STDDEV STDDEV_POP STDDEV_SAMP VARIANCE VAR_POP VAR_SAMP GREATEST LEAST POWER POW SQRT MOD FILTER'.split())


def parse_query(sql):
    try:
        expressions = sqlglot.parse(sql, read='postgres')
        if len(expressions) != 1 or not isinstance(expressions[0], exp.Query):
            raise ValueError('not a single query')
        tree = normalize_identifiers(expressions[0], dialect='postgres')
        forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Into, exp.Lock)
        if any(isinstance(node, forbidden) for node in tree.walk()):
            raise ValueError('mutating statement')
        scopes = list(traverse_scope(tree))
        tables = [source for scope in scopes for _, source in scope.sources.items() if isinstance(source, exp.Table)]
        if any(not isinstance(t.this, exp.Identifier) for t in tables):
            raise ValueError('table function')
        return tree, tables
    except (ValueError, sqlglot.errors.SqlglotError) as exc:
        raise QueryTargetError('sql_error', '仅支持一条只读 SELECT/WITH 查询，不支持写入、锁定或表函数。') from exc


def validate_postgres(tree, tables, assets, con):
    schema, hidden = {}, {}
    for table, asset in zip(tables, assets):
        namespace = asset['namespace'] or 'public'
        table.set('db', exp.to_identifier(namespace, quoted=True))
        columns = con.execute('SELECT physical_name,sensitivity FROM catalog_columns WHERE asset_id=?', (asset['id'],)).fetchall()
        schema.setdefault(namespace, {})[asset['physical_name']] = {c['physical_name']: 'UNKNOWN' for c in columns}
        hidden[(namespace, asset['physical_name'])] = {c['physical_name'] for c in columns if c['sensitivity'] in {'sensitive','restricted'}}
    for node in tree.walk():
        if isinstance(node, exp.Dot):
            raise QueryTargetError('permission_denied', '不允许调用自定义函数或读取复合字段。', 403)
        # SQLGlot models fixed binary operators (including AND, OR and
        # arithmetic) as Func subclasses. They are grammar nodes rather than
        # callable database functions, so only apply the function allow-list
        # to actual calls and function-like expressions.
        if isinstance(node, exp.Func) and not isinstance(node, exp.Binary):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if name not in SAFE_FUNCTIONS | {'EXISTS'}:
                raise QueryTargetError('permission_denied', '查询包含未批准的函数。', 403)
        if isinstance(node, exp.DataType) and node.this.value not in {'TEXT','VARCHAR','CHAR','INT','BIGINT','SMALLINT','FLOAT','DOUBLE','DECIMAL','BOOLEAN','DATE','TIMESTAMP','TIMESTAMPTZ','INTERVAL'}:
            raise QueryTargetError('permission_denied', '查询包含未批准的数据类型转换。', 403)
    try:
        tree = qualify(tree, dialect='postgres', schema=schema, infer_schema=False, validate_qualify_columns=True)
        if any(column.is_star for column in tree.find_all(exp.Column)):
            raise QueryTargetError('permission_denied', '不允许将整行记录传入聚合函数，请明确字段。', 403)
        for scope in traverse_scope(tree):
            for column in scope.columns:
                source = scope.sources.get(column.table)
                # Correlated columns may belong to an outer scope.
                outer = scope.parent
                while source is None and outer is not None:
                    source = outer.sources.get(column.table)
                    outer = outer.parent
                if isinstance(source, exp.Table) and (column.name in hidden.get((source.db, source.name), set()) or (column.is_star and hidden.get((source.db, source.name)))):
                    raise QueryTargetError('permission_denied', 'SQL 读取了敏感字段，缺少该字段的查询权限。', 403)
        return tree.sql(dialect='postgres')
    except sqlglot.errors.SqlglotError as exc:
        raise SQLSchemaError() from exc
