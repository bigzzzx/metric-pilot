"""Authenticated HTTP endpoints for platform administration."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.admin.service import administration_overview, audit_logs, update_user_access
from backend.auth import require_user

router = APIRouter(prefix="/api/admin", tags=["system-administration"])


class UserAccessUpdate(BaseModel):
    role_ids: list[str]
    group_ids: list[str]
    is_active: bool = True


@router.get("/overview")
def overview(user: dict = Depends(require_user)):
    return administration_overview(user["id"])


@router.put("/users/{user_id}/access")
def update_access(user_id: str, payload: UserAccessUpdate, user: dict = Depends(require_user)):
    return update_user_access(user["id"], user_id, role_ids=payload.role_ids, group_ids=payload.group_ids, is_active=payload.is_active)


@router.get("/audit-logs")
def list_audit_logs(limit: int = Query(default=100, ge=1, le=200), user: dict = Depends(require_user)):
    return audit_logs(user["id"], limit)
