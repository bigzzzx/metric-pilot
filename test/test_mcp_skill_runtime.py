import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.api.tools import _resume_mcp_approval
from backend.main import init_storage
from backend.mcp.client import discover_server
from backend.mcp.repository import create_server, delete_server, replace_discovered_tools, update_tool
from backend.mcp.runtime import _input_model
from backend.skills.repository import create_skill, delete_skill, selected_skill_context, update_skill
from backend.tools.approvals import decide_approval, get_approval
from backend.tools.async_executor import AsyncToolExecutor
from backend.tools.models import HighRiskOperationInput, HighRiskOperationOutput, RiskLevel, ToolExecutionContext, ToolSpec
from backend.tools.policy import evaluate_tool_policy
from backend.tools.registry import ToolRegistry


def _factory(path):
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    return connect


def test_discovered_tools_default_enabled_and_use_annotations_for_risk(tmp_path):
    factory = _factory(tmp_path / "mcp.db")
    init_storage(factory)
    server = create_server(
        name="test", description="", transport="streamable-http",
        endpoint="http://127.0.0.1:18765/mcp", auth_type="none", api_key="",
        timeout_seconds=10, enabled=True, connection_factory=factory,
    )

    tools = replace_discovered_tools(server["id"], [
        {"name": "read", "description": "read", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
        {"name": "write", "description": "write", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": True}},
        {"name": "unknown", "description": "unknown", "inputSchema": {"type": "object"}},
    ], factory)

    assert {tool["name"]: tool["risk_level"] for tool in tools} == {
        "read": "low", "write": "high", "unknown": "medium",
    }
    assert all(tool["enabled"] for tool in tools)
    changed = update_tool(tools[0]["id"], {"enabled": False, "risk_level": "medium", "required_permission": "metric.read"}, factory)
    assert changed["enabled"] is False
    assert changed["required_permission"] == "metric.read"


def test_mcp_json_schema_is_enforced_through_pydantic():
    model = _input_model("mcp:test:lookup", {
        "type": "object",
        "properties": {"name": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}},
        "required": ["name"],
        "additionalProperties": False,
    })

    assert model.model_validate({"name": "GMV", "limit": 2}).model_dump()["name"] == "GMV"
    with pytest.raises(ValidationError):
        model.model_validate({"limit": 0})


def test_mcp_tool_is_allowed_only_when_server_or_tool_is_bound(tmp_path):
    factory = _factory(tmp_path / "policy.db")
    init_storage(factory)
    connection = factory()
    connection.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('u1','user1','hash',1)")
    connection.execute(
        "INSERT INTO agents(id,name,description,model_id,tools,system_prompt,enabled,mcp_servers,mcp_tools,skills) VALUES (?,?,?,?,?,?,1,?,?,?)",
        ("general-agent", "General", "", "m1", "[]", "", json.dumps(["server-1"]), "[]", "[]"),
    )
    connection.commit(); connection.close()
    spec = ToolSpec(
        id="mcp:server-1:lookup", name="lookup", description="lookup",
        input_schema=HighRiskOperationInput, output_schema=HighRiskOperationOutput,
        handler=lambda *_: {}, source="mcp:server-1", risk_level=RiskLevel.LOW,
    )
    context = ToolExecutionContext(user_id="u1", agent_id="general-agent")

    assert evaluate_tool_policy(spec, context, factory).allowed is True
    connection = factory(); connection.execute("UPDATE agents SET mcp_servers='[]' WHERE id='general-agent'"); connection.commit(); connection.close()
    assert evaluate_tool_policy(spec, context, factory).code == "agent_tool_denied"


def test_skill_requires_binding_and_keeps_version_history(tmp_path):
    factory = _factory(tmp_path / "skills.db")
    init_storage(factory)
    connection = factory()
    connection.execute(
        "INSERT INTO agents(id,name,description,model_id,tools,system_prompt,enabled,mcp_servers,mcp_tools,skills) VALUES (?,?,?,?,?,?,1,'[]','[]','[]')",
        ("data-query-agent", "Data Query", "", "m1", "[]", ""),
    )
    connection.commit(); connection.close()
    skill = create_skill(
        name="指标口径", description="解释指标", instructions="回答时说明计算公式。",
        applicability="用户询问指标口径", tags=["指标"], connection_factory=factory,
    )
    assert selected_skill_context("data-query-agent", [skill["id"]], connection_factory=factory) == ""
    connection = factory(); connection.execute("UPDATE agents SET skills=? WHERE id='data-query-agent'", (json.dumps([skill["id"]]),)); connection.commit(); connection.close()
    assert "计算公式" in selected_skill_context("data-query-agent", [skill["id"]], connection_factory=factory)
    updated = update_skill(skill["id"], {"instructions": "回答时说明公式和时间范围。", "change_note": "补充时间范围"}, factory)
    assert updated["current_version"] == 2
    archived = update_skill(skill["id"], {"archived": True, "enabled": False, "change_note": "归档"}, factory)
    assert archived["archived"] is True
    connection = factory()
    assert json.loads(connection.execute("SELECT skills FROM agents WHERE id='data-query-agent'").fetchone()["skills"]) == []
    connection.close()


def test_delete_mcp_server_removes_tools_and_agent_bindings(tmp_path):
    factory = _factory(tmp_path / "delete-mcp.db")
    init_storage(factory)
    server = create_server(
        name="delete-me", description="", transport="streamable-http",
        endpoint="http://127.0.0.1:18765/mcp", auth_type="none", api_key="",
        timeout_seconds=10, enabled=True, connection_factory=factory,
    )
    tools = replace_discovered_tools(server["id"], [
        {"name": "lookup", "inputSchema": {"type": "object"}},
        {"name": "write", "inputSchema": {"type": "object"}},
    ], factory)
    connection = factory()
    connection.execute(
        "INSERT INTO agents(id,name,description,model_id,tools,system_prompt,enabled,mcp_servers,mcp_tools,skills) VALUES (?,?,?,?,?,?,1,?,?,?)",
        ("delete-agent", "Delete", "", "m1", "[]", "", json.dumps([server["id"]]), json.dumps([tools[0]["id"]]), "[]"),
    )
    connection.commit(); connection.close()

    result = delete_server(server["id"], factory)

    assert result["deleted_tool_count"] == 2
    connection = factory()
    assert connection.execute("SELECT COUNT(*) FROM mcp_servers").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM mcp_tools").fetchone()[0] == 0
    agent = connection.execute("SELECT mcp_servers,mcp_tools FROM agents WHERE id='delete-agent'").fetchone()
    assert json.loads(agent["mcp_servers"]) == []
    assert json.loads(agent["mcp_tools"]) == []
    connection.close()


def test_delete_skill_removes_versions_and_agent_bindings(tmp_path):
    factory = _factory(tmp_path / "delete-skill.db")
    init_storage(factory)
    skill = create_skill(
        name="delete-me", description="", instructions="rule",
        applicability="", tags=[], connection_factory=factory,
    )
    update_skill(skill["id"], {"instructions": "rule v2"}, factory)
    connection = factory()
    connection.execute(
        "INSERT INTO agents(id,name,description,model_id,tools,system_prompt,enabled,mcp_servers,mcp_tools,skills) VALUES (?,?,?,?,?,?,1,'[]','[]',?)",
        ("delete-skill-agent", "Delete", "", "m1", "[]", "", json.dumps([skill["id"]])),
    )
    connection.commit(); connection.close()

    result = delete_skill(skill["id"], factory)

    assert result["deleted_version_count"] == 2
    connection = factory()
    assert connection.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0] == 0
    assert json.loads(connection.execute("SELECT skills FROM agents WHERE id='delete-skill-agent'").fetchone()["skills"]) == []
    connection.close()


def test_approved_high_risk_mcp_tool_resumes_from_durable_context(tmp_path):
    factory = _factory(tmp_path / "mcp-approval.db")
    init_storage(factory)
    connection = factory()
    connection.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('u1','user1','hash',1)")
    connection.execute(
        "INSERT INTO agents(id,name,description,model_id,tools,system_prompt,enabled,mcp_servers,mcp_tools,skills) VALUES (?,?,?,?,?,?,1,?,?,?)",
        ("general-agent", "General", "", "m1", "[]", "", json.dumps(["server-1"]), "[]", "[]"),
    )
    connection.commit()
    connection.close()

    calls: list[str] = []

    async def handler(payload, _context):
        calls.append(payload.summary)
        return {"accepted": True, "message": "remote write completed"}

    local_registry = ToolRegistry()
    local_registry.register(ToolSpec(
        id="mcp:server-1:dangerous_write", name="dangerous_write", description="write",
        input_schema=HighRiskOperationInput, output_schema=HighRiskOperationOutput,
        handler=handler, risk_level=RiskLevel.HIGH, source="mcp:server-1", category="mcp",
    ))
    runtime = AsyncToolExecutor(local_registry, factory)
    context = ToolExecutionContext(user_id="u1", agent_id="general-agent", conversation_id="c1")
    arguments = {"operation": "write", "summary": "update metric", "target": "warehouse"}

    pending = asyncio.run(runtime.ainvoke("mcp:server-1:dangerous_write", arguments, context, thread_id="c1"))
    assert pending.status.value == "approval_required"
    approval = get_approval(pending.approval["approval_id"], factory)
    assert approval["arguments"] == {**arguments, "estimated_effects": []}
    assert approval["context"]["call_id"] == context.call_id

    approved = decide_approval(approval["id"], "u1", "approved", factory)
    completed = asyncio.run(_resume_mcp_approval(
        approved, True, tool_executor=runtime, tool_registry=local_registry,
    ))

    assert completed.status.value == "success"
    assert calls == ["update metric"]
    assert get_approval(approval["id"], factory)["status"] == "consumed"


def _wait_port(port: int, timeout: float = 8) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("test MCP server did not start")


def test_official_sdk_discovers_real_streamable_http_server(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "test_mcp_server.py"
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(18765)
        monkeypatch.setattr("backend.mcp.client.get_server", lambda *_args, **_kwargs: {
            "id": "server-test", "enabled": True, "transport": "streamable-http",
            "endpoint": "http://127.0.0.1:18765/mcp", "auth_type": "none",
            "api_key": "", "timeout_seconds": 10,
        })
        result = asyncio.run(discover_server("server-test"))
        assert any(tool["name"] == "lookup_metric" for tool in result["tools"])
    finally:
        process.terminate()
        process.wait(timeout=5)
