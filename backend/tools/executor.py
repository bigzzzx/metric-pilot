"""Governed execution entry point; handlers must not be called around it."""
from __future__ import annotations

import time
from typing import Any, Callable

from pydantic import ValidationError

from backend.core import db
from backend.observability import log_event
from backend.tools.approvals import claim_approved, create_approval, record_audit
from backend.tools.errors import ToolApprovalError, ToolRuntimeError
from backend.tools.models import (
    RiskLevel,
    ToolError,
    ToolExecutionContext,
    ToolResult,
    ToolStatus,
)
from backend.tools.policy import evaluate_tool_policy
from backend.tools.registry import ToolRegistry, registry
from backend.tools.cache import ToolCache, configured_tool_cache

ConnectionFactory = Callable[[], Any]


class ToolExecutor:
    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        connection_factory: ConnectionFactory | None = None,
        cache: ToolCache | None = None,
    ) -> None:
        self.registry = tool_registry or registry
        self.connection_factory = connection_factory or db
        self.cache = cache

    def _error_result(
        self,
        tool_id: str,
        context: ToolExecutionContext,
        code: str,
        message: str,
        *,
        status: ToolStatus = ToolStatus.FAILED,
        required_permission: str | None = None,
    ) -> ToolResult:
        audit_id = record_audit(
            context, tool_id, "execution_blocked", status.value,
            detail={"code": code}, connection_factory=self.connection_factory,
        )
        return ToolResult(
            status=status, tool_id=tool_id, call_id=context.call_id, audit_id=audit_id,
            error=ToolError(
                code=code, message=message, required_permission=required_permission,
                resource_type=context.resource_type, resource_id=context.resource_id,
            ),
        )

    def invoke(
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
                status=ToolStatus.DENIED,
                required_permission=decision.required_permission,
            )
        if (self.cache and decision.risk_level is not RiskLevel.HIGH
                and spec.cache_ttl_seconds > 0 and context.cache_mode == "default"):
            cached = self.cache.get(spec, normalized, context)
            if cached is not None:
                try:
                    output = spec.output_schema.model_validate(cached.data)
                except ValidationError:
                    self.cache.delete(cached.key)
                else:
                    audit_id = record_audit(
                        context, tool_id, "executed", ToolStatus.SUCCESS.value,
                        detail={"cache_hit": True, "cache_backend": self.cache.backend_name},
                        duration_ms=0, connection_factory=self.connection_factory,
                    )
                    return ToolResult(
                        status=ToolStatus.SUCCESS, tool_id=tool_id, call_id=context.call_id,
                        data=output.model_dump(mode="json"), audit_id=audit_id, cache_hit=True,
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
                    "approval_id": approval["id"],
                    "tool_id": tool_id,
                    "tool_name": spec.name,
                    "risk_level": decision.risk_level.value,
                    "summary": display.get("summary") or spec.description,
                    "target": display.get("target") or "",
                    "effects": display.get("estimated_effects") or [],
                    "required_permission": spec.required_permission,
                    "expires_at": approval["expires_at"],
                    "status": approval["status"],
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
            output = spec.output_schema.model_validate(raw)
            serialized = output.model_dump(mode="json")
            duration = round((time.perf_counter() - started) * 1000)
            audit_id = record_audit(
                context, tool_id, "executed", ToolStatus.SUCCESS.value,
                approval_id=approval_id, duration_ms=duration,
                detail={"cache_hit": False},
                connection_factory=self.connection_factory,
            )
            log_event("tool.executed", tool_id=tool_id, duration_ms=duration)
            if tool_id == 'readonly_sql':
                from backend.lineage.capture import record_executed_query
                record_executed_query(normalized['sql'],context.user_id,normalized.get('source_id'),
                    'tool:readonly_sql',connection_factory=self.connection_factory)
            result = ToolResult(
                status=ToolStatus.SUCCESS, tool_id=tool_id, call_id=context.call_id,
                data=serialized, audit_id=audit_id,
            )
            if (self.cache and decision.risk_level is not RiskLevel.HIGH
                    and spec.cache_ttl_seconds > 0 and context.cache_mode != "bypass"):
                self.cache.set(spec, normalized, context, serialized)
            if self.cache and tool_id in {"sql_write", "etl_execute"}:
                self.cache.invalidate_all()
            return result
        except ToolRuntimeError as exc:
            status = ToolStatus.UNAVAILABLE if exc.code == "tool_unavailable" else ToolStatus.FAILED
            return self._error_result(tool_id, context, exc.code, exc.message, status=status)
        except Exception as exc:
            log_event("tool.failed", level=30, tool_id=tool_id, error_type=type(exc).__name__)
            return self._error_result(
                tool_id, context, "tool_failed",
                f"工具“{spec.name}”执行失败，请稍后重试或联系管理员查看审计日志。",
            )


executor = ToolExecutor(cache=configured_tool_cache(db))

# Import after the executor class is defined to avoid a registration cycle.
from backend.tools.builtins import register_builtin_tools  # noqa: E402

register_builtin_tools()
