"""Official MCP SDK client wrapper for Streamable HTTP servers."""
from __future__ import annotations

from typing import Any

import httpx

from backend.mcp.repository import get_server


class McpUnavailable(RuntimeError):
    pass


def _sdk():
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:  # pragma: no cover - deployment configuration guard
        raise McpUnavailable("MCP Python SDK 未安装，请安装 requirements.txt 后重启服务。") from exc
    return ClientSession, streamable_http_client


def _json(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return dict(value)


async def discover_server(server_id: str) -> dict[str, Any]:
    server = get_server(server_id, include_secret=True)
    if not server or not server["enabled"]:
        raise McpUnavailable("MCP Server 不存在或未启用。")
    if server["transport"] != "streamable-http":
        raise McpUnavailable("当前版本仅支持 Streamable HTTP MCP Server。")
    ClientSession, streamable_http_client = _sdk()
    headers = {}
    if server["auth_type"] == "bearer" and server.get("api_key"):
        headers["Authorization"] = f"Bearer {server['api_key']}"
    timeout = httpx.Timeout(server["timeout_seconds"])
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=timeout, trust_env=False, follow_redirects=True,
        ) as http_client:
            async with streamable_http_client(server["endpoint"], http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialized = await session.initialize()
                    tools: list[dict[str, Any]] = []
                    cursor = None
                    while True:
                        page = await session.list_tools(cursor=cursor)
                        tools.extend(_json(tool) for tool in page.tools)
                        cursor = getattr(page, "next_cursor", None)
                        if not cursor:
                            break
                    init_payload = _json(initialized)
                    return {
                        "protocol_version": str(init_payload.get("protocolVersion") or init_payload.get("protocol_version") or ""),
                        "capabilities": _json(getattr(initialized, "capabilities", {})),
                        "server_info": _json(getattr(initialized, "serverInfo", {}) or getattr(initialized, "server_info", {})),
                        "tools": tools,
                    }
    except McpUnavailable:
        raise
    except Exception as exc:
        raise McpUnavailable(f"MCP 连接失败：{str(exc)[:240]}") from exc


async def call_server_tool(server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    server = get_server(server_id, include_secret=True)
    if not server or not server["enabled"]:
        raise McpUnavailable("MCP Server 不存在或未启用。")
    ClientSession, streamable_http_client = _sdk()
    headers = {}
    if server["auth_type"] == "bearer" and server.get("api_key"):
        headers["Authorization"] = f"Bearer {server['api_key']}"
    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(server["timeout_seconds"]),
            trust_env=False,
            follow_redirects=True,
        ) as http_client:
            async with streamable_http_client(server["endpoint"], http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return _json(result)
    except Exception as exc:
        raise McpUnavailable(f"MCP 工具调用失败：{str(exc)[:240]}") from exc
