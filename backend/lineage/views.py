"""Observed PostgreSQL view definitions, gated by the approved catalog snapshot."""
from __future__ import annotations

import json

from backend.core import db
from backend.lineage.catalog import assets_for, candidate_assets, snapshot, BindingError, validate_endpoints
from backend.lineage.models import EvidenceInput
from backend.lineage.storage import canonical, digest


def migrate(con):
    con.execute('''CREATE TABLE IF NOT EXISTS lineage_view_observations(
        source_id TEXT NOT NULL, namespace TEXT NOT NULL, name TEXT NOT NULL,
        source_revision INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
        definition_hash TEXT NOT NULL, definition TEXT NOT NULL, definition_status TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1, processed_generation INTEGER NOT NULL DEFAULT 0,
        evidence_ref TEXT, analysis_status TEXT NOT NULL DEFAULT 'pending',
        PRIMARY KEY(source_id,namespace,name))''')


def record_view_observations(con, source_id, discovery, version):
    """Record a complete authorized scan. Do not publish from an observation alone."""
    from backend.index_worker import enqueue_catalog

    revision = con.execute('SELECT lineage_source_revision FROM data_sources WHERE id=?', (source_id,)).fetchone()[0]
    existing = {(r['namespace'], r['name']): r for r in con.execute(
        'SELECT * FROM lineage_view_observations WHERE source_id=?', (source_id,))}
    changed = False
    for item in discovery.get('tables', []):
        if item.get('asset_type') not in {'view', 'materialized_view'}:
            continue
        key = (item['namespace'], item['name'])
        definition = item.get('view_definition') or ''
        state = item.get('view_definition_status', 'available' if definition else 'unavailable')
        if len(definition) > 100000:
            definition, state = '', 'too_large'
        content_hash = digest(definition) if definition else ''
        previous = existing.pop(key, None)
        if previous and (previous['source_revision'], previous['definition_hash'], previous['definition_status']) == (revision, content_hash, state):
            continue
        changed = True
        con.execute('''INSERT INTO lineage_view_observations
            (source_id,namespace,name,source_revision,snapshot_version,definition_hash,definition,definition_status)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id,namespace,name) DO UPDATE SET
            source_revision=excluded.source_revision,snapshot_version=excluded.snapshot_version,
            definition_hash=excluded.definition_hash,definition=excluded.definition,definition_status=excluded.definition_status,
            generation=lineage_view_observations.generation+1,evidence_ref=NULL,analysis_status='pending' ''',
            (source_id, *key, revision, version, content_hash, definition, state))
    for key, old in existing.items():
        if old['definition_status'] != 'missing':
            changed = True
            con.execute('''UPDATE lineage_view_observations SET definition='',definition_hash='',definition_status='missing',
                snapshot_version=?,generation=generation+1,evidence_ref=NULL,analysis_status='missing'
                WHERE source_id=? AND namespace=? AND name=?''', (version, source_id, *key))
    if changed:
        enqueue_catalog(con)


def queue_view_refresh(con, source_id):
    """Transactionally request re-binding after asset review/schema changes."""
    con.execute('''UPDATE lineage_view_observations SET generation=generation+1
        WHERE source_id=? AND definition_status='available' ''', (source_id,))


def _approved(con, row):
    from backend.catalog.sync import _table_key

    source = con.execute('SELECT enabled,kind,lineage_source_revision FROM data_sources WHERE id=?', (row['source_id'],)).fetchone()
    if (not source or not source['enabled'] or source['kind'] != 'postgresql'
            or source['lineage_source_revision'] != row['source_revision'] or row['definition_status'] != 'available'):
        return None
    review = con.execute('''SELECT payload_json FROM catalog_technical_state WHERE source_id=? AND entity_type='table'
        AND entity_key=?''', (row['source_id'], _table_key(row['namespace'], row['name']))).fetchone()
    if not review:
        return None
    payload = json.loads(review['payload_json'])
    if (payload.get('namespace'), payload.get('name'), payload.get('view_definition_hash'), payload.get('view_source_revision')) != (
        row['namespace'], row['name'], row['definition_hash'], row['source_revision'],
    ) or payload.get('asset_type') not in {'view', 'materialized_view'}:
        return None
    return payload


def _latest_observed_catalog(con, source_id):
    latest = con.execute('SELECT payload_json FROM catalog_sync_snapshots WHERE source_id=? ORDER BY version DESC LIMIT 1',
                         (source_id,)).fetchone()
    return {(a['namespace'],a['name']):a for a in json.loads(latest[0]).get('tables',[])} if latest else {}


def _catalog_matches_observation(observed, assets):
    """A newly observed physical schema must not be bound to old approved columns."""
    for asset in assets:
        item = observed.get((asset['namespace'],asset['physical_name']))
        if not item or item.get('asset_type','table') != asset['asset_type']:
            return False
        if {c['name']:c['type'] for c in item['columns']} != {k:c['data_type'] for k,c in asset['columns'].items()}:
            return False
    return True


def invalidate_drifted_views(con, source_id):
    """Invalidate observations on definition or physical-schema drift, not routine scans."""
    rows = con.execute('''SELECT v.namespace,v.name,s.metadata_json FROM lineage_view_observations v
        JOIN lineage_sources s ON s.id=v.evidence_ref WHERE v.source_id=?''', (source_id,)).fetchall()
    if not rows:
        return
    assets = assets_for(con, None, source_id=source_id, _system=True)
    observed = _latest_observed_catalog(con, source_id)
    changed = False
    for row in rows:
        refs = json.loads(row['metadata_json']).get('resolved_asset_ids', [])
        if any(aid not in assets for aid in refs) or not _catalog_matches_observation(observed, [assets[aid] for aid in refs]):
            changed = True
            con.execute('''UPDATE lineage_view_observations SET evidence_ref=NULL,analysis_status='waiting_review',generation=generation+1
                WHERE source_id=? AND namespace=? AND name=?''',(source_id,row['namespace'],row['name']))
    if changed:
        from backend.index_worker import enqueue_catalog
        enqueue_catalog(con)


def evidence_is_current(con, source_ref, source_id, metadata):
    row = con.execute('''SELECT * FROM lineage_view_observations WHERE source_id=? AND namespace=? AND name=?
        AND evidence_ref=?''', (source_id, metadata.get('namespace'), metadata.get('name'), source_ref)).fetchone()
    return bool(row and _approved(con, row) and row['snapshot_version']==metadata.get('snapshot_version')
                and row['definition_hash']==metadata.get('definition_hash'))


def assert_approved_for_query(con, asset):
    """A changed view definition must not be executed using stale catalog approval."""
    if asset['asset_type'] not in {'view', 'materialized_view'}:
        return None
    row = con.execute('SELECT * FROM lineage_view_observations WHERE source_id=? AND namespace=? AND name=?',
                      (asset['source_id'], asset['namespace'], asset['physical_name'])).fetchone()
    if not row or not _approved(con, row) or row['analysis_status'] != 'parsed' or not row['evidence_ref']:
        from backend.data_sources.query_catalog import QueryTargetError
        raise QueryTargetError('view_definition_unapproved', '视图定义未审核、已变化或尚未完成上游校验。', 409)
    return row


def cacheable_query(con, sql, user_id, source_id=None):
    """A view needs a fresh warehouse definition check; never replay its cached rows."""
    from backend.data_sources.query_catalog import QueryTargetError, resolve_target
    from backend.data_sources.sql_validation import parse_query

    if not con.execute("SELECT 1 FROM catalog_assets WHERE asset_type IN ('view','materialized_view') LIMIT 1").fetchone():
        return True
    try:
        _, tables = parse_query(sql)
        return all(asset['asset_type']=='table' for asset in resolve_target(tables,con,user_id,source_id))
    except QueryTargetError:
        return False


def query_view_definitions(con, user_id, assets):
    """Verify every view and its upstreams, even if the DB view owner can read more."""
    from backend.catalog.service import DISCOVER, QUERY_DATA, VIEW_METADATA, can_access
    from backend.data_sources.query_catalog import QueryTargetError
    from backend.lineage import service

    checked, visiting = {}, set()
    def visit(asset):
        row = assert_approved_for_query(con, asset)
        if row is None:
            return
        key = (asset['namespace'], asset['physical_name'])
        if key in checked:
            return
        if key in visiting or len(visiting) + len(checked) >= 30:
            raise QueryTargetError('view_definition_unapproved','视图依赖过深或形成循环，不能执行。',409)
        visiting.add(key)
        current_assets = assets_for(con, user_id, asset['source_id'])
        edges = [service.candidate_view(edge) for edge in con.execute('''SELECT * FROM lineage_candidates
            WHERE source_ref=? AND kind='table_transform' ''', (row['evidence_ref'],))]
        if not edges or any(e['status']!='published' or not service.current_candidate(con,e,current_assets,published=True) for e in edges):
            raise QueryTargetError('view_definition_unapproved','视图上游血缘不完整、失效或存在不可见资产。',409)
        for edge in edges:
            upstream = current_assets[edge['from_asset_id']]
            if not all(can_access(user_id, action, asset_id=upstream['id'], connection=con)
                       for action in (DISCOVER, VIEW_METADATA, QUERY_DATA)):
                raise QueryTargetError('permission_denied','缺少视图上游资产的 query_data 权限。',403)
            visit(upstream)
        visiting.remove(key)
        checked[key] = (asset['asset_type'], row['definition'])
    for asset in assets:
        visit(asset)
    return checked


def view_status(con, asset):
    if asset['asset_type'] not in {'view', 'materialized_view'}:
        return None
    row = con.execute('SELECT * FROM lineage_view_observations WHERE source_id=? AND namespace=? AND name=?',
                      (asset['source_id'], asset['namespace'], asset['physical_name'])).fetchone()
    if not row or row['definition_status'] != 'available':
        return row['definition_status'] if row else 'not_collected'
    if not _approved(con, row):
        return 'waiting_review'
    if row['generation'] != row['processed_generation']:
        return 'waiting_review' if row['analysis_status']=='waiting_review' and not row['evidence_ref'] else 'pending'
    return row['analysis_status']


def _analyze(con, row):
    from backend.lineage.sql_parser import parse_sql
    from backend.data_sources.sql_validation import parse_query, validate_postgres
    from backend.data_sources.query_catalog import QueryTargetError
    from backend.query_quality.sql_preflight import SQLSchemaError
    from backend.lineage.catalog import resolve_table

    if row['definition_status'] != 'available':
        return row['definition_status'], None, None, None
    approved = _approved(con, row)
    if not approved:
        return 'waiting_review', None, None, None
    assets = assets_for(con, None, source_id=row['source_id'], _system=True)
    target = next((a for a in assets.values() if (a['namespace'], a['physical_name'], a['asset_type']) ==
                   (row['namespace'], row['name'], approved['asset_type'])), None)
    if not target or target['publish_status'] != 'published':
        return 'waiting_assets', None, None, None
    # Use real ordinal order, and never treat an unrestricted SELECT as a materialization.
    analysis = parse_sql(row['definition'], assets, observation='database_view_definition', view_target=target)
    bound = {target['id']}
    for statement in analysis['statements']:
        bound.update(statement['context']['source_asset_ids'])
    pins = {aid: snapshot(assets[aid]) for aid in sorted(bound)}
    if not _catalog_matches_observation(_latest_observed_catalog(con,row['source_id']), [assets[aid] for aid in bound]):
        return 'waiting_review', None, None, None
    ready = []
    try:
        tree, tables = parse_query(row['definition'])
        validate_postgres(tree, tables, [resolve_table(t, assets) for t in tables], con)
    except (QueryTargetError, BindingError, SQLSchemaError):
        # An approved catalog entry is not permission to execute functions or
        # access sensitive columns hidden inside a database-side view.
        analysis['errors'].append({'code':'view_read_guard_not_supported'})
    for edge in analysis['candidates']:
        if edge['kind'] == 'join':
            continue
        try:
            validate_endpoints(edge, assets, published=True)
        except BindingError as exc:
            analysis['errors'].append({'code': exc.code, 'message': str(exc)})
        else:
            ready.append(edge)
    if analysis['errors'] and analysis['status'] == 'parsed':
        analysis['status'] = 'partial' if ready else 'unresolved'
    return analysis['status'], analysis, pins, assets


def process_pending_view(factory=db):
    """Parse outside the write transaction; publish only if the pins still match."""
    from backend.index_worker import enqueue_catalog
    from backend.lineage import service

    con = factory()
    try:
        # Pin a consistent read snapshot for metadata and approval while parsing.
        con.execute('BEGIN')
        row = con.execute('''SELECT * FROM lineage_view_observations WHERE generation>processed_generation
            ORDER BY snapshot_version,source_id,namespace,name LIMIT 1''').fetchone()
        if not row:
            return False
        status, analysis, pins, assets = _analyze(con, row)
    finally:
        con.close()

    key = (row['source_id'], row['namespace'], row['name'])
    con = factory()
    try:
        with con:
            con.execute('BEGIN IMMEDIATE')
            current = con.execute('SELECT * FROM lineage_view_observations WHERE source_id=? AND namespace=? AND name=?', key).fetchone()
            if not current or current['generation'] != row['generation'] or current['processed_generation'] >= row['generation']:
                return True
            source_ref = None
            if analysis is not None:
                if not _approved(con, current):
                    status = 'waiting_review'
                else:
                    live = assets_for(con, None, source_id=row['source_id'], asset_ids=list(pins), _system=True)
                    if {aid: snapshot(a) for aid, a in live.items()} != pins:
                        return True
                    metadata = dict(namespace=row['namespace'], name=row['name'], snapshot_version=row['snapshot_version'],
                                    definition_hash=row['definition_hash'], resolved_asset_ids=sorted(pins))
                    payload = EvidenceInput(title=('PostgreSQL 视图定义 / '+'.'.join(key[1:]))[:200],
                        content=row['definition'], source_id=row['source_id'], source_uri='postgres-view://'+digest(key),
                        source_version=f"{row['source_revision']}:{row['snapshot_version']}:"+digest(pins)[:32])
                    source_ref, created = service._store_source(con, payload, 'system:lineage', analysis,
                        source_kind='postgres_view_definition', metadata=metadata)
                    if created:
                        service._persist_candidates(con, source_ref, analysis, assets)
                        for edge in analysis['candidates']:
                            if edge['kind'] == 'join':
                                continue
                            try:
                                validate_endpoints(edge, live, published=True)
                            except BindingError:
                                continue
                            identifier = 'edge:'+digest([source_ref,edge])[:40]
                            con.execute('''UPDATE lineage_candidates SET status='published',review_json=?,reviewed_by=?
                                WHERE id=? AND status='draft' ''',
                                (canonical({'basis':'approved_view_definition'}), 'system:lineage', identifier))
                    # A previously withdrawn candidate/source is never re-published by a rescan.
            con.execute('''UPDATE lineage_view_observations SET processed_generation=?,analysis_status=?,evidence_ref=?
                WHERE source_id=? AND namespace=? AND name=?''', (row['generation'], status, source_ref, *key))
            if source_ref != current['evidence_ref']:
                enqueue_catalog(con)
        return True
    finally:
        con.close()
