"""Expose discovered MCP tools through the platform Tool Registry."""
from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core import db
from backend.mcp.client import McpUnavailable, call_server_tool
from backend.mcp.repository import get_tool, list_tools, record_tool_call
from backend.tools.errors import ToolRuntimeError
from backend.tools.models import RiskLevel, ToolExecutionContext, ToolSpec


class McpArguments(BaseModel):
    """Pydantic entry point with a remote JSON Schema attached per tool."""

    model_config = ConfigDict(extra="allow")
    json_schema_contract: ClassVar[dict[str, Any]] = {"type": "object"}

    @model_validator(mode="before")
    @classmethod
    def validate_remote_schema(cls, value: Any) -> Any:
        try:
            Draft202012Validator(cls.json_schema_contract).validate(value)
        except (JsonSchemaValidationError, SchemaError) as exc:
            raise ValueError(f"不符合 MCP 工具输入 Schema：{exc.message}") from exc
        return value


class McpToolOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    content: list[dict[str, Any]] = Field(default_factory=list)
    structuredContent: Any = None
    isError: bool = False


def _input_model(tool_id: str, schema: dict[str, Any]) -> type[McpArguments]:
    suffix = re.sub(r"[^A-Za-z0-9]", "_", tool_id)[-80:]
    return type(
        f"McpArguments_{suffix}",
        (McpArguments,),
        {"json_schema_contract": schema or {"type": "object"}},
    )


def _handler(tool_id: str, server_id: str, tool_name: str):
    async def invoke(payload: BaseModel, _context: ToolExecutionContext) -> dict[str, Any]:
        try:
            result = await call_server_tool(server_id, tool_name, payload.model_dump(mode="json"))
        except McpUnavailable as exc:
            record_tool_call(tool_id, f"failed: {str(exc)[:220]}")
            raise ToolRuntimeError("tool_unavailable", str(exc)) from exc
        if result.get("isError") or result.get("is_error"):
            record_tool_call(tool_id, "remote_error")
            raise ToolRuntimeError("mcp_tool_error", "MCP Server 返回了工具执行错误。")
        normalized = {
            "content": result.get("content") or [],
            "structuredContent": result.get("structuredContent", result.get("structured_content")),
            "isError": False,
        }
        record_tool_call(tool_id, "success")
        return normalized

    return invoke


def register_mcp_tools(registry) -> None:
    """Refresh enabled, currently available MCP tools in the process registry."""
    for item in list_tools():
        if not item["enabled"] or item["unavailable"]:
            registry.remove(item["id"])
            continue
        registry.replace(ToolSpec(
            id=item["id"],
            name=item["display_name"],
            description=item["description"] or f"MCP tool {item['name']}",
            input_schema=_input_model(item["id"], item["input_schema"]),
            output_schema=McpToolOutput,
            handler=_handler(item["id"], item["server_id"], item["name"]),
            risk_level=RiskLevel(item["risk_level"]),
            required_permission=item["required_permission"],
            timeout_seconds=30,
            enabled=True,
            category="mcp",
            source=f"mcp:{item['server_id']}",
            version=item["updated_at"] or "1",
        ))


def tool_catalog_for_router(max_tools: int = 20, max_chars: int = 7000) -> list[dict[str, Any]]:
    """Return enabled tools that are bound to at least one enabled Agent."""
    connection = db()
    try:
        agents = connection.execute("SELECT id,mcp_servers,mcp_tools FROM agents WHERE enabled=1").fetchall()
    finally:
        connection.close()
    result: list[dict[str, Any]] = []
    used = 0
    for item in list_tools():
        if not item["enabled"] or item["unavailable"]:
            continue
        bound_agents = []
        for agent in agents:
            if item["server_id"] in json.loads(agent["mcp_servers"] or "[]") or item["id"] in json.loads(agent["mcp_tools"] or "[]"):
                bound_agents.append(agent["id"])
        if not bound_agents:
            continue
        summary = {
            "id": item["id"],
            "name": item["display_name"],
            "description": item["description"],
            "input_schema": item["input_schema"],
            "agents": bound_agents,
        }
        size = len(json.dumps(summary, ensure_ascii=False))
        if len(result) >= max_tools or used + size > max_chars:
            break
        result.append(summary)
        used += size
    return result


def is_tool_bound(tool_id: str, agent_id: str) -> bool:
    item = get_tool(tool_id)
    if not item:
        return False
    connection = db()
    try:
        agent = connection.execute(
            "SELECT mcp_servers,mcp_tools FROM agents WHERE id=? AND enabled=1", (agent_id,)
        ).fetchone()
        if not agent:
            return False
        return item["server_id"] in json.loads(agent["mcp_servers"] or "[]") or tool_id in json.loads(agent["mcp_tools"] or "[]")
    finally:
        connection.close()
