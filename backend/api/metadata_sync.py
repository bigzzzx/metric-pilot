"""Authenticated HTTP contracts for metadata synchronization review."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.auth import require_user
from backend.catalog.publications import require_platform_admin
from backend.catalog.sync import decide_change, decide_changes, list_changes, list_sync_runs, rollback_change


router = APIRouter(prefix="/api/data-sources/{source_id}", tags=["metadata-sync"])


class ChangeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=500)


class BatchChangeDecision(ChangeDecision):
    change_ids: list[str] = Field(min_length=1, max_length=100)


@router.get("/sync-runs")
def sync_runs(
    source_id: str, limit: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(require_user),
):
    require_platform_admin(user["id"])
    return list_sync_runs(source_id, user["id"], limit)


@router.get("/changes")
def metadata_changes(
    source_id: str,
    status: Literal["pending", "approved", "rejected", "superseded", "rolled_back"] | None = "pending",
    user: dict = Depends(require_user),
):
    require_platform_admin(user["id"])
    return list_changes(source_id, user["id"], status)


@router.post("/changes/{change_id}/decision")
def review_metadata_change(
    source_id: str, change_id: str, payload: ChangeDecision,
    user: dict = Depends(require_user),
):
    require_platform_admin(user["id"])
    try:
        return decide_change(source_id, change_id, user["id"], payload.decision, payload.reason)
    except LookupError as exc:
        raise HTTPException(404, "Metadata change not found") from exc


@router.post("/changes/batch-decision")
def review_metadata_changes(
    source_id: str, payload: BatchChangeDecision,
    user: dict = Depends(require_user),
):
    require_platform_admin(user["id"])
    if len(payload.change_ids) != len(set(payload.change_ids)):
        raise HTTPException(422, "同一变更不能重复提交")
    return decide_changes(
        source_id, payload.change_ids, user["id"], payload.decision, payload.reason,
    )


@router.post("/changes/{change_id}/rollback")
def rollback_metadata_change(
    source_id: str, change_id: str, user: dict = Depends(require_user),
):
    require_platform_admin(user["id"])
    try:
        return rollback_change(source_id, change_id, user["id"])
    except LookupError as exc:
        raise HTTPException(404, "Metadata change not found") from exc
