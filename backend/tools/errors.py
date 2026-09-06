"""Domain errors returned by the governed tool runtime."""
from __future__ import annotations


class ToolRuntimeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        required_permission: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.required_permission = required_permission
        self.resource_type = resource_type
        self.resource_id = resource_id


class ToolNotFound(ToolRuntimeError):
    def __init__(self, tool_id: str) -> None:
        super().__init__("tool_not_found", f"工具不存在：{tool_id}")


class ToolPermissionDenied(ToolRuntimeError):
    pass


class ToolApprovalError(ToolRuntimeError):
    pass
