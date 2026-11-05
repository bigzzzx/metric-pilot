"""Catalog identity and schema versions; never bind by field name alone."""
from backend.catalog.service import DISCOVER, VIEW_METADATA, can_access


class BindingError(ValueError):
    def __init__(self,code,message):
        super().__init__(message)
        self.code = code


def assets_for(con,user_id,source_id=None,asset_ids=None,*,_system=False):
    result = {}
    for row in con.execute('''SELECT a.*,s.kind AS source_kind,s.enabled AS source_enabled,
        s.catalog_name FROM catalog_assets a JOIN data_sources s ON s.id=a.source_id
        WHERE a.lifecycle_status='active' ORDER BY a.id'''):
        if source_id and row['source_id'] != source_id:
            continue
        if asset_ids and row['id'] not in asset_ids:
            continue
        if not _system and not all(can_access(user_id,action,asset_id=row['id'],connection=con) for action in (DISCOVER,VIEW_METADATA)):
            continue
        asset = dict(row)
        asset['columns'] = {c['physical_name']:dict(c) for c in con.execute(
            'SELECT * FROM catalog_columns WHERE asset_id=? ORDER BY ordinal',(row['id'],))}
        result[row['id']] = asset
    return result


def snapshot(asset):
    return dict(source_id=asset['source_id'],namespace=asset['namespace'],table=asset['physical_name'],
        domain_id=asset['domain_id'],catalog_version=asset['catalog_version'],enabled=bool(asset['source_enabled']),
        publish_status=asset['publish_status'],columns={k:dict(type=c['data_type'],sensitivity=c['sensitivity'])
            for k,c in asset['columns'].items()})


def resolve_table(table,assets):
    matches = []
    for asset in assets.values():
        quoted = bool(table.this and table.this.args.get('quoted'))
        local = asset.get('source_kind')=='local-sqlite'
        same_name = asset['physical_name'].lower()==table.name.lower() if local else asset['physical_name']==(table.name if quoted else table.name.lower())
        if not same_name:
            continue
        if table.db:
            ns_quoted = bool(table.args.get('db') and table.args['db'].args.get('quoted'))
            namespace = table.db if ns_quoted else table.db.lower()
            if local:
                if namespace.lower()!=(asset['namespace'] or 'main').lower():continue
            elif namespace!=asset['namespace']:continue
        if table.catalog and table.catalog not in {asset['catalog_name'],asset['source_id']}:
            continue
        matches.append(asset)
    if len(matches) != 1:
        raise BindingError('table_ambiguous' if matches else 'table_unresolved',
            '表未唯一绑定到有权限的真实资产：'+table.sql())
    return matches[0]


def candidate_assets(candidate):
    ids = {candidate['from_asset_id'],candidate['to_asset_id']}
    ids.update(candidate.get('context',{}).get('source_asset_ids',[]))
    if candidate.get('context',{}).get('target_asset_id'):
        ids.add(candidate['context']['target_asset_id'])
    for ref in candidate.get('context',{}).get('all_references',[]):
        ids.add(ref['asset_id'])
    return ids


def validate_endpoints(candidate,assets,*,published=False):
    for aid in candidate_assets(candidate):
        asset = assets.get(aid)
        if not asset or not asset['source_enabled']:
            raise BindingError('asset_unavailable','资产不存在、数据源停用或没有元数据权限。')
        if published and asset['publish_status'] != 'published':
            raise BindingError('asset_unpublished','所有相关资产都需要先审核发布。')
    refs = list(candidate.get('context',{}).get('all_references',[]))
    for prefix in ('from','to'):
        if column := candidate.get(prefix+'_column'):
            refs.append({'asset_id':candidate[prefix+'_asset_id'],'column':column})
    for ref in refs:
        column = assets[ref['asset_id']]['columns'].get(ref['column'])
        if not column:
            raise BindingError('column_unresolved','证据引用的字段不在当前真实 Schema 中。')
        if published and column['sensitivity'] in {'sensitive','restricted'}:
            raise BindingError('column_restricted','证据涉及受限字段，不能发布到通用目录/RAG。')
    if candidate['kind'] in {'join','column_value'} and (not candidate.get('from_column') or not candidate.get('to_column')):
        raise BindingError('column_evidence_missing','关联或字段值来源必须绑定实际字段。')
    return {aid:snapshot(assets[aid]) for aid in sorted(candidate_assets(candidate))}
