"""Current authorized published facts, RAG projection and bounded impact traversal."""
import json
from functools import lru_cache
from fastapi import HTTPException
from backend.core import db
from backend.lineage.catalog import assets_for, candidate_assets
from backend.lineage.service import candidate_view, current_candidate, _decorate


@lru_cache(maxsize=1)
def demo_relation_ids():
    from backend.core import ROOT
    return {item['id'] for item in json.loads((ROOT/'metadata'/'relations.json').read_text(encoding='utf-8'))['relations']}


def legacy_relation_provenance(row):
    return 'demo_seed' if row['id'] in demo_relation_ids() else 'database_or_legacy_declaration'


def published_candidates(con,user_id=None):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='lineage_candidates'").fetchone():
        return [],{}
    assets = assets_for(con,user_id,_system=user_id is None)
    result = []
    for row in con.execute("SELECT * FROM lineage_candidates WHERE status='published' ORDER BY id LIMIT 3000"):
        item = candidate_view(row)
        if item['technical_status']!='type_mismatch' and item['observation']!='run_failed' and current_candidate(con,item,assets,published=True):
            result.append(item)
    return result,assets


def rag_documents(con):
    edges,assets = published_candidates(con)
    docs = []
    for edge in edges:
        left,right = assets[edge['from_asset_id']],assets[edge['to_asset_id']]
        source = con.execute('SELECT source_kind,source_version,title FROM lineage_sources WHERE id=?',(edge['source_ref'],)).fetchone()
        review = edge['review']
        text = (f"{'表关联' if edge['kind']=='join' else '数据血缘'}：{left['namespace']}.{left['physical_name']}.{edge['from_column']} → "
            f"{right['namespace']}.{right['physical_name']}.{edge['to_column']}。类型：{edge['kind']}。表达式：{edge['expression'][:2000]}{'...（完整表达式见证据）' if len(edge['expression'])>2000 else ''}。"
            f"证据类型：{source['source_kind']}；观察：{edge['observation']}（设计定义不代表实际执行，外部上报不代表平台观察）。"
            f"人工业务审核：已批准并发布。基数：{review.get('cardinality','unknown')}；建议连接：{review.get('recommended_join','unspecified')}。"
            f"证据 ID：{edge['source_ref']}；来源版本：{source['source_version']}。")
        # Raw document paragraphs and arbitrary quotes are intentionally NOT model context.
        docs.append(dict(id='catalog:evidence:'+edge['id'],kind='relation' if edge['kind']=='join' else 'field_lineage',
            title=f"已审核{edge['kind']}：{left['physical_name']} → {right['physical_name']}",content=text,
            keywords=[left['physical_name'],right['physical_name'],edge['from_column'],edge['to_column'],'血缘','关联','join'],
            asset_ids=sorted(candidate_assets(edge)),domain_id=left['domain_id'],source_id=left['source_id'] if left['source_id']==right['source_id'] else None,
            source_kind=left['source_kind'],namespace=left['namespace'],physical_name=left['physical_name'],
            lineage_candidate_id=edge['id'],lineage_version=edge['version']))
    return docs


def document_visible(con,document,user_id):
    row = con.execute('SELECT * FROM lineage_candidates WHERE id=?',(document.get('lineage_candidate_id'),)).fetchone()
    if not row: return False
    item = candidate_view(row)
    return item['status']=='published' and item['version']==document.get('lineage_version') and current_candidate(con,item,assets_for(con,user_id),published=True)


def relationships(con,user_id,asset_id):
    edges,assets = published_candidates(con,user_id)
    def brief(aid): return {k:assets[aid][k] for k in ('id','physical_name','namespace','display_name','source_id','publish_status')}
    return [dict(id=e['id'],from_asset=brief(e['from_asset_id']),to_asset=brief(e['to_asset_id']),
        from_column=e['from_column'],to_column=e['to_column'],recommended_join=e['review'].get('recommended_join','unspecified'),
        cardinality=e['review'].get('cardinality','unknown'),description=e['expression'],visible=True,
        provenance=e['observation'],source_ref=e['source_ref']) for e in edges if e['kind']=='join' and asset_id in candidate_assets(e)]


def lineage(user_id,asset_id,column='',direction='upstream',hops=2,include_influences=False,*,connection_factory=None):
    con = (connection_factory or db)()
    try:
        edges,assets = published_candidates(con,user_id)
        asset = assets.get(asset_id)
        if not asset or not asset['source_enabled'] or asset['publish_status']!='published':
            raise HTTPException(404,'资产不存在、未发布或缺少 discover/view_metadata 权限。')
        if column and (column not in asset['columns'] or asset['columns'][column]['sensitivity'] in {'sensitive','restricted'}):
            raise HTTPException(404,'字段不存在或不可查看。')
        if direction not in {'upstream','downstream'}: raise HTTPException(422,'direction 必须是 upstream/downstream。')
        allowed = {'column_value','column_influence'} if column and include_influences else {'column_value'} if column else {'table_transform'}
        frontier,seen,found = {(asset_id,column)},{(asset_id,column)},{}
        for _ in range(max(1,min(3,hops))):
            nxt = set()
            for edge in edges:
                if edge['kind'] not in allowed: continue
                src = (edge['from_asset_id'],edge['from_column'] if column else '')
                dst = (edge['to_asset_id'],edge['to_column'] if column else '')
                anchor,other = (dst,src) if direction=='upstream' else (src,dst)
                if anchor not in frontier: continue
                found[edge['id']] = _decorate(con,edge,assets)
                nxt.add(other)
            frontier = nxt-seen; seen.update(nxt)
            if not frontier: break
        return dict(asset_id=asset_id,column=column,direction=direction,hops=max(1,min(3,hops)),
            nodes=[dict(asset_id=aid,column=col,table=assets[aid]['physical_name'],namespace=assets[aid]['namespace'],source_id=assets[aid]['source_id']) for aid,col in sorted(seen)],
            edges=list(found.values()),scope='current_authorized_published',
            limitation='只展示已审核发布证据；设计/外部上报与实际成功运行分别标识。没有边不代表不存在依赖。')
    finally: con.close()
