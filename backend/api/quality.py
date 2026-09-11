"""Authenticated administration routes for real data-quality profiling."""
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.auth import require_user
from backend.catalog.publications import require_platform_admin
from backend.catalog.quality import QualityProfilerUnsupported, list_quality_runs, run_source_quality
from backend.data_sources.base import ConnectorUnavailable


router = APIRouter(prefix="/api/data-sources/{source_id}/quality", tags=["data-quality"])


@router.get("/runs")
def quality_runs(
    source_id: str, limit: int = Query(default=10, ge=1, le=50),
    user: dict = Depends(require_user),
):
    require_platform_admin(user["id"])
    return list_quality_runs(source_id, user["id"], limit)


@router.post("/run")
def run_quality(source_id: str, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    try:
        return run_source_quality(source_id, user["id"])
    except LookupError as exc:
        raise HTTPException(404, "Data source not found") from exc
    except QualityProfilerUnsupported as exc:
        raise HTTPException(422, str(exc)) from exc
    except ConnectorUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
