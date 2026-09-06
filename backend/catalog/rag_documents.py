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
              domain_id: str | None = None) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": kind,
        "title": title,
        "content": content,
        "keywords": [keyword for keyword in keywords if keyword],
        "asset_ids": asset_ids or [],
        "domain_id": domain_id,
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
                ))
            documents.append(_document(
                identifier=f"catalog:asset:{asset_id}", kind="table",
                title=f"数据资产：{asset['display_name']}（{asset['physical_name']}）",
                content=(f"数据资产：{asset['display_name']}。物理表：{asset['physical_name']}。"
                         f"业务域：{asset['domain_id']}。数仓层：{asset['warehouse_layer']}。"
                         f"标准层级：{asset['normalized_stage']}。描述：{asset['description']}。"
                         f"别名：{'、'.join(aliases) or '无'}。数据粒度：{asset['data_grain']}。"
                         f"主键：{asset['primary_key']}。可用于 Agent 的字段：\n" + "\n".join(field_lines)),
                keywords=[asset["physical_name"], asset["display_name"], *aliases],
                asset_ids=[asset_id], domain_id=asset["domain_id"],
            ))

        for relation in con.execute("SELECT * FROM catalog_relationships WHERE status='published' ORDER BY id"):
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
                         f"说明：{relation['description']}。已验证：{'是' if relation['verified'] else '否'}。"),
                keywords=[left["physical_name"], relation["from_column"], right["physical_name"], relation["to_column"], "关联", "join"],
                asset_ids=[str(left["id"]), str(right["id"])], domain_id=left["domain_id"],
            ))

        for metric in con.execute("SELECT * FROM catalog_metrics WHERE publish_status='published' ORDER BY id"):
            documents.append(_document(
                identifier=f"catalog:metric:{metric['id']}", kind="metric",
                title=f"指标：{metric['display_name']}",
                content=(f"指标：{metric['display_name']}。定义：{metric['description']}。"
                         f"公式：{metric['formula']}。SQL 口径：{metric['definition_sql']}。"
                         f"粒度：{metric['grain']}。认证状态：{metric['certification_status']}。"),
                keywords=[metric["name"], metric["display_name"], "指标", "口径"], domain_id=metric["domain_id"],
            ))

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
