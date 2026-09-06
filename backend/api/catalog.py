"""Authenticated HTTP routes for the governed data catalog."""
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.auth import require_user
from backend.catalog.publications import publish_asset, require_catalog_admin
from backend.catalog.retrieval import catalog_overview, lineage_for_asset
from backend.catalog.service import get_asset, list_assets, list_domains, list_metrics, list_terms, sample_asset

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


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


@router.post("/admin/assets/{asset_id}/publish")
def publish_catalog_asset(asset_id: str, user: dict = Depends(require_user)):
    try:
        return publish_asset(asset_id, user["id"])
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc