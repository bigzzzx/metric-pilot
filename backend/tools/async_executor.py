"""Async governed execution for remote tools such as MCP."""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from pydantic import ValidationError

from backend.observability import log_event
from backend.tools.approvals import claim_approved, create_approval, record_audit
from backend.tools.errors import ToolApprovalError, ToolRuntimeError
from backend.tools.executor import ToolExecutor, executor
from backend.tools.models import RiskLevel, ToolError, ToolExecutionContext, ToolResult, ToolStatus
from backend.tools.policy import evaluate_tool_policy


class AsyncToolExecutor(ToolExecutor):
    async def ainvoke(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        *,
        approval_id: str | None = None,
        thread_id: str | None = None,
    ) -> ToolResult:
        try:
            spec = self.registry.get(tool_id)
        except ToolRuntimeError as exc:
            return self._error_result(tool_id, context, exc.code, exc.message)
        try:
            validated = spec.input_schema.model_validate(arguments)
        except ValidationError as exc:
            return self._error_result(
                tool_id, context, "invalid_tool_input",
                "工具参数格式不正确：" + exc.errors(include_url=False)[0]["msg"],
            )
        normalized = validated.model_dump(mode="json")
        decision = evaluate_tool_policy(spec, context, self.connection_factory)
        if not decision.allowed:
            return self._error_result(
                tool_id, context, decision.code, decision.message,
                status=ToolStatus.DENIED, required_permission=decision.required_permission,
            )
        if decision.risk_level is RiskLevel.HIGH and not approval_id:
            display = spec.redact_input(validated) if spec.redact_input else normalized
            approval = create_approval(
                spec, context, normalized, display,
                thread_id=thread_id or f"tool:{context.call_id}",
                connection_factory=self.connection_factory,
            )
            audit_id = record_audit(
                context, tool_id, "approval_requested", ToolStatus.APPROVAL_REQUIRED.value,
                approval_id=approval["id"], detail={"risk_level": decision.risk_level.value},
                connection_factory=self.connection_factory,
            )
            return ToolResult(
                status=ToolStatus.APPROVAL_REQUIRED, tool_id=tool_id,
                call_id=context.call_id, audit_id=audit_id,
                approval={
                    "approval_id": approval["id"], "tool_id": tool_id,
                    "tool_name": spec.name, "risk_level": decision.risk_level.value,
                    "summary": display.get("summary") or spec.description,
                    "target": display.get("target") or "",
                    "effects": display.get("estimated_effects") or [],
                    "required_permission": spec.required_permission,
                    "expires_at": approval["expires_at"], "status": approval["status"],
                },
            )
        if decision.risk_level is RiskLevel.HIGH:
            try:
                claim_approved(
                    approval_id or "", context.user_id, tool_id, normalized,
                    connection_factory=self.connection_factory,
                )
            except ToolApprovalError as exc:
                status = ToolStatus.REJECTED if exc.code in {"approval_rejected", "approval_not_approved"} else ToolStatus.DENIED
                return self._error_result(tool_id, context, exc.code, exc.message, status=status)

        started = time.perf_counter()
        try:
            raw = spec.handler(validated, context)
            if inspect.isawaitable(raw):
                raw = await asyncio.wait_for(raw, timeout=spec.timeout_seconds)
            output = spec.output_schema.model_validate(raw)
            duration = round((time.perf_counter() - started) * 1000)
            audit_id = record_audit(
                context, tool_id, "executed", ToolStatus.SUCCESS.value,
                approval_id=approval_id, duration_ms=duration,
                connection_factory=self.connection_factory,
            )
            log_event("tool.executed", tool_id=tool_id, duration_ms=duration)
            if self.cache and decision.risk_level is RiskLevel.HIGH:
                self.cache.invalidate_all()
            return ToolResult(
                status=ToolStatus.SUCCESS, tool_id=tool_id, call_id=context.call_id,
                data=output.model_dump(mode="json"), audit_id=audit_id,
            )
        except asyncio.TimeoutError:
            return self._error_result(
                tool_id, context, "tool_timeout", f"工具“{spec.name}”执行超时。",
                status=ToolStatus.UNAVAILABLE,
            )
        except ToolRuntimeError as exc:
            status = ToolStatus.UNAVAILABLE if exc.code == "tool_unavailable" else ToolStatus.FAILED
            return self._error_result(tool_id, context, exc.code, exc.message, status=status)
        except Exception as exc:
            log_event("tool.failed", level=30, tool_id=tool_id, error_type=type(exc).__name__)
            return self._error_result(
                tool_id, context, "tool_failed",
                f"工具“{spec.name}”执行失败，请稍后重试或联系管理员查看审计日志。",
            )


async_executor = AsyncToolExecutor(cache=executor.cache)
