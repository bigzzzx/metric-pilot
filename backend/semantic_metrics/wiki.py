"""LLM-authored drafts require a version-checked catalog publication."""
import json
from fastapi import HTTPException
from pydantic import Field
from backend.core import db
from backend.catalog.service import can_access, DISCOVER, MANAGE
from backend.semantic_metrics.models import ContractModel, MetricContract
from backend.semantic_metrics.compiler import validate_expression
from backend.model_client import complete_structured


class WikiDraft(ContractModel):
    summary: str = Field(max_length=1000)
    content: str = Field(min_length=1,max_length=6000)
    related_metric_ids: list[str] = Field(default_factory=list,max_length=10)


class MetricReview(ContractModel):
    expected_version: int = Field(ge=1)
    aliases: list[str] = Field(default_factory=list,max_length=30)
    contract: MetricContract
    wiki: WikiDraft


def require_admin(con,user_id):
    if not can_access(user_id,MANAGE,connection=con):
        raise HTTPException(403,'缺少 manage 目录管理权限。')


def visible_wiki(con,row,user_id):
    wiki=json.loads(dict(row).get('wiki_json') or '{}')
    if wiki.get('status')!='approved': return {}
    for identifier in wiki.get('related_metric_ids',[]):
        other=con.execute("SELECT * FROM catalog_metrics WHERE id=? AND publish_status='published'",(identifier,)).fetchone()
        if not other or not can_access(user_id,DISCOVER,domain_id=other['domain_id'],connection=con): return {}
        contract=json.loads(other['contract_json'] or '{}')
        if contract.get('asset_id') and not can_access(user_id,DISCOVER,asset_id=contract['asset_id'],connection=con): return {}
    return wiki


async def generate_draft(metric_id,user_id):
    con=db()
    try:
        require_admin(con,user_id)
        row=con.execute('SELECT * FROM catalog_metrics WHERE id=?',(metric_id,)).fetchone()
        if not row: raise HTTPException(404,'Metric not found')
        source={key:row[key] for key in ('id','display_name','description','formula','definition_sql','grain','catalog_version','contract_json')}
        related=[dict(r) for r in con.execute('SELECT id,display_name,description FROM catalog_metrics WHERE domain_id=? AND id!=? LIMIT 10',(row['domain_id'],metric_id))]
    finally: con.close()
    prompt=('Write a Chinese metric Wiki draft grounded ONLY in the supplied definitions. Explain filters, time field, '
            'grain, applicable scenarios, and differences from related metrics. Identify missing/conflicting information explicitly. '
            'Do not invent facts, executable contracts or approval. Return summary, content, related_metric_ids (only supplied IDs).\n'
            +json.dumps({'source':source,'related':related},ensure_ascii=False))
    draft=await complete_structured('data-query-agent',[{'role':'system','content':prompt},{'role':'user','content':'生成待审核口径 Wiki'}],WikiDraft,None)
    if set(draft.related_metric_ids)-{r['id'] for r in related}: raise HTTPException(422,'Wiki 引用了未提供的指标。')
    saved={**draft.model_dump(),'source_refs':['catalog:metric:'+metric_id],'source_snapshot':source,'status':'draft'}
    con=db()
    try:
        require_admin(con,user_id)
        changed=con.execute('UPDATE catalog_metrics SET wiki_draft_json=? WHERE id=? AND catalog_version=?',
                            (json.dumps(saved,ensure_ascii=False),metric_id,source['catalog_version'])).rowcount
        if changed!=1: raise HTTPException(409,'指标已变化，请重新生成 Wiki。')
        con.commit()
    finally: con.close()
    return saved


def publish(metric_id,user_id,payload):
    con=db()
    try:
        con.execute('BEGIN IMMEDIATE')
        require_admin(con,user_id)
        row=con.execute('SELECT * FROM catalog_metrics WHERE id=?',(metric_id,)).fetchone()
        if not row: raise HTTPException(404,'Metric not found')
        if row['catalog_version']!=payload.expected_version: raise HTTPException(409,'指标已变化，请刷新后重新审核。')
        c=payload.contract
        asset=con.execute('''SELECT a.*,s.kind source_kind FROM catalog_assets a JOIN data_sources s ON s.id=a.source_id
                          WHERE a.id=? AND a.publish_status='published' AND s.enabled=1''',(c.asset_id,)).fetchone()
        if not asset or (asset['source_id'],asset['namespace'],asset['physical_name'],asset['domain_id']) != (
                c.source_id,c.namespace,c.table,row['domain_id']): raise HTTPException(422,'契约必须绑定同业务域的已发布资产。')
        if c.dialect!=('postgresql' if asset['source_kind']=='postgresql' else 'sqlite'): raise HTTPException(422,'SQL 方言不匹配。')
        fields={r['physical_name'] for r in con.execute("SELECT physical_name FROM catalog_columns WHERE asset_id=? AND sensitivity NOT IN ('sensitive','restricted')",(c.asset_id,))}
        needed={f.column for f in c.required_filters}|{d.column for d in c.dimensions.values()}|({c.time_column} if c.time_column else set())
        if not needed<=fields or (c.requires_time_range and not c.time_column): raise HTTPException(422,'契约字段不存在或时间配置不完整。')
        try: validate_expression(c,fields)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        for other in payload.wiki.related_metric_ids:
            if not con.execute("SELECT 1 FROM catalog_metrics WHERE id=? AND publish_status='published' AND domain_id=?",(other,row['domain_id'])).fetchone():
                raise HTTPException(422,'关联指标必须已发布且属于同一业务域。')
        wiki={**payload.wiki.model_dump(),'status':'approved','source_refs':['catalog:metric:'+metric_id],
              'source_snapshot':{k:row[k] for k in ('description','definition_sql','catalog_version')}}
        version=payload.expected_version+1
        con.execute('''UPDATE catalog_metrics SET aliases_json=?,contract_json=?,wiki_json=?,wiki_draft_json='{}',
                    certification_status='certified',publish_status='published',catalog_version=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                    (json.dumps(payload.aliases,ensure_ascii=False),c.model_dump_json(),json.dumps(wiki,ensure_ascii=False),version,metric_id))
        con.execute('INSERT INTO metric_wiki_revisions(metric_id,version,snapshot_json,actor_id) VALUES (?,?,?,?)',
                    (metric_id,version,payload.model_dump_json(),user_id))
        con.execute("INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,'metric_publish','metric',?,?)",(user_id,metric_id,json.dumps({'version':version})))
        con.commit()
    finally: con.close()
    return {'id':metric_id,'version':version,'status':'published'}
