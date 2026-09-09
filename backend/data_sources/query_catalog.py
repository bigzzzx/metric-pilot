"""Source-aware, permission-filtered query targets; never exposes credentials."""
from fastapi import HTTPException
from backend.catalog.service import QUERY_DATA, can_access


class QueryTargetError(HTTPException):
    def __init__(self, code, message, status=400):
        self.code = code
        super().__init__(status, message)


def query_assets(con, user_id, source_id=None):
    rows = con.execute("""SELECT a.*,s.kind AS source_kind,s.name AS source_name FROM catalog_assets a
        JOIN data_sources s ON s.id=a.source_id WHERE a.publish_status='published' AND s.enabled=1
        AND s.kind IN ('local-sqlite','postgresql')""").fetchall()
    return [dict(a) for a in rows if (not source_id or a['source_id'] == source_id)
            and can_access(user_id, QUERY_DATA, asset_id=a['id'], connection=con)]


def resolve_target(tables, con, user_id, source_id=None):
    assets = query_assets(con, user_id, source_id)
    matches = []
    for table in tables:
        if table.catalog:
            raise QueryTargetError('cross_source_query', '暂不支持跨数据库或跨数据源查询，请选择同一数据源内的表。')
        candidates = [a for a in assets if a['physical_name'] == table.name and
                      (not table.db or a['namespace'] == table.db or (a['source_kind'] == 'local-sqlite' and table.db == 'main'))]
        if not candidates:
            raise QueryTargetError('permission_denied', '查询表未发布、数据源已停用或缺少 query_data 权限。', 403)
        if len(candidates) != 1:
            raise QueryTargetError('source_ambiguous', '存在同名表，请明确数据源和 Schema 后再查询。')
        matches.append(candidates[0])
    if not matches:
        raise QueryTargetError('sql_error', '查询必须引用已发布且有权限的数据表。')
    if len({a['source_id'] for a in matches}) != 1:
        raise QueryTargetError('cross_source_query', '暂不支持跨数据源 Join，请选择同一数据源内的表。')
    return matches
