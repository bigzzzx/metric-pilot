"""Render governed data-catalog records into small, searchable RAG documents."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from backend.catalog.service import DISCOVER, can_access
from backend.core import db

_HIDDEN_FROM_AGENT = {"sensitive", "restricted"}


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def _document(*, identifier: str, kind: str, title: str, content: str,
              keywords: list[str], asset_ids: list[str] | None = None,
              domain_id: str | None = None, source_id: str | None = None,
              source_kind: str | None = None, namespace: str | None = None,
              physical_name: str | None = None) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": kind,
        "title": title,
        "content": content,
        "keywords": [keyword for keyword in keywords if keyword],
        "asset_ids": asset_ids or [],
        "domain_id": domain_id,
        "source_id": source_id,
        "source_kind": source_kind,
        "namespace": namespace,
        "physical_name": physical_name,
    }


def published_catalog_documents(connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Build the full published catalog projection without granting access.

    SQLite remains the source of truth. This projection intentionally omits
    sensitive and restricted columns from Agent-visible text.
    """
    own_connection = connection is None
    con = connection or db()
    documents: list[dict[str, Any]] = []
    try:
        assets = list(con.execute("SELECT * FROM catalog_assets WHERE publish_status='published' ORDER BY id"))
        assets_by_id = {str(asset["id"]): asset for asset in assets}
        for asset in assets:
            asset_id = str(asset["id"])
            source = con.execute("SELECT name,kind FROM data_sources WHERE id=?", (asset['source_id'],)).fetchone()
            remote = asset['source_id'] != 'local-ecommerce-demo'
            source_name = source['name'] if source else asset['source_id']
            source_kind = source['kind'] if source else 'unknown'
            aliases = _json(asset["aliases_json"], [])
            columns = list(con.execute(
                "SELECT * FROM catalog_columns WHERE asset_id=? ORDER BY ordinal", (asset_id,)
            ))
            visible_columns = [column for column in columns if column["sensitivity"] not in _HIDDEN_FROM_AGENT]
            field_lines = []
            for column in visible_columns:
                column_aliases = _json(column["aliases_json"], [])
                field_lines.append(
                    f"- {column['physical_name']} ({column['data_type']})：{column['description']}；"
                    f"语义角色：{column['semantic_role']}；别名：{'、'.join(column_aliases) or '无'}"
                )
                documents.append(_document(
                    identifier=f"catalog:column:{column['id']}", kind="column",
                    title=f"字段：{asset['physical_name']}.{column['physical_name']}",
                    content=(f"所属数据资产：{asset['display_name']}（{asset['physical_name']}）。"
                             f"字段：{column['physical_name']}，类型：{column['data_type']}。"
                             f"说明：{column['description']}。语义角色：{column['semantic_role']}。"
                             f"别名：{'、'.join(column_aliases) or '无'}。"),
                    keywords=[asset["physical_name"], asset["display_name"], column["physical_name"], *column_aliases],
                    asset_ids=[asset_id], domain_id=asset["domain_id"],
                    source_id=asset["source_id"], source_kind=source_kind,
                    namespace=asset["namespace"], physical_name=column["physical_name"],
                ))
            documents.append(_document(
                identifier=f"catalog:asset:{asset_id}", kind="table",
                title=f"数据资产：{asset['display_name']}（{asset['physical_name']}）",
                content=(f"数据资产：{asset['display_name']}。物理表：{asset['physical_name']}。"
                         + (f"数据源：{source_name}。数据库类型：{source_kind}。数据源 ID：{asset['source_id']}。完整表名：{asset['namespace']}.{asset['physical_name']}。" if remote else '') +
                         f"业务域：{asset['domain_id']}。数仓层：{asset['warehouse_layer']}。"
                         f"标准层级：{asset['normalized_stage']}。描述：{asset['description']}。"
                         f"别名：{'、'.join(aliases) or '无'}。数据粒度：{asset['data_grain']}。"
                         f"主键：{asset['primary_key']}。可用于 Agent 的字段：\n" + "\n".join(field_lines)),
                keywords=[asset["physical_name"], asset["display_name"], *aliases, *([source_name, source_kind, f"{asset['namespace']}.{asset['physical_name']}"] if remote else [])],
                asset_ids=[asset_id], domain_id=asset["domain_id"],
                source_id=asset["source_id"], source_kind=source_kind,
                namespace=asset["namespace"], physical_name=asset["physical_name"],
            ))

        for relation in con.execute("SELECT * FROM catalog_relationships WHERE status='published' ORDER BY id"):
            from backend.lineage.projection import legacy_relation_provenance
            provenance = legacy_relation_provenance(relation)
            left, right = assets_by_id.get(str(relation["from_asset_id"])), assets_by_id.get(str(relation["to_asset_id"]))
            if not left or not right:
                continue
            hidden = con.execute(
                "SELECT 1 FROM catalog_columns WHERE ((asset_id=? AND physical_name=?) OR (asset_id=? AND physical_name=?)) "
                "AND sensitivity IN ('sensitive','restricted') LIMIT 1",
                (left["id"], relation["from_column"], right["id"], relation["to_column"]),
            ).fetchone()
            if hidden:
                continue
            documents.append(_document(
                identifier=f"catalog:relation:{relation['id']}", kind="relation",
                title=f"关联：{left['physical_name']} 与 {right['physical_name']}",
                content=(f"关联条件：{relation['join_condition']}。关系类型：{relation['relation_type']}，"
                         f"基数：{relation['cardinality']}，推荐 {relation['recommended_join']}。"
                         f"说明：{relation['description']}。来源：{provenance}；演示及历史声明不代表当前业务已批准。"),
                keywords=[left["physical_name"], relation["from_column"], right["physical_name"], relation["to_column"], "关联", "join"],
                asset_ids=[str(left["id"]), str(right["id"])], domain_id=left["domain_id"],
                source_id=left["source_id"] if left["source_id"] == right["source_id"] else None,
                source_kind=(con.execute("SELECT kind FROM data_sources WHERE id=?", (left["source_id"],)).fetchone() or {"kind": None})["kind"]
                if left["source_id"] == right["source_id"] else None,
            ))

        for metric in con.execute("SELECT * FROM catalog_metrics WHERE publish_status='published' ORDER BY id"):
            from backend.semantic_metrics.storage import metric_document, wiki_document
            documents.append(metric_document(metric))
            wiki = wiki_document(metric)
            if wiki:
                documents.append(wiki)

        from backend.lineage.projection import rag_documents as lineage_documents
        documents.extend(lineage_documents(con))

        for term in con.execute("SELECT * FROM catalog_terms WHERE publish_status='published' ORDER BY id"):
            aliases = _json(term["aliases_json"], [])
            documents.append(_document(
                identifier=f"catalog:term:{term['id']}", kind="term",
                title=f"业务术语：{term['display_name']}",
                content=f"业务术语：{term['display_name']}。定义：{term['definition']}。别名：{'、'.join(aliases) or '无'}。",
                keywords=[term["term"], term["display_name"], *aliases], domain_id=term["domain_id"],
            ))
        return documents
    finally:
        if own_connection:
            con.close()


def is_document_authorized(document: dict[str, Any], user_id: str, connection: sqlite3.Connection) -> bool:
    """Require access to every asset referenced by a RAG document."""
    if document.get('lineage_candidate_id'):
        from backend.lineage.projection import document_visible
        if not document_visible(connection,document,user_id): return False
    if document.get('kind')=='metric_wiki':
        from backend.semantic_metrics.wiki import visible_wiki
        row=connection.execute('SELECT * FROM catalog_metrics WHERE id=?',(document['metric_id'],)).fetchone()
        if not row or not visible_wiki(connection,row,user_id): return False
    domain=document.get('domain_id')
    if domain and not can_access(user_id,DISCOVER,domain_id=domain,connection=connection): return False
    asset_ids = document.get("asset_ids") or []
    if asset_ids:
        return all(can_access(user_id, DISCOVER, asset_id=asset_id, connection=connection) for asset_id in asset_ids)
    domain_id = document.get("domain_id")
    return bool(domain_id and can_access(user_id, DISCOVER, domain_id=domain_id, connection=connection))


def authorized_catalog_documents(user_id: str, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Return only published metadata that may enter this user's model prompt."""
    if not user_id:
        return []
    own_connection = connection is None
    con = connection or db()
    try:
        return [document for document in published_catalog_documents(con) if is_document_authorized(document, user_id, con)]
    finally:
        if own_connection:
            con.close()
