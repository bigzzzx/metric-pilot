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
    """Store connector discoveries as technical-metadata drafts, never as RAG evidence."""
    require_catalog_admin(actor_id)
    con = db(); imported = 0
    domain = con.execute("SELECT id FROM catalog_domains WHERE id='domain-ecommerce'").fetchone()
    domain_id = domain["id"] if domain else "domain-ecommerce"
    for item in discovery.get("tables", []):
        name = str(item.get("name") or "").strip()
        if not name: continue
        asset = con.execute("SELECT id,catalog_version FROM catalog_assets WHERE source_id=? AND physical_name=?", (source_id,name)).fetchone()
        asset_id = asset["id"] if asset else f"asset-{source_id}-{name}".replace("_", "-")
        version = int(asset["catalog_version"] or 0) + 1 if asset else 1
        con.execute("""INSERT INTO catalog_assets(id,source_id,physical_name,display_name,asset_type,domain_id,warehouse_layer,normalized_stage,description,publish_status,certification_status,catalog_version,source_updated_at)
        VALUES (?,?,?,?,? ,?,'UNKNOWN','unknown','技术元数据待补充语义。','draft','draft',?,CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET description=excluded.description,publish_status='draft',catalog_version=excluded.catalog_version,source_updated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP""", (asset_id,source_id,name,name,"table",domain_id,version))
        con.execute("DELETE FROM catalog_columns WHERE asset_id=?", (asset_id,))
        for ordinal, column in enumerate(item.get("columns", []), 1):
            col_name = str(column.get("name") or "").strip()
            if not col_name: continue
            con.execute("""INSERT INTO catalog_columns(id,asset_id,physical_name,display_name,data_type,ordinal,description,is_primary_key,sensitivity)
            VALUES (?,?,?,?,?,?,?,?,'internal')""", (str(uuid.uuid4()),asset_id,col_name,col_name,str(column.get("type") or "TEXT"),ordinal,"待补充字段说明。",int(bool(column.get("is_primary_key")))))
        con.execute("INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)", (actor_id,"sync_draft","asset",asset_id,json.dumps({"source_id":source_id,"table":name},ensure_ascii=False)))
        imported += 1
    con.commit(); con.close()
    return {"imported": imported, "status": "draft", "message": "已采集技术元数据为草稿；补充语义、权限与质量信息并发布后才可被 RAG 检索。"}


def publish_asset(asset_id: str, actor_id: str) -> dict[str, Any]:
    """Validate the minimal semantic contract then make one asset retrievable."""
    require_catalog_admin(actor_id)
    con = db(); asset = con.execute("SELECT * FROM catalog_assets WHERE id=?", (asset_id,)).fetchone()
    if not asset: con.close(); raise LookupError("Asset not found")
    errors = []
    if not asset["description"] or asset["description"].startswith("技术元数据待"): errors.append("资产说明")
    if not asset["data_grain"]: errors.append("数据粒度")
    columns = con.execute("SELECT physical_name,description FROM catalog_columns WHERE asset_id=?", (asset_id,)).fetchall()
    if not columns: errors.append("字段")
    if any(not item["description"] or item["description"].startswith("待补充") for item in columns): errors.append("字段说明")
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
