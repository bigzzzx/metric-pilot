"""Immutable evidence and optimistic review lifecycle; raw sources stay admin-only."""
from __future__ import annotations

import json
import hashlib
import uuid
from datetime import UTC, datetime
from fastapi import HTTPException

from backend.catalog.service import MANAGE, QUERY_DATA, can_access, is_platform_admin
from backend.core import db, DATA_DIR
from backend.lineage.catalog import BindingError, assets_for, candidate_assets, snapshot, validate_endpoints
from backend.lineage.models import EvidenceInput, ReviewInput
from backend.lineage.sql_parser import parse_sql
from backend.lineage.storage import canonical, digest

SOURCE_ROOT = DATA_DIR / 'lineage-evidence'


def require_admin(con,user_id):
    if not can_access(user_id,MANAGE,connection=con):
        raise HTTPException(403,'缺少 manage 目录管理权限，请向管理员申请。')


def candidate_view(row):
    item = dict(row)
    for name in ('context','locator','schema','checks','review'):
        item[name] = json.loads(item.pop(name+'_json') or '{}')
    return item


def _source_view(con,row,*,content=False):
    item = dict(row)
    for name in ('scope','metadata'):
        item[name] = json.loads(item.pop(name+'_json'))
    state = con.execute('SELECT * FROM lineage_source_states WHERE source_ref=?',(item['id'],)).fetchone()
    item['state'] = {**dict(state),'analysis':json.loads(state['analysis_json'])} if state else {}
    item['state'].pop('analysis_json',None)
    if not content:
        item.pop('content',None)
        item['metadata'].pop('blocks',None)
        item['metadata'].pop('storage_path',None)
    return item


def _can_read_source(con,row,user_id):
    if is_platform_admin(user_id,con):
        return True
    ids = json.loads(row['scope_json']).get('asset_ids',[])
    ids = sorted(set(ids)|set(json.loads(row['metadata_json']).get('resolved_asset_ids',[])))
    return set(ids) <= set(assets_for(con,user_id,asset_ids=ids)) if ids else row['created_by']==user_id


def get_source(source_ref,user_id,*,connection_factory=None):
    con = (connection_factory or db)()
    try:
        require_admin(con,user_id)
        row = con.execute('SELECT * FROM lineage_sources WHERE id=?',(source_ref,)).fetchone()
        if not row or not _can_read_source(con,row,user_id):
            raise HTTPException(404,'证据不存在或无权查看。')
        return _source_view(con,row,content=True)
    finally:
        con.close()


def list_sources(user_id,*,connection_factory=None):
    con = (connection_factory or db)()
    try:
        require_admin(con,user_id)
        return [_source_view(con,row) for row in con.execute('SELECT * FROM lineage_sources ORDER BY created_at DESC,id DESC LIMIT 100')
                if _can_read_source(con,row,user_id)]
    finally:
        con.close()


def _store_source(con,payload,user_id,analysis,*,source_kind=None,metadata=None):
    kind = source_kind or payload.source_kind
    content_hash = hashlib.sha256(payload.content.encode()).hexdigest()
    version = payload.source_version or content_hash
    scope = dict(asset_ids=sorted(set(payload.asset_ids)),source_id=payload.source_id)
    identifier = 'evidence:'+digest(dict(kind=kind,source_id=payload.source_id,uri=payload.source_uri,
        version=version,hash=content_hash,scope=scope))[:40]
    if payload.source_uri:
        existing = con.execute('''SELECT content_hash FROM lineage_sources WHERE source_kind=?
            AND COALESCE(source_id,'')=? AND source_uri=? AND source_version=?''',
            (kind,payload.source_id or '',payload.source_uri,version)).fetchone()
        if existing and existing['content_hash'] != content_hash:
            raise HTTPException(409,'同一来源版本已经登记不同内容，请使用新的来源版本。')
    exists = con.execute('SELECT 1 FROM lineage_sources WHERE id=?',(identifier,)).fetchone()
    if exists:
        return identifier,False
    con.execute('''INSERT INTO lineage_sources(id,source_kind,source_id,source_uri,source_version,title,
        content,content_hash,scope_json,metadata_json,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (identifier,kind,payload.source_id,payload.source_uri,version,payload.title,payload.content,content_hash,
         canonical(scope),canonical(metadata or {'run':payload.run_metadata}),user_id))
    con.execute('INSERT INTO lineage_source_states(source_ref,analysis_json) VALUES (?,?)',
                (identifier,canonical({k:v for k,v in analysis.items() if k!='candidates'})))
    return identifier,True


def _persist_candidates(con,source_ref,analysis,assets):
    added = []
    for candidate in analysis.get('candidates',[]):
        try:
            pinned = validate_endpoints(candidate,assets)
        except BindingError as exc:
            analysis.setdefault('errors',[]).append(dict(code=exc.code,message=str(exc)))
            continue
        identifier = 'edge:'+digest([source_ref,candidate])[:40]
        con.execute('''INSERT OR IGNORE INTO lineage_candidates(id,source_ref,kind,from_asset_id,to_asset_id,
            from_column,to_column,expression,context_json,locator_json,observation,schema_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (identifier,source_ref,candidate['kind'],candidate['from_asset_id'],
            candidate['to_asset_id'],candidate.get('from_column',''),candidate.get('to_column',''),candidate['expression'],
            canonical(candidate.get('context',{})),canonical(candidate.get('locator',{})),candidate['observation'],canonical(pinned)))
        added.append(identifier)
        if candidate['kind']=='join':
            from backend.lineage.validation import family
            left = family(assets[candidate['from_asset_id']]['columns'][candidate['from_column']]['data_type'])
            right = family(assets[candidate['to_asset_id']]['columns'][candidate['to_column']]['data_type'])
            mismatch = left!=right and 'unknown' not in {left,right}
            # Do not overwrite a prior reviewed row on re-extraction.
            con.execute('''UPDATE lineage_candidates SET technical_status=?,checks_json=?
                WHERE id=? AND status='draft' AND version=1''',
                ('type_mismatch' if mismatch else 'schema_validated',canonical(dict(schema='passed',
                    type_compatibility='mismatch' if mismatch else 'unknown' if 'unknown' in {left,right} else 'compatible',
                    business_semantics='requires_human_review')),identifier))
    if analysis.get('errors') and analysis.get('status')=='parsed':
        analysis['status'] = 'partial' if added else 'unresolved'
    summary = {k:v for k,v in analysis.items() if k!='candidates'}
    summary['candidate_count'] = len(added)
    con.execute('UPDATE lineage_source_states SET analysis_json=? WHERE source_ref=?',(canonical(summary),source_ref))
    return added


def import_evidence(payload: EvidenceInput,user_id,*,connection_factory=None,_source_kind=None,_metadata=None,_system=False):
    factory = connection_factory or db
    con = factory()
    try:
        if not _system:
            require_admin(con,user_id)
        assets = assets_for(con,user_id,payload.source_id,payload.asset_ids or None)
        if payload.asset_ids and not set(payload.asset_ids)<=set(assets):
            raise HTTPException(403,'部分资产不存在或缺少 discover/view_metadata 权限。')
        if payload.source_kind == 'business_document':
            analysis = dict(status='awaiting_model' if payload.asset_ids else 'needs_assets',candidates=[],errors=[])
            metadata = _metadata or dict(blocks=[dict(text=payload.content,locator={'kind':'pasted_text','line':1})])
        else:
            kinds = {a['source_kind'] for a in assets.values()}
            dialect = 'sqlite' if kinds == {'local-sqlite'} else 'postgres'
            state = payload.run_metadata.get('state','DESIGN').upper()
            if _source_kind=='production_run':
                observation = 'run_observed' if state=='COMPLETE' else 'run_failed'
            elif payload.source_kind=='external_run':
                observation = 'external_reported' if state=='COMPLETE' else 'run_failed' if state in {'FAIL','ABORT'} else 'design'
            elif _source_kind=='query_execution':
                observation = 'query_observed'
            else:
                observation = 'design'
            analysis = parse_sql(payload.content,assets,dialect=dialect,observation=observation)
            metadata = _metadata or {'run':payload.run_metadata}
            resolved = {aid for c in analysis.get('candidates',[]) for aid in candidate_assets(c)}
            for statement in analysis.get('statements',[]):
                resolved.update(statement['context']['source_asset_ids'])
                if statement['context'].get('target_asset_id'): resolved.add(statement['context']['target_asset_id'])
            metadata = {**metadata,'resolved_asset_ids':sorted(resolved)}
        con.execute('BEGIN IMMEDIATE')
        source_ref,created = _store_source(con,payload,user_id,analysis,source_kind=_source_kind,metadata=metadata)
        if created:
            _persist_candidates(con,source_ref,analysis,assets)
        con.commit()
        row = con.execute('SELECT * FROM lineage_sources WHERE id=?',(source_ref,)).fetchone()
        return {**_source_view(con,row,content=False),'idempotent':not created}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def current_candidate(con,item,assets,*,published=False):
    try:
        pins = validate_endpoints(item,assets,published=published)
        source = con.execute('SELECT status FROM lineage_source_states WHERE source_ref=?',(item['source_ref'],)).fetchone()
        if not source or source['status']!='active' or digest(pins)!=digest(item['schema']):
            return False
        raw = con.execute('SELECT source_kind,source_id,metadata_json FROM lineage_sources WHERE id=?',(item['source_ref'],)).fetchone()
        if raw and raw['source_kind'] in {'database_constraint','catalog_declaration'}:
            if raw['source_kind']=='database_constraint':
                state = con.execute('SELECT snapshot_hash AS content_hash FROM catalog_sync_snapshots WHERE source_id=? ORDER BY version DESC LIMIT 1',(raw['source_id'],)).fetchone()
            else:
                state = con.execute("SELECT content_hash FROM catalog_technical_state WHERE source_id=? AND entity_type='relationships' AND entity_key='__all__'",(raw['source_id'],)).fetchone()
            if not state or state['content_hash']!=json.loads(raw['metadata_json']).get('technical_state_hash'):
                return False
        return True
    except BindingError:
        return False


def candidate_visible(con,item,user_id):
    if item['status'] != 'published':
        return False
    return current_candidate(con,item,assets_for(con,user_id,asset_ids=candidate_assets(item)),published=True)


def _decorate(con,item,assets):
    item['effective_status'] = item['status'] if current_candidate(con,item,assets) else 'stale'
    item['from_asset'] = {k:assets[item['from_asset_id']][k] for k in ('id','source_id','namespace','physical_name','display_name')} if item['from_asset_id'] in assets else None
    item['to_asset'] = {k:assets[item['to_asset_id']][k] for k in ('id','source_id','namespace','physical_name','display_name')} if item['to_asset_id'] in assets else None
    source = con.execute('SELECT source_kind,title,source_version,content_hash,created_at,metadata_json FROM lineage_sources WHERE id=?',(item['source_ref'],)).fetchone()
    item['source'] = dict(source) if source else None
    if item['source']:
        metadata = json.loads(item['source'].pop('metadata_json'))
        if source['source_kind']=='production_run':
            run = metadata.get('run',{})
            item['source']['run'] = {k:run[k] for k in ('run_id','job_id','job_version','state','started_at','completed_at') if k in run}
            item['source']['run']['parameters'] = {k:v for k,v in run.get('parameters',{}).items()
                if k in {'paid_value','range_start','range_end','processed_at','production_run_id'}}
    return item


def list_candidates(user_id,*,status=None,asset_id=None,source_ref=None,connection_factory=None):
    con = (connection_factory or db)()
    try:
        manager = can_access(user_id,MANAGE,connection=con)
        assets = assets_for(con,user_id)
        rows = con.execute('''SELECT * FROM lineage_candidates WHERE (? IS NULL OR status=?)
            AND (? IS NULL OR from_asset_id=? OR to_asset_id=?) AND (? IS NULL OR source_ref=?)
            ORDER BY created_at DESC,id LIMIT 1000''', (status,status,asset_id,asset_id,asset_id,source_ref,source_ref))
        result = []
        for row in rows:
            item = candidate_view(row)
            if not candidate_assets(item)<=set(assets):
                continue
            if not manager and not candidate_visible(con,item,user_id):
                continue
            result.append(_decorate(con,item,assets))
        return result
    finally:
        con.close()


def _get_for_admin(con,identifier,user_id):
    require_admin(con,user_id)
    row = con.execute('SELECT * FROM lineage_candidates WHERE id=?',(identifier,)).fetchone()
    if not row:
        raise HTTPException(404,'候选不存在。')
    item = candidate_view(row)
    if not candidate_assets(item)<=set(assets_for(con,user_id)):
        raise HTTPException(404,'候选不存在或无权查看全部端点。')
    return item


def audit(con,item,user_id,action,details=None):
    con.execute('''INSERT INTO lineage_audit(candidate_id,source_ref,actor_id,action,version,details_json)
        VALUES (?,?,?,?,?,?)''',(item['id'],item['source_ref'],user_id,action,item['version'],canonical(details or {})))


def _queue_index(con):
    identifier = str(uuid.uuid4())
    con.execute('INSERT INTO lineage_index_jobs(id) VALUES (?)',(identifier,))
    return identifier


def review_candidate(identifier,payload: ReviewInput,user_id,*,connection_factory=None):
    con = (connection_factory or db)()
    try:
        con.execute('BEGIN IMMEDIATE')
        item = _get_for_admin(con,identifier,user_id)
        if item['version'] != payload.expected_version:
            raise HTTPException(409,'候选版本已变化，请刷新后重新审核。')
        if item['status'] not in {'draft','rejected','withdrawn','stale','approved'}:
            raise HTTPException(409,'已发布候选请先撤回再审核。')
        if payload.decision=='approve' and not current_candidate(con,item,assets_for(con,user_id)):
            raise HTTPException(409,'来源或 Schema 已变化，请重新验证并重新审核。')
        if payload.decision=='approve' and item['technical_status']=='type_mismatch':
            raise HTTPException(422,'关联字段类型不兼容，请修正来源后重新验证。')
        status = 'approved' if payload.decision=='approve' else 'rejected'
        con.execute('''UPDATE lineage_candidates SET status=?,version=version+1,review_json=?,reviewed_by=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?''', (status,canonical(payload.model_dump()),user_id,identifier))
        item['version'] += 1
        audit(con,item,user_id,'review_'+payload.decision,payload.model_dump())
        con.commit()
        return dict(id=identifier,status=status,version=item['version'])
    except Exception:
        con.rollback(); raise
    finally:
        con.close()


def change_candidate_status(identifier,expected_version,user_id,*,publish=False,connection_factory=None):
    con = (connection_factory or db)()
    try:
        con.execute('BEGIN IMMEDIATE')
        item = _get_for_admin(con,identifier,user_id)
        if item['version'] != expected_version:
            raise HTTPException(409,'候选版本已变化，请刷新后重试。')
        if publish:
            if item['status'] != 'approved':
                raise HTTPException(409,'需要先人工审核批准候选，再显式发布。')
            if item['observation']=='run_failed':
                raise HTTPException(422,'失败/回滚运行不能发布为成功产出，请单独登记经过审核的设计定义。')
            if item['technical_status']=='type_mismatch':
                raise HTTPException(422,'关联字段类型不兼容，不能发布。')
            if not current_candidate(con,item,assets_for(con,user_id),published=True):
                raise HTTPException(409,'证据或资产字段版本已失效、受限或未发布，请重新验证审核。')
            status = 'published'
        else:
            if item['status'] not in {'published','approved'}:
                raise HTTPException(409,'仅已批准或已发布候选可撤回。')
            status = 'withdrawn'
        con.execute('UPDATE lineage_candidates SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?',(status,identifier))
        item['version'] += 1
        audit(con,item,user_id,status)
        job_id = _queue_index(con)
        con.commit()
        return dict(id=identifier,status=status,version=item['version'],index_job_id=job_id)
    except Exception:
        con.rollback(); raise
    finally:
        con.close()


def change_source_status(source_ref,payload,user_id,*,connection_factory=None):
    factory = connection_factory or db
    get_source(source_ref,user_id,connection_factory=factory)
    con = factory()
    try:
        con.execute('BEGIN IMMEDIATE')
        state = con.execute('SELECT * FROM lineage_source_states WHERE source_ref=?',(source_ref,)).fetchone()
        if state['version'] != payload.expected_version:
            raise HTTPException(409,'证据状态版本已变化。')
        con.execute('UPDATE lineage_source_states SET status=?,reason=?,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE source_ref=?',
            (payload.status,payload.reason,source_ref))
        # Re-activation never silently re-publishes a previously invalidated edge.
        con.execute("UPDATE lineage_candidates SET status='stale',version=version+1 WHERE source_ref=?",(source_ref,))
        con.execute('''INSERT INTO lineage_audit(source_ref,actor_id,action,version,details_json) VALUES (?,?,?,?,?)''',
            (source_ref,user_id,'source_'+payload.status,state['version']+1,canonical({'reason':payload.reason})))
        job_id = _queue_index(con)
        con.commit()
        return dict(source_ref=source_ref,status=payload.status,version=state['version']+1,index_job_id=job_id)
    except Exception:
        con.rollback(); raise
    finally:
        con.close()


def run_index_job(job_id,*,connection_factory=None):
    from backend.index_worker import run_job
    return run_job('lineage',job_id,connection_factory=connection_factory or db)


def reanalyze(source_ref,user_id,*,connection_factory=None):
    factory = connection_factory or db
    source = get_source(source_ref,user_id,connection_factory=factory)
    if source['source_kind'] not in {'sql_import','etl_definition','query_execution','production_run'}:
        raise HTTPException(422,'该来源请使用文档提取或新的结构化事件导入。')
    if source['state']['status']!='active': raise HTTPException(409,'请先显式恢复失效证据。')
    con = factory()
    try:
        assets = assets_for(con,user_id,source['source_id'],source['scope']['asset_ids'] or None)
        existing = con.execute('SELECT observation FROM lineage_candidates WHERE source_ref=? LIMIT 1',(source_ref,)).fetchone()
        obs = existing['observation'] if existing else 'query_observed' if source['source_kind']=='query_execution' else 'run_observed' if source['source_kind']=='production_run' and source['metadata'].get('run',{}).get('state')=='COMPLETE' else 'run_failed' if source['source_kind']=='production_run' else 'design'
        parsed = parse_sql(source['content'],assets,dialect='sqlite' if {a['source_kind'] for a in assets.values()}=={'local-sqlite'} else 'postgres',observation=obs)
        con.execute('BEGIN IMMEDIATE')
        current = con.execute('SELECT status,version FROM lineage_source_states WHERE source_ref=?',(source_ref,)).fetchone()
        if current['status']!='active' or current['version']!=source['state']['version']:
            raise HTTPException(409,'来源状态已变化。')
        # Old human-reviewed content is never mutated by re-analysis.
        added = _persist_candidates(con,source_ref,parsed,assets)
        con.commit()
        return dict(source_ref=source_ref,status=parsed['status'],candidate_count=len(added),errors=parsed['errors'])
    finally: con.close()
