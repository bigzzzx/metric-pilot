"""Draft ingestion and controlled catalog publication workflow."""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException

from backend.catalog.service import MANAGE, can_access, is_platform_admin
from backend.core import db


def require_catalog_admin(user_id: str) -> None:
    if not can_access(user_id, MANAGE):
        raise HTTPException(403, "Catalog management permission is required")


def require_platform_admin(user_id: str) -> None:
    if not is_platform_admin(user_id):
        raise HTTPException(403, "Platform administrator permission is required")


def import_discovery_as_draft(source_id: str, discovery: dict[str, Any], actor_id: str) -> dict[str, Any]:
    """Store discovered metadata and physical relationships as governed drafts."""
    require_catalog_admin(actor_id)
    con = db(); imported = 0
    domain = con.execute("SELECT id FROM catalog_domains WHERE id='domain-ecommerce'").fetchone()
    domain_id = domain["id"] if domain else "domain-ecommerce"
    asset_ids: dict[tuple[str, str], str] = {}
    for item in discovery.get("tables", []):
        namespace = str(item.get("namespace") or "").strip()
        name = str(item.get("name") or "").strip()
        if not name: continue
        asset = con.execute(
            "SELECT id,catalog_version FROM catalog_assets WHERE source_id=? AND namespace=? AND physical_name=?",
            (source_id,namespace,name),
        ).fetchone()
        asset_id = asset["id"] if asset else str(uuid.uuid5(uuid.NAMESPACE_URL, f"data-agent:{source_id}:{namespace}:{name}"))
        version = int(asset["catalog_version"] or 0) + 1 if asset else 1
        description = str(item.get("description") or "").strip() or "数据源未提供表说明。"
        primary_key = ", ".join(str(value) for value in item.get("primary_key", []) if value)
        warehouse_layer = namespace.upper() if namespace.upper() in {"ODS", "DWD", "DWS", "ADS", "DIM"} else "UNKNOWN"
        normalized_stage = {
            "ODS": "raw", "DWD": "detail", "DWS": "summary", "ADS": "application", "DIM": "dimension",
        }.get(warehouse_layer, "unknown")
        con.execute("""INSERT INTO catalog_assets(
          id,source_id,namespace,physical_name,display_name,asset_type,domain_id,warehouse_layer,
          normalized_stage,description,primary_key,publish_status,certification_status,catalog_version,source_updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET namespace=excluded.namespace,description=excluded.description,
          primary_key=excluded.primary_key,warehouse_layer=excluded.warehouse_layer,
          normalized_stage=excluded.normalized_stage,
          publish_status=CASE WHEN catalog_assets.publish_status='published' THEN 'published' ELSE 'draft' END,
          catalog_version=excluded.catalog_version,
          source_updated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP""",
        (asset_id,source_id,namespace,name,name,"table",domain_id,warehouse_layer,normalized_stage,description,primary_key,"draft","draft",version))
        asset_ids[(namespace, name)] = asset_id
        con.execute("DELETE FROM catalog_columns WHERE asset_id=?", (asset_id,))
        for ordinal, column in enumerate(item.get("columns", []), 1):
            col_name = str(column.get("name") or "").strip()
            if not col_name: continue
            column_description = str(column.get("description") or "").strip() or "数据源未提供字段说明。"
            con.execute("""INSERT INTO catalog_columns(
              id,asset_id,physical_name,display_name,data_type,ordinal,description,nullable,is_primary_key,sensitivity)
            VALUES (?,?,?,?,?,?,?,?,?,'internal')""", (
                str(uuid.uuid4()),asset_id,col_name,col_name,str(column.get("type") or "TEXT"),
                int(column.get("ordinal") or ordinal),column_description,int(bool(column.get("nullable", True))),
                int(bool(column.get("is_primary_key"))),
            ))
        con.execute("INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)", (actor_id,"sync_draft","asset",asset_id,json.dumps({"source_id":source_id,"namespace":namespace,"table":name},ensure_ascii=False)))
        imported += 1
    current_asset_ids = list(asset_ids.values())
    if current_asset_ids:
        holders = ",".join("?" for _ in current_asset_ids)
        con.execute(
            f"DELETE FROM catalog_relationships WHERE from_asset_id IN ({holders}) OR to_asset_id IN ({holders})",
            [*current_asset_ids, *current_asset_ids],
        )
    relationships_imported = 0
    for relation in discovery.get("relationships", []):
        from_key = (str(relation.get("from_namespace") or ""), str(relation.get("from_table") or ""))
        to_key = (str(relation.get("to_namespace") or ""), str(relation.get("to_table") or ""))
        from_asset_id, to_asset_id = asset_ids.get(from_key), asset_ids.get(to_key)
        from_column, to_column = str(relation.get("from_column") or ""), str(relation.get("to_column") or "")
        if not from_asset_id or not to_asset_id or not from_column or not to_column:
            continue
        relation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"data-agent-relation:{source_id}:{from_key}:{from_column}:{to_key}:{to_column}"))
        join_condition = f"{from_key[0]}.{from_key[1]}.{from_column} = {to_key[0]}.{to_key[1]}.{to_column}"
        con.execute("""INSERT INTO catalog_relationships(
          id,from_asset_id,from_column,to_asset_id,to_column,relation_type,cardinality,
          recommended_join,join_condition,description,verified,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft')""", (
            relation_id,from_asset_id,from_column,to_asset_id,to_column,
            str(relation.get("relation") or "foreign_key"),str(relation.get("cardinality") or "many_to_one"),
            str(relation.get("recommended_join") or "LEFT JOIN"),join_condition,
            str(relation.get("description") or "从数据源主外键约束自动采集。"),int(bool(relation.get("verified"))),
        ))
        relationships_imported += 1
    con.commit(); con.close()
    return {
        "imported": imported,
        "relationships_imported": relationships_imported,
        "status": "draft",
        "message": "已将表、字段、注释和主外键关系采集为目录草稿；审核发布后进入 RAG。",
    }


def publish_asset(asset_id: str, actor_id: str) -> dict[str, Any]:
    """Validate the minimal semantic contract then make one asset retrievable."""
    require_catalog_admin(actor_id)
    con = db(); asset = con.execute("SELECT * FROM catalog_assets WHERE id=?", (asset_id,)).fetchone()
    if not asset: con.close(); raise LookupError("Asset not found")
    errors = []
    if not asset["description"] or asset["description"].startswith(("技术元数据待", "数据源未提供")): errors.append("资产说明")
    if not asset["data_grain"]: errors.append("数据粒度")
    columns = con.execute("SELECT physical_name,description FROM catalog_columns WHERE asset_id=?", (asset_id,)).fetchall()
    if not columns: errors.append("字段")
    if any(not item["description"] or item["description"].startswith(("待补充", "数据源未提供")) for item in columns): errors.append("字段说明")
    if errors:
        con.close(); raise HTTPException(422, "无法发布；仍需补充：" + "、".join(errors))
    next_version = int(asset["catalog_version"] or 0) + 1
    con.execute("UPDATE catalog_assets SET publish_status='published',published_at=CURRENT_TIMESTAMP,catalog_version=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (next_version,asset_id))
    con.execute("INSERT INTO catalog_publications(id,entity_type,entity_id,version,status,published_by,summary) VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()),"asset",asset_id,next_version,"published",actor_id,"验证资产语义后发布至目录与 RAG。"))
    con.execute("INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id) VALUES (?,?,?,?)", (actor_id,"publish","asset",asset_id))
    con.commit(); con.close()
    # The source of truth is committed first.  An unavailable vector service
    # never rolls back governance publication: lexical retrieval remains safe
    # and the idempotent publisher retries on the next catalog query.
    from backend.catalog.vector_index import ensure_catalog_index
    indexed = ensure_catalog_index()
    return {
        "asset_id": asset_id, "status": "published", "version": next_version,
        "rag_indexed": indexed,
        "message": "资产已发布并进入 RAG。" if indexed else "资产已发布；向量索引暂不可用，将在下次检索时重试。",
    }
