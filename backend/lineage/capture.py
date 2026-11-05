"""Collect only available platform evidence. No external DB log access is implied."""
import json
import uuid

from backend.core import db
from backend.observability import log_event
from backend.lineage import service
from backend.lineage.catalog import assets_for, resolve_table
from backend.lineage.models import EvidenceInput
from backend.lineage.storage import canonical, digest


def record_executed_query(sql,user_id,source_id,origin,*,connection_factory=None):
    """Called only after an actual successful read; cache hits do not enter here."""
    if len(sql)>100000:
        log_event('lineage.query_capture_skipped',level=30,reason='sql_budget_exceeded')
        return
    con = (connection_factory or db)()
    try:
        con.execute('INSERT INTO lineage_query_history(id,user_id,source_id,sql_text,origin) VALUES (?,?,?,?,?)',
            (str(uuid.uuid4()),user_id,source_id or 'local-ecommerce-demo',sql,origin))
        # This is a bounded operational queue, not immutable imported evidence.
        con.execute('''DELETE FROM lineage_query_history WHERE id NOT IN
            (SELECT id FROM lineage_query_history ORDER BY created_at DESC,rowid DESC LIMIT 1000)''')
        con.commit()
    except Exception as exc:
        log_event('lineage.query_capture_failed',level=30,error_type=type(exc).__name__)
    finally:
        con.close()


def capture_production(run_id,*,connection_factory=None,user_id=None):
    factory = connection_factory or db
    con = factory()
    try:
        run = con.execute('SELECT * FROM production_runs WHERE id=?',(run_id,)).fetchone()
        if not run:
            return dict(status='missing_run')
        job = con.execute('SELECT * FROM production_jobs WHERE id=?',(run['job_id'],)).fetchone()
        if not job:
            return dict(status='missing_job')
        config = json.loads(job['config_json'])
        result = json.loads(run['result_json'] or '{}')
        actor = user_id or run['actor_id']
        success = run['status']=='succeeded'
        # Durable job versions must match; never apply today's definition to an older run.
        if job['version']!=run['job_version']:
            return dict(status='historical_job_version_unavailable',run_id=run_id)
        sql = result.get('sql_template')
        payload = EvidenceInput(title=job['name']+' / '+run_id,source_id=config['source_id'],
            source_uri='platform://production/'+run_id,source_version=str(run['job_version'])+':'+run['status'],
            content=sql or canonical(dict(run_id=run_id,status=run['status'],error_code=run['error_code'])),
            run_metadata=dict(state='COMPLETE' if success else 'FAIL',run_id=run_id,job_id=job['id'],
                job_version=run['job_version'],started_at=run['started_at'],completed_at=run['completed_at'],
                parameters=result.get('parameters',{}),error_code=run['error_code']))
    finally:
        con.close()
    return service.import_evidence(payload,actor,connection_factory=factory,_source_kind='production_run',_system=True)


def safe_capture_production(run_id,*,connection_factory=None):
    try:
        return capture_production(run_id,connection_factory=connection_factory)
    except Exception as exc:
        # Do not misreport an already committed business transaction as rolled back.
        log_event('lineage.production_capture_failed',level=30,run_id=run_id,error_type=type(exc).__name__)
        return dict(status='capture_failed',run_id=run_id)


def collect_available(user_id,*,connection_factory=None):
    """Explicit bounded collection, with idempotent immutable evidence identities."""
    factory = connection_factory or db
    con = factory()
    try:
        service.require_admin(con,user_id)
        history = [dict(r) for r in con.execute('SELECT * FROM lineage_query_history ORDER BY created_at DESC LIMIT 100')]
        runs = [r['id'] for r in con.execute("SELECT id FROM production_runs WHERE status IN ('succeeded','failed') ORDER BY started_at DESC LIMIT 50")]
        assets = assets_for(con,user_id)
        states = [dict(r) for r in con.execute("SELECT * FROM catalog_technical_state WHERE entity_type='relationships' LIMIT 100")]
        discoveries = [dict(r) for r in con.execute('''SELECT s.* FROM catalog_sync_snapshots s WHERE s.version=
            (SELECT MAX(x.version) FROM catalog_sync_snapshots x WHERE x.source_id=s.source_id) LIMIT 100''')]
        discovered_sources = {r['source_id'] for r in discoveries}
        states = [s for s in states if s['source_id'] not in discovered_sources]
        for row in discoveries:
            states.append(dict(source_id=row['source_id'],version=row['version'],content_hash=row['snapshot_hash'],
                payload_json=canonical(json.loads(row['payload_json']).get('relationships',[])),discovery_id=row['id']))
    finally:
        con.close()
    results,errors = [],[]
    for item in history:
        try:
            results.append(service.import_evidence(EvidenceInput(title='平台成功查询 / '+item['origin'],
                content=item['sql_text'],source_id=item['source_id'],source_uri='platform://queries/'+item['id'],
                source_version='1'),user_id,connection_factory=factory,_source_kind='query_execution',
                _metadata=dict(executed_by=item['user_id'],executed_at=item['created_at'],origin=item['origin'])))
        except Exception as exc:
            errors.append(dict(kind='query_execution',code=type(exc).__name__))
    for rid in runs:
        try: results.append(capture_production(rid,connection_factory=factory,user_id=user_id))
        except Exception as exc: errors.append(dict(kind='production_run',code=type(exc).__name__))
    # Only a discovery snapshot is called a database declaration. Baselines in
    # technical_state can originate from demo/manual catalog records.
    from sqlglot import exp
    for state in states:
        try:
            refs = json.loads(state['payload_json'])
            scope_assets = {k:v for k,v in assets.items() if v['source_id']==state['source_id']}
            candidates = []
            for index,rel in enumerate(refs):
                left = resolve_table(exp.Table(this=exp.to_identifier(rel['from_table'],quoted=True),db=exp.to_identifier(rel.get('from_namespace') or '',quoted=True)),scope_assets)
                right = resolve_table(exp.Table(this=exp.to_identifier(rel['to_table'],quoted=True),db=exp.to_identifier(rel.get('to_namespace') or '',quoted=True)),scope_assets)
                candidates.append(dict(kind='join',from_asset_id=left['id'],from_column=rel['from_column'],
                    to_asset_id=right['id'],to_column=rel['to_column'],expression=f"{left['physical_name']}.{rel['from_column']} = {right['physical_name']}.{rel['to_column']}",
                    context=dict(all_references=[],technical_state_hash=state['content_hash'],source_asset_ids=[left['id'],right['id']]),
                    locator=dict(constraint_index=index),observation='database_declared' if state.get('discovery_id') else 'catalog_declared'))
            payload = EvidenceInput(title='数据库关系声明快照',content=state['payload_json'],source_id=state['source_id'],
                source_uri=('platform://discovery-constraints/' if state.get('discovery_id') else 'platform://catalog-declarations/')+state['source_id'],source_version=str(state['version']))
            con = factory()
            try:
                con.execute('BEGIN IMMEDIATE')
                analysis = dict(status='parsed',candidates=candidates,errors=[])
                sid,created = service._store_source(con,payload,user_id,analysis,source_kind='database_constraint' if state.get('discovery_id') else 'catalog_declaration',
                    metadata=dict(technical_state_hash=state['content_hash'],technical_state_version=state['version'],
                        discovery_id=state.get('discovery_id'),
                        resolved_asset_ids=sorted({a for c in candidates for a in (c['from_asset_id'],c['to_asset_id'])})))
                if created: service._persist_candidates(con,sid,analysis,assets)
                con.commit(); results.append(dict(id=sid,idempotent=not created))
            finally: con.close()
        except Exception as exc: errors.append(dict(kind='database_constraint',code=type(exc).__name__))
    return dict(sources=results,errors=errors,history_rows=len(history),production_runs=len(runs),
        limitation='仅采集平台已记录查询/生产及已采集技术快照；未连接外部审计日志。每次最多 100 条查询、50 次运行。')
