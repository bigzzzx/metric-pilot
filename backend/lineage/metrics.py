"""Versioned metric lineage, derived only from a certified executable contract."""
from __future__ import annotations

import json

from fastapi import HTTPException
from sqlglot import exp
from sqlglot.errors import SqlglotError

from backend.catalog.service import DISCOVER, VIEW_METADATA, can_access
from backend.core import db
from backend.lineage.catalog import assets_for
from backend.lineage.storage import canonical, digest
from backend.semantic_metrics.compiler import measure_ast
from backend.semantic_metrics.models import MetricContract


def migrate(con):
    con.execute('''CREATE TABLE IF NOT EXISTS metric_lineage_revisions(
        metric_id TEXT NOT NULL,metric_version INTEGER NOT NULL,contract_hash TEXT NOT NULL,
        asset_id TEXT NOT NULL,dependencies_json TEXT NOT NULL,
        PRIMARY KEY(metric_id,metric_version))''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_metric_lineage_asset ON metric_lineage_revisions(asset_id)')
    # Existing certified definitions need no second review. Never infer from Wiki.
    for row in con.execute('''SELECT id,catalog_version,contract_json FROM catalog_metrics
        WHERE publish_status='published' AND certification_status='certified' AND contract_json!='{}'
        AND NOT EXISTS (SELECT 1 FROM metric_lineage_revisions r
            WHERE r.metric_id=catalog_metrics.id AND r.metric_version=catalog_metrics.catalog_version)'''):
        try:
            contract = MetricContract.model_validate_json(row['contract_json'])
            record_contract(con, row['id'], row['catalog_version'], contract)
        except (ValueError, SqlglotError):
            # An invalid legacy definition is not evidence.
            continue


def _validated_asset(con, contract):
    asset = assets_for(con, None, source_id=contract.source_id, asset_ids=[contract.asset_id], _system=True).get(contract.asset_id)
    if not asset or not asset['source_enabled'] or asset['publish_status'] != 'published':
        raise ValueError('Metric input asset is unavailable')
    if (asset['namespace'], asset['physical_name'], asset['source_kind']) != (
        contract.namespace, contract.table, 'postgresql' if contract.dialect == 'postgresql' else 'local-sqlite',
    ):
        raise ValueError('Metric input asset no longer matches its contract')
    return asset


def _extract(contract, asset):
    allowed = {key for key, col in asset['columns'].items() if col['sensitivity'] not in {'sensitive', 'restricted'}}
    tree = measure_ast(contract, allowed)
    result = [{'role': 'dataset', 'column': ''}]
    for column in sorted({c.name for c in tree.find_all(exp.Column)}):
        result.append({'role': 'value', 'column': column})
    if any(isinstance(c.this, exp.Star) for c in tree.find_all(exp.Count)):
        result.append({'role': 'row_count', 'column': ''})
    for item in contract.required_filters:
        result.append({'role': 'required_filter', 'column': item.column, 'operator': item.operator, 'value': item.value})
    if contract.time_column:
        result.append({'role': 'time', 'column': contract.time_column, 'required': contract.requires_time_range})
    for key, dim in sorted(contract.dimensions.items()):
        result.append({'role': 'optional_dimension', 'column': dim.column, 'dimension': key, 'label': dim.label})
    for dep in result:
        if dep['column']:
            if dep['column'] not in allowed:
                raise ValueError('Metric dependency column is unavailable')
            col = asset['columns'][dep['column']]
            dep.update(column_id=col['id'], data_type=col['data_type'])
    return result


def record_contract(con, metric_id, version, contract):
    """Called in the metric publication transaction, without any warehouse I/O."""
    if contract.status != 'active':
        return 0
    deps = _extract(contract, _validated_asset(con, contract))
    con.execute('''INSERT INTO metric_lineage_revisions
        (metric_id,metric_version,contract_hash,asset_id,dependencies_json) VALUES (?,?,?,?,?)''',
        (metric_id, version, digest(contract.model_dump(mode='json')), contract.asset_id, canonical(deps)))
    return len(deps)


def _visible(con, user_id, metric, asset_id=None):
    return all(can_access(user_id, action, domain_id=metric['domain_id'], connection=con)
               for action in (DISCOVER, VIEW_METADATA)) and (not asset_id or all(
        can_access(user_id, action, asset_id=asset_id, connection=con) for action in (DISCOVER, VIEW_METADATA)))


def _current(con, user_id, metric):
    if metric['certification_status'] != 'certified' or metric['publish_status'] != 'published':
        return None
    revision = con.execute('''SELECT * FROM metric_lineage_revisions WHERE metric_id=? AND metric_version=?''',
                           (metric['id'], metric['catalog_version'])).fetchone()
    if not revision or not _visible(con, user_id, metric, revision['asset_id']):
        return None
    try:
        contract = MetricContract.model_validate_json(metric['contract_json'])
        if contract.status != 'active' or digest(contract.model_dump(mode='json')) != revision['contract_hash']:
            return None
        asset = _validated_asset(con, contract)
        if asset['domain_id'] != metric['domain_id']:
            return None
        if asset['asset_type'] in {'view', 'materialized_view'}:
            from backend.lineage.views import view_status
            if view_status(con, asset) not in {'parsed', 'partial'}:
                return None
        deps = json.loads(revision['dependencies_json'])
        for dep in deps:
            if not dep['column']:
                continue
            col = asset['columns'].get(dep['column'])
            if (not col or col['sensitivity'] in {'sensitive', 'restricted'} or col['id'] != dep['column_id']
                    or col['data_type'] != dep['data_type']):
                return None
        return contract, asset, deps
    except (ValueError, HTTPException):
        return None


def metric_lineage(user_id, metric_id, *, connection_factory=None):
    from backend.lineage.projection import walk_edges, published_candidates

    con = (connection_factory or db)()
    try:
        con.execute('BEGIN')
        metric = con.execute("SELECT * FROM catalog_metrics WHERE id=? AND publish_status='published'", (metric_id,)).fetchone()
        if not metric or not _visible(con, user_id, metric):
            raise HTTPException(404, '指标不存在或无权查看。')
        try:
            bound = MetricContract.model_validate_json(metric['contract_json']).asset_id
        except ValueError:
            bound = None
        if bound and not _visible(con, user_id, metric, bound):
            raise HTTPException(404, '指标不存在或无权查看。')
        result = dict(metric_id=metric['id'], display_name=metric['display_name'], metric_version=metric['catalog_version'],
                      provenance='certified_metric_contract', status='unavailable', dependencies=[], upstream_edges=[])
        current = _current(con, user_id, metric)
        if not current:
            return result
        contract, asset, deps = current
        edges, assets = published_candidates(con, user_id)
        # Traverse all required and optional referenced columns in one walk.
        anchors = {(asset['id'], d['column']) for d in deps if d['column']}
        upstream = walk_edges(con, assets, edges, anchors, 'upstream', 3, include_influences=True)[0]
        if not anchors or any(d['role'] == 'row_count' for d in deps):
            table_edges = walk_edges(con, assets, edges, {(asset['id'], '')}, 'upstream', 3)[0]
            upstream = list({e['id']: e for e in [*upstream, *table_edges]}.values())
        return {**result, 'status': 'current', 'dependencies': deps, 'upstream_edges': upstream,
                'asset': {key: asset[key] for key in ('id', 'source_id', 'namespace', 'physical_name', 'display_name')},
                'measure_expression': contract.measure_expression}
    finally:
        con.close()


def metric_impacts(con, user_id, nodes, *, via_column):
    """Reverse lookup of current certified metrics; never infer a derived metric from Wiki links."""
    asset_ids = sorted({aid for aid, _ in nodes})
    if not asset_ids:
        return []
    impacts = []
    for row in con.execute('''SELECT m.* FROM catalog_metrics m JOIN metric_lineage_revisions r
        ON r.metric_id=m.id AND r.metric_version=m.catalog_version
        WHERE r.asset_id IN (SELECT value FROM json_each(?)) ORDER BY m.display_name,m.id LIMIT 250''',
                           (canonical(asset_ids),)):
        current = _current(con, user_id, row)
        if not current:
            continue
        _, asset, deps = current
        matching = [d for d in deps if (asset['id'], d['column'] if via_column else '') in nodes
                    and (via_column or d['role'] == 'dataset') and (not via_column or d['column'])]
        if matching:
            impacts.append(dict(metric_id=row['id'], display_name=row['display_name'], metric_version=row['catalog_version'],
                                asset_id=asset['id'], table=asset['physical_name'], namespace=asset['namespace'], dependencies=matching))
    return impacts
