"""Read-only bounded technical checks; never approve business meaning."""
import json
from fastapi import HTTPException
from backend.core import db
from backend.catalog.service import QUERY_DATA, can_access
from backend.lineage.catalog import assets_for, candidate_assets, validate_endpoints, BindingError
from backend.lineage.service import _get_for_admin, audit
from backend.lineage.storage import canonical
from backend.semantic_metrics.compiler import quote
from backend.tools.sql_tool import run_readonly_sql


def family(kind):
    value = kind.lower()
    if any(x in value for x in ('int','numeric','decimal','float','real','double')): return 'number'
    if any(x in value for x in ('date','time')): return 'time'
    if 'bool' in value: return 'boolean'
    if any(x in value for x in ('text','char','string','uuid')): return 'text'
    return 'unknown'


def validate_candidate(identifier,expected_version,user_id,*,profile=False,connection_factory=None):
    factory = connection_factory or db
    con = factory()
    try:
        item = _get_for_admin(con,identifier,user_id)
        if item['version'] != expected_version:
            raise HTTPException(409,'候选版本已变化。')
        if item['status']=='published':
            raise HTTPException(409,'已发布候选请先撤回，重新验证后重新审核。')
        assets = assets_for(con,user_id)
        try:
            pins = validate_endpoints(item,assets)
        except BindingError as exc:
            raise HTTPException(422,dict(code=exc.code,message=str(exc))) from exc
        checks = {'schema':'passed','business_semantics':'requires_human_review'}
        if item['kind']=='join':
            left = assets[item['from_asset_id']]
            right = assets[item['to_asset_id']]
            lt = family(left['columns'][item['from_column']]['data_type'])
            rt = family(right['columns'][item['to_column']]['data_type'])
            checks['type_compatibility'] = 'compatible' if lt==rt and lt!='unknown' else 'unknown' if 'unknown' in {lt,rt} else 'mismatch'
            if profile:
                if not all(can_access(user_id,QUERY_DATA,asset_id=aid,connection=con) and assets[aid]['publish_status']=='published'
                           for aid in candidate_assets(item)):
                    raise HTTPException(403,'只读技术采样需要所有相关已发布资产的 query_data 权限。')
                checks['profile'] = _profile_join(item,left,right,user_id)
    finally:
        con.close()
    con = factory()
    try:
        con.execute('BEGIN IMMEDIATE')
        current = _get_for_admin(con,identifier,user_id)
        if current['version'] != expected_version:
            raise HTTPException(409,'采样期间候选已变化，请刷新后重试。')
        status = 'type_mismatch' if checks.get('type_compatibility')=='mismatch' else 'schema_validated'
        con.execute('''UPDATE lineage_candidates SET schema_json=?,checks_json=?,technical_status=?,status='draft',
            review_json='{}',version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
            (canonical(pins),canonical(checks),status,identifier))
        current['version'] += 1
        audit(con,current,user_id,'validated',checks)
        con.commit()
        return dict(id=identifier,version=current['version'],status='draft',technical_status=status,checks=checks)
    except Exception:
        con.rollback(); raise
    finally:
        con.close()


def _profile_join(item,left,right,user_id):
    if left['source_id'] != right['source_id']:
        return dict(status='unsupported',reason='cross_source_sampling_not_supported',sample_limit=1000)
    try:
        def table(asset): return '.'.join(quote(x) for x in (asset['namespace'],asset['physical_name']) if x)
        lc,rc = quote(item['from_column']),quote(item['to_column'])
        sql = f'''WITH lsample AS (SELECT {lc} AS k FROM {table(left)} LIMIT 1000),
            rsample AS (SELECT {rc} AS k FROM {table(right)} LIMIT 1000)
            SELECT (SELECT COUNT(CASE WHEN k IS NULL THEN 1 ELSE 1 END) FROM lsample) AS left_rows,
              (SELECT COUNT(k) FROM lsample) AS left_nonnull,
              (SELECT COUNT(DISTINCT k) FROM lsample) AS left_distinct,
              (SELECT COUNT(CASE WHEN k IS NULL THEN 1 ELSE 1 END) FROM rsample) AS right_rows,
              (SELECT COUNT(k) FROM rsample) AS right_nonnull,
              (SELECT COUNT(DISTINCT k) FROM rsample) AS right_distinct,
              (SELECT COUNT(DISTINCT l.k) FROM lsample l JOIN rsample r ON l.k=r.k) AS matched_keys'''
        rows = run_readonly_sql(sql,user_id,left['source_id'])
        values = rows[0]
        return dict(status='sampled',sample_limit=1000,predicate_scope='single_column_pair_only',statistics=values,
            distinct_key_coverage=(float(values['matched_keys'])/float(values['left_distinct'])) if values['left_distinct'] else None,
            limitation='最多各 1000 行；样本唯一/覆盖率不证明全量基数或业务关系，不能自动批准。')
    except Exception:
        return dict(status='unavailable',sample_limit=1000,reason='read_only_sample_failed_or_unsupported')
