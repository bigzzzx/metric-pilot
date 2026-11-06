"""Draft ingestion and controlled catalog publication workflow."""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException

from backend.catalog.service import MANAGE, can_access, is_platform_admin
from backend.catalog.metadata_semantics import column_semantics, table_semantics
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
    asset_ids: dict[tuple[str, str], str] = {}
    protected_ids: set[str] = set()
    for item in discovery.get("tables", []):
        namespace = str(item.get("namespace") or "").strip()
        name = str(item.get("name") or "").strip()
        if not name: continue
        asset = con.execute(
            "SELECT id,catalog_version,publish_status FROM catalog_assets WHERE source_id=? AND namespace=? AND physical_name=?",
            (source_id,namespace,name),
        ).fetchone()
        asset_id = asset["id"] if asset else str(uuid.uuid5(uuid.NAMESPACE_URL, f"data-agent:{source_id}:{namespace}:{name}"))
        asset_ids[(namespace, name)] = asset_id
        # Discovery cannot overwrite approved or manually reviewed metadata.
        reviewed = asset and con.execute("SELECT 1 FROM catalog_audit_logs WHERE entity_id=? AND action='review_saved' LIMIT 1", (asset_id,)).fetchone()
        if asset and (asset["publish_status"] in {"published", "rejected"} or reviewed):
            protected_ids.add(asset_id)
            imported += 1
            continue
        version = int(asset["catalog_version"] or 0) + 1 if asset else 1
        description = str(item.get("description") or "").strip() or "数据源未提供表说明。"
        semantics = table_semantics(description, namespace)
        domain_id = semantics["domain_id"]
        primary_key = ", ".join(str(value) for value in item.get("primary_key", []) if value)
        warehouse_layer = semantics["warehouse_layer"]
        normalized_stage = {
            "ODS": "raw", "DWD": "detail", "DWS": "summary", "ADS": "application", "DIM": "dimension",
        }.get(warehouse_layer, "unknown")
        con.execute("""INSERT INTO catalog_assets(
          id,source_id,namespace,physical_name,display_name,asset_type,domain_id,warehouse_layer,
          normalized_stage,description,aliases_json,data_grain,primary_key,owner_group_id,sensitivity,
          publish_status,certification_status,catalog_version,source_updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET namespace=excluded.namespace,description=excluded.description,
          aliases_json=excluded.aliases_json,data_grain=excluded.data_grain,domain_id=excluded.domain_id,
          owner_group_id=excluded.owner_group_id,sensitivity=excluded.sensitivity,
          primary_key=excluded.primary_key,warehouse_layer=excluded.warehouse_layer,
          normalized_stage=excluded.normalized_stage,
          publish_status=CASE WHEN catalog_assets.publish_status='published' THEN 'published' ELSE 'draft' END,
          catalog_version=excluded.catalog_version,
          source_updated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP""",
        (asset_id,source_id,namespace,name,name,"table",domain_id,warehouse_layer,normalized_stage,description,
         json.dumps(semantics["aliases"], ensure_ascii=False),semantics["data_grain"],primary_key,
         semantics["owner_group_id"],semantics["sensitivity"],"draft","draft",version))
        asset_ids[(namespace, name)] = asset_id
        con.execute("DELETE FROM catalog_columns WHERE asset_id=?", (asset_id,))
        for ordinal, column in enumerate(item.get("columns", []), 1):
            col_name = str(column.get("name") or "").strip()
            if not col_name: continue
            column_description = str(column.get("description") or "").strip() or "数据源未提供字段说明。"
            is_primary_key = bool(column.get("is_primary_key"))
            column_type = str(column.get("type") or "TEXT")
            column_metadata = column_semantics(column_description, col_name, column_type, is_primary_key)
            con.execute("""INSERT INTO catalog_columns(
              id,asset_id,physical_name,display_name,data_type,ordinal,description,aliases_json,semantic_role,
              nullable,is_primary_key,sensitivity,classification,masking_strategy)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                str(uuid.uuid4()),asset_id,col_name,column_metadata["display_name"],column_type,
                int(column.get("ordinal") or ordinal),column_description,
                json.dumps(column_metadata["aliases"], ensure_ascii=False),column_metadata["semantic_role"],
                int(bool(column.get("nullable", True))),int(is_primary_key),column_metadata["sensitivity"],
                column_metadata["classification"],column_metadata["masking_strategy"],
            ))
        con.execute("INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)", (actor_id,"sync_draft","asset",asset_id,json.dumps({"source_id":source_id,"namespace":namespace,"table":name},ensure_ascii=False)))
        imported += 1
    current_asset_ids = [asset_id for asset_id in asset_ids.values() if asset_id not in protected_ids]
    if current_asset_ids:
        holders = ",".join("?" for _ in current_asset_ids)
        protected = list(protected_ids)
        guard = ""
        if protected:
            protected_holders = ",".join("?" for _ in protected)
            guard = f" AND from_asset_id NOT IN ({protected_holders}) AND to_asset_id NOT IN ({protected_holders})"
        con.execute(
            f"DELETE FROM catalog_relationships WHERE (from_asset_id IN ({holders}) OR to_asset_id IN ({holders}))" + guard,
            [*current_asset_ids, *current_asset_ids, *protected, *protected],
        )
    relationships_imported = 0
    for relation in discovery.get("relationships", []):
        from_key = (str(relation.get("from_namespace") or ""), str(relation.get("from_table") or ""))
        to_key = (str(relation.get("to_namespace") or ""), str(relation.get("to_table") or ""))
        from_asset_id, to_asset_id = asset_ids.get(from_key), asset_ids.get(to_key)
        from_column, to_column = str(relation.get("from_column") or ""), str(relation.get("to_column") or "")
        if not from_asset_id or not to_asset_id or not from_column or not to_column or from_asset_id in protected_ids or to_asset_id in protected_ids:
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
        "preserved": len(protected_ids),
        "message": "已将表、字段、注释和主外键关系采集为目录草稿；审核发布后进入 RAG。",
    }


def _publish_in_transaction(con, asset_id: str, actor_id: str, expected_version: int | None, batch_id: str | None = None) -> dict[str, Any]:
    """Validate first, then publish one asset inside the caller's transaction."""
    asset = con.execute("SELECT * FROM catalog_assets WHERE id=?", (asset_id,)).fetchone()
    if not asset:
        raise LookupError("Asset not found")
    if expected_version is not None and asset["catalog_version"] != expected_version:
        raise HTTPException(409, "资产已被更新，请刷新后重新审核。")
    if asset["publish_status"] != "draft":
        raise HTTPException(409, "仅待审核草稿可以发布；退回资产请先编辑保存。")
    errors = []
    if not asset["description"] or asset["description"].startswith(("技术元数据待", "数据源未提供")): errors.append("资产说明")
    if not asset["data_grain"]: errors.append("数据粒度")
    columns = con.execute("SELECT physical_name,description FROM catalog_columns WHERE asset_id=?", (asset_id,)).fetchall()
    if not columns: errors.append("字段")
    if any(not item["description"] or item["description"].startswith(("待补充", "数据源未提供")) for item in columns): errors.append("字段说明")
    if errors:
        raise HTTPException(422, "无法发布；仍需补充：" + "、".join(errors))
    next_version = int(asset["catalog_version"] or 0) + 1
    con.execute("UPDATE catalog_assets SET publish_status='published',certification_status=CASE WHEN certification_status='draft' THEN 'validated' ELSE certification_status END,published_at=CURRENT_TIMESTAMP,catalog_version=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (next_version,asset_id))
    con.execute("""UPDATE catalog_relationships SET status='published'
        WHERE (from_asset_id=? OR to_asset_id=?)
        AND from_asset_id IN (SELECT id FROM catalog_assets WHERE publish_status='published')
        AND to_asset_id IN (SELECT id FROM catalog_assets WHERE publish_status='published')""", (asset_id, asset_id))
    con.execute("INSERT INTO catalog_publications(id,entity_type,entity_id,version,status,published_by,summary) VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()),"asset",asset_id,next_version,"published",actor_id,"验证资产语义后发布至目录与 RAG。"))
    con.execute(
        "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
        (actor_id,"publish","asset",asset_id,json.dumps({"batch_id": batch_id} if batch_id else {}, ensure_ascii=False)),
    )
    from backend.index_worker import enqueue_catalog
    index_job_id = enqueue_catalog(con)
    return {"asset_id": asset_id, "status": "published", "version": next_version,"index_job_id":index_job_id}


def publish_asset(asset_id: str, actor_id: str, expected_version: int | None = None) -> dict[str, Any]:
    """Validate the minimal semantic contract then make one asset retrievable."""
    require_catalog_admin(actor_id)
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        result = _publish_in_transaction(con, asset_id, actor_id, expected_version)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    # The source of truth is committed first.  An unavailable vector service
    # never rolls back governance publication: lexical retrieval remains safe
    # and the idempotent publisher retries on the next catalog query.
    from backend.catalog.vector_index import ensure_catalog_index
    indexed = ensure_catalog_index()
    return {**result,
        "rag_indexed": indexed,
        "message": "资产已发布并进入 RAG。" if indexed else "资产已发布；向量索引暂不可用，后台将自动重试。",
    }


def publish_assets(items: list[dict[str, Any]], actor_id: str) -> dict[str, Any]:
    """Partially publish valid drafts in one transaction and refresh RAG once."""
    require_catalog_admin(actor_id)
    batch_id = str(uuid.uuid4())
    results: list[dict[str, Any]] = []
    published = 0
    seen: set[str] = set()
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        for item in items:
            asset_id = str(item.get("asset_id") or "")
            if asset_id in seen:
                results.append({"asset_id": asset_id, "status": "failed", "status_code": 422, "message": "同一资产不能重复提交。"})
                continue
            seen.add(asset_id)
            try:
                result = _publish_in_transaction(con, asset_id, actor_id, item.get("version"), batch_id)
                results.append({**result, "message": "资产已发布。"})
                published += 1
            except LookupError:
                results.append({"asset_id": asset_id, "status": "failed", "status_code": 404, "message": "资产不存在。"})
            except HTTPException as exc:
                results.append({"asset_id": asset_id, "status": "failed", "status_code": exc.status_code, "message": str(exc.detail)})
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    indexed = None
    if published:
        from backend.catalog.vector_index import ensure_catalog_index
        indexed = ensure_catalog_index()
    return {
        "batch_id": batch_id,
        "published_count": published,
        "failed_count": len(results) - published,
        "rag_indexed": indexed,
        "results": results,
        "message": f"批量发布完成：成功 {published} 个，失败 {len(results) - published} 个。",
    }
