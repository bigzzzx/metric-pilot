"""Catalog review contracts and transactional draft/lifecycle operations."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.catalog.publications import require_catalog_admin
from backend.core import db


class ReviewColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=10000)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    semantic_role: str = Field(default="", max_length=100)
    sensitivity: Literal["public", "internal", "sensitive", "restricted"] = "internal"
    masking_strategy: Literal["none", "partial", "redact"] = "none"


class AssetReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=10000)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    domain_id: str
    warehouse_layer: Literal["ODS", "DWD", "DIM", "DWS", "ADS", "UNKNOWN"]
    data_grain: str = Field(max_length=2000)
    sensitivity: Literal["public", "internal", "sensitive", "restricted"]
    columns: list[ReviewColumn] = Field(min_length=1, max_length=2000)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)


def checked_asset(con, asset_id: str, version: int):
    asset = con.execute("SELECT * FROM catalog_assets WHERE id=?", (asset_id,)).fetchone()
    if not asset:
        raise LookupError("Asset not found")
    if asset["catalog_version"] != version:
        raise HTTPException(409, "资产已被更新，请刷新后重新审核。")
    return asset


def save_review(asset_id: str, actor_id: str, review: AssetReview) -> dict:
    require_catalog_admin(actor_id)
    con = db()
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            asset = checked_asset(con, asset_id, review.version)
            if asset["publish_status"] == "published":
                raise HTTPException(409, "请先撤回发布，再编辑元数据。")
            if not review.display_name.strip():
                raise HTTPException(422, "业务名称不能为空。")
            if not con.execute("SELECT 1 FROM catalog_domains WHERE id=? AND active=1", (review.domain_id,)).fetchone():
                raise HTTPException(422, "请选择有效业务域。")
            existing = {row["id"] for row in con.execute("SELECT id FROM catalog_columns WHERE asset_id=?", (asset_id,))}
            supplied = [column.id for column in review.columns]
            if set(supplied) != existing or len(supplied) != len(existing):
                raise HTTPException(409, "字段列表已变化或包含无效字段，请刷新后重试。")
            stage = {"ODS": "raw", "DWD": "detail", "DIM": "dimension", "DWS": "summary", "ADS": "application"}.get(review.warehouse_layer, "unknown")
            con.execute("""UPDATE catalog_assets SET display_name=?,description=?,aliases_json=?,
                domain_id=?,warehouse_layer=?,normalized_stage=?,data_grain=?,sensitivity=?,
                publish_status='draft',catalog_version=catalog_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (review.display_name.strip(), review.description.strip(), json.dumps(review.aliases, ensure_ascii=False),
                 review.domain_id, review.warehouse_layer, stage, review.data_grain.strip(), review.sensitivity, asset_id))
            for column in review.columns:
                con.execute("""UPDATE catalog_columns SET display_name=?,description=?,aliases_json=?,
                    semantic_role=?,sensitivity=?,masking_strategy=? WHERE id=? AND asset_id=?""",
                    (column.display_name.strip(), column.description.strip(), json.dumps(column.aliases, ensure_ascii=False),
                     column.semantic_role, column.sensitivity, column.masking_strategy, column.id, asset_id))
            con.execute("INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id) VALUES (?,'review_saved','asset',?)", (actor_id, asset_id))
        return {"asset_id": asset_id, "version": review.version + 1, "message": "草稿已保存，可继续审核发布。"}
    finally:
        con.close()


def change_status(asset_id: str, actor_id: str, decision: ReviewDecision, *, withdraw: bool = False) -> dict:
    require_catalog_admin(actor_id)
    if not decision.reason.strip():
        raise HTTPException(422, "请填写操作原因。")
    con = db()
    status = "draft" if withdraw else "rejected"
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            asset = checked_asset(con, asset_id, decision.version)
            if (asset["publish_status"] == "published") != withdraw:
                raise HTTPException(409, "资产状态已变化，请刷新后重试。")
            con.execute("UPDATE catalog_assets SET publish_status=?,published_at=NULL,catalog_version=catalog_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, asset_id))
            con.execute("UPDATE catalog_relationships SET status='draft' WHERE from_asset_id=? OR to_asset_id=?", (asset_id, asset_id))
            con.execute("INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
                        (actor_id, "withdraw" if withdraw else "reject", "asset", asset_id, json.dumps({"reason": decision.reason.strip()}, ensure_ascii=False)))
        indexed = None
        if withdraw:
            from backend.catalog.vector_index import ensure_catalog_index
            indexed = ensure_catalog_index()
        message = "已退回，修改保存后可重新审核。" if not withdraw else "已撤回发布，普通用户及 RAG 已不可检索该资产。"
        if withdraw and not indexed:
            message += " 向量清理暂未完成，下次检索时重试；权限过滤立即生效。"
        return {"asset_id": asset_id, "status": status, "rag_indexed": indexed, "message": message}
    finally:
        con.close()
