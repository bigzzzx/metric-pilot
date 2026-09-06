"""LangGraph interrupt/resume workflow for one governed high-risk tool call."""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from backend.checkpoint import checkpoint_saver
from backend.tools.executor import ToolExecutor, executor
from backend.tools.models import ToolError, ToolExecutionContext, ToolResult, ToolStatus


class ToolApprovalState(TypedDict, total=False):
    tool_id: str
    arguments: dict[str, Any]
    context: dict[str, Any]
    thread_id: str
    tool_result: dict[str, Any]


def build_approval_graph(runtime: ToolExecutor, saver=checkpoint_saver):
    def approval_node(state: ToolApprovalState) -> ToolApprovalState:
        context = ToolExecutionContext.model_validate(state["context"])
        pending = runtime.invoke(
            state["tool_id"], state["arguments"], context, thread_id=state["thread_id"]
        )
        if pending.status is not ToolStatus.APPROVAL_REQUIRED:
            return {"tool_result": pending.model_dump(mode="json")}
        decision = interrupt(pending.approval)
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        approval_id = pending.approval["approval_id"]
        if not approved:
            rejected = ToolResult(
                status=ToolStatus.REJECTED,
                tool_id=state["tool_id"],
                call_id=context.call_id,
                error=ToolError(code="approval_rejected", message="用户已拒绝本次高风险操作。"),
                approval={**pending.approval, "status": "rejected"},
            )
            return {"tool_result": rejected.model_dump(mode="json")}
        completed = runtime.invoke(
            state["tool_id"], state["arguments"], context, approval_id=approval_id
        )
        return {"tool_result": completed.model_dump(mode="json")}

    builder = StateGraph(ToolApprovalState)
    builder.add_node("approval", approval_node)
    builder.add_edge(START, "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=saver)


approval_graph = build_approval_graph(executor)


def start_approval_flow(
    tool_id: str,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolResult:
    thread_id = f"tool:{context.call_id}"
    state = {
        "tool_id": tool_id,
        "arguments": arguments,
        "context": context.model_dump(mode="json"),
        "thread_id": thread_id,
    }
    result = approval_graph.invoke(
        state, config={"configurable": {"thread_id": thread_id}}
    )
    interruptions = result.get("__interrupt__", [])
    if interruptions:
        payload = interruptions[0].value
        return ToolResult(
            status=ToolStatus.APPROVAL_REQUIRED,
            tool_id=tool_id,
            call_id=context.call_id,
            approval=payload,
        )
    return ToolResult.model_validate(result["tool_result"])


def resume_approval_flow(thread_id: str, approved: bool) -> ToolResult:
    result = approval_graph.invoke(
        Command(resume={"approved": approved}),
        config={"configurable": {"thread_id": thread_id}},
    )
    return ToolResult.model_validate(result["tool_result"])
