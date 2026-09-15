"""Authenticated HTTP routes for the governed data catalog."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.auth import require_user
from backend.catalog.publications import publish_asset, publish_assets, require_catalog_admin
from backend.catalog.retrieval import catalog_overview, lineage_for_asset
from backend.catalog.service import get_asset, list_assets, list_domains, list_metrics, list_terms, sample_asset
from backend.catalog.review import AssetReview, ReviewDecision, save_review, change_status

router = APIRouter(prefix="/api/catalog", tags=["catalog"])
from backend.semantic_metrics.wiki import MetricReview, generate_draft, publish


@router.post('/admin/metrics/{metric_id}/wiki-draft')
async def metric_wiki_draft(metric_id: str, user: dict = Depends(require_user)):
    return await generate_draft(metric_id,user['id'])


@router.put('/admin/metrics/{metric_id}/publish')
def publish_metric(metric_id: str, payload: MetricReview, user: dict = Depends(require_user)):
    return publish(metric_id,user['id'],payload)


class BatchPublishItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=0)


class BatchPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BatchPublishItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_assets(self):
        ids = [item.asset_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("同一资产不能重复提交")
        return self


@router.get("/overview")
def overview(user: dict = Depends(require_user)): return catalog_overview(user["id"])


@router.get("/domains")
def domains(user: dict = Depends(require_user)): return list_domains(user["id"])


@router.get("/assets")
def assets(q: str = "", domain_id: str | None = None, layer: str | None = None, source_id: str | None = None, sensitivity: str | None = None, user: dict = Depends(require_user)):
    return list_assets(user["id"],query=q,domain_id=domain_id,layer=layer,source_id=source_id,sensitivity=sensitivity)


@router.get("/assets/{asset_id}")
def asset(asset_id: str,user: dict = Depends(require_user)):
    try: return get_asset(user["id"],asset_id)
    except LookupError as exc: raise HTTPException(404,"Data asset not found or not authorized") from exc


@router.get("/assets/{asset_id}/sample")
def asset_sample(asset_id: str,limit: int = Query(default=5,ge=1,le=5),user: dict = Depends(require_user)):
    try: return sample_asset(user["id"],asset_id,limit)
    except LookupError as exc: raise HTTPException(404,"Sample data not found or not authorized") from exc


@router.get("/assets/{asset_id}/lineage")
def asset_lineage(asset_id: str,hops: int = Query(default=2,ge=1,le=3),user: dict = Depends(require_user)):
    try: return lineage_for_asset(user["id"],asset_id,hops)
    except LookupError as exc: raise HTTPException(404,"Lineage not found or not authorized") from exc


@router.get("/metrics")
def metrics(user: dict = Depends(require_user)): return list_metrics(user["id"])


@router.get("/terms")
def terms(user: dict = Depends(require_user)): return list_terms(user["id"])


@router.put("/admin/assets/{asset_id}/review")
def review_catalog_asset(asset_id: str, payload: AssetReview, user: dict = Depends(require_user)):
    try:
        return save_review(asset_id, user["id"], payload)
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc


@router.post("/admin/assets/{asset_id}/reject")
def reject_catalog_asset(asset_id: str, payload: ReviewDecision, user: dict = Depends(require_user)):
    try:
        return change_status(asset_id, user["id"], payload)
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc


@router.post("/admin/assets/{asset_id}/withdraw")
def withdraw_catalog_asset(asset_id: str, payload: ReviewDecision, user: dict = Depends(require_user)):
    try:
        return change_status(asset_id, user["id"], payload, withdraw=True)
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc


@router.post("/admin/assets/{asset_id}/publish")
def publish_catalog_asset(asset_id: str, version: int | None = Query(default=None, ge=0), user: dict = Depends(require_user)):
    try:
        return publish_asset(asset_id, user["id"], expected_version=version)
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc


@router.post("/admin/assets/batch-publish")
def batch_publish_catalog_assets(payload: BatchPublishRequest, user: dict = Depends(require_user)):
    return publish_assets([item.model_dump() for item in payload.items], user["id"])
