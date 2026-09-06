"""Authenticated tool catalog and human approval endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.auth import require_user
from backend.tools.async_executor import async_executor
from backend.tools.approval_graph import resume_approval_flow
from backend.tools.approvals import decide_approval, get_approval, list_approvals, save_approval_result
from backend.tools.errors import ToolApprovalError, ToolRuntimeError
from backend.tools.models import ToolError, ToolExecutionContext, ToolResult, ToolStatus
from backend.tools.registry import initialized_registry

router = APIRouter(prefix="/api", tags=["tools"])


class ApprovalDecision(BaseModel):
    decision: str


def _approval_error(exc: ToolRuntimeError) -> HTTPException:
    status = 404 if exc.code == "approval_not_found" else 409
    return HTTPException(status, detail={"code": exc.code, "message": exc.message})


async def _resume_mcp_approval(
    approval: dict,
    approved: bool,
    *,
    tool_executor=async_executor,
    tool_registry=None,
) -> ToolResult:
    """Resume one MCP call from its durable, original execution context."""
    context_data = approval.get("context") or {}
    arguments = approval.get("arguments") or {}
    try:
        context = ToolExecutionContext.model_validate(context_data)
    except Exception as exc:
        raise ToolApprovalError("approval_context_missing", "审批上下文不完整，无法安全恢复该 MCP 操作。") from exc
    if not approved:
        return ToolResult(
            status=ToolStatus.REJECTED,
            tool_id=approval["tool_id"],
            call_id=context.call_id,
            error=ToolError(code="approval_rejected", message="用户已拒绝本次高风险操作。"),
        )
    try:
        (tool_registry or initialized_registry()).get(approval["tool_id"])
    except ToolRuntimeError as exc:
        return ToolResult(
            status=ToolStatus.UNAVAILABLE,
            tool_id=approval["tool_id"],
            call_id=context.call_id,
            error=ToolError(code=exc.code, message=exc.message),
        )
    return await tool_executor.ainvoke(
        approval["tool_id"], arguments, context,
        approval_id=approval["id"], thread_id=approval["thread_id"],
    )


@router.get("/tools/{tool_id}")
def tool_detail(tool_id: str, _user: dict = Depends(require_user)):
    try:
        return initialized_registry().get(tool_id).catalog_item()
    except ToolRuntimeError as exc:
        raise HTTPException(404, detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/tool-approvals")
def approvals(
    status: str | None = Query(default="pending"),
    user: dict = Depends(require_user),
):
    return list_approvals(user["id"], status=status)


@router.get("/tool-approvals/{approval_id}")
def approval_detail(approval_id: str, user: dict = Depends(require_user)):
    try:
        item = get_approval(approval_id)
        if item["requested_by"] != user["id"]:
            raise ToolApprovalError("approval_not_found", "审批请求不存在或你无权查看。")
        return item
    except ToolApprovalError as exc:
        raise _approval_error(exc) from exc


@router.post("/tool-approvals/{approval_id}/decision")
async def approval_decision(
    approval_id: str,
    payload: ApprovalDecision,
    user: dict = Depends(require_user),
):
    try:
        approval = decide_approval(approval_id, user["id"], payload.decision)
        spec = initialized_registry().get(approval["tool_id"])
        if spec.category == "mcp":
            result = await _resume_mcp_approval(approval, payload.decision == "approved")
        else:
            result = resume_approval_flow(
                approval["thread_id"], payload.decision == "approved"
            )
        result_payload = result.model_dump(mode="json")
        stored = save_approval_result(approval_id, result_payload)
        return {"approval": stored, "tool_result": result_payload}
    except ToolApprovalError as exc:
        raise _approval_error(exc) from exc
