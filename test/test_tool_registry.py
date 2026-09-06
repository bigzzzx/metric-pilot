import json
import sqlite3

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.extensions import tools
from backend.main import init_storage
from backend.tools.approvals import decide_approval, get_approval, list_approvals, save_approval_result
from backend.tools.approval_graph import build_approval_graph
from backend.tools.executor import ToolExecutor
from backend.tools.models import (
    HighRiskOperationInput,
    HighRiskOperationOutput,
    RiskLevel,
    ToolExecutionContext,
    ToolSpec,
    ToolStatus,
)
from backend.tools.registry import ToolRegistry


def _factory(path):
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    return connect


def _seed(factory, *, admin=True):
    connection = factory()
    connection.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('user-1','user1','hash',1)")
    role = "role-platform-admin" if admin else "role-data-analyst"
    connection.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES (?, 'user-1')", (role,))
    connection.execute(
        "INSERT INTO agents(id,name,description,model_id,tools,system_prompt,enabled,mcp_servers,skills) VALUES (?,?,?,?,?,?,1,'[]','[]')",
        ("data-query-agent", "Data Query Agent", "test", "model", json.dumps(["write_test"]), "test"),
    )
    connection.commit()
    connection.close()


def _runtime(factory, calls):
    registry = ToolRegistry()

    def handler(payload, _context):
        calls.append(payload.operation)
        return {"accepted": True, "message": "executed"}

    registry.register(ToolSpec(
        id="write_test", name="测试写入", description="test",
        input_schema=HighRiskOperationInput, output_schema=HighRiskOperationOutput,
        handler=handler, risk_level=RiskLevel.HIGH,
        required_permission="data.write", allowed_agents=frozenset({"data-query-agent"}),
    ))
    return ToolExecutor(registry, factory)


def _arguments(summary="更新数据"):
    return {"operation": "update", "summary": summary, "target": "orders", "estimated_effects": ["1 row"]}


def test_tool_catalog_comes_from_registry_and_excludes_agent_capabilities():
    identifiers = {item["id"] for item in tools()}
    assert "readonly_sql" in identifiers
    assert "sql_write" in identifiers
    assert "chat" not in identifiers
    assert "query_result_analysis" not in identifiers


def test_missing_permission_is_explicit_and_does_not_create_approval(tmp_path):
    factory = _factory(tmp_path / "denied.db")
    init_storage(factory)
    _seed(factory, admin=False)
    calls = []
    runtime = _runtime(factory, calls)
    context = ToolExecutionContext(user_id="user-1", agent_id="data-query-agent", conversation_id="c1")

    result = runtime.invoke("write_test", _arguments(), context)

    assert result.status is ToolStatus.DENIED
    assert result.error.code == "permission_denied"
    assert result.error.required_permission == "data.write"
    assert "data.write" in result.error.message
    assert list_approvals("user-1", connection_factory=factory) == []
    assert calls == []


def test_high_risk_tool_executes_once_after_one_time_approval(tmp_path):
    factory = _factory(tmp_path / "approved.db")
    init_storage(factory)
    _seed(factory, admin=True)
    calls = []
    runtime = _runtime(factory, calls)
    context = ToolExecutionContext(user_id="user-1", agent_id="data-query-agent", conversation_id="c1")

    pending = runtime.invoke("write_test", _arguments(), context, thread_id="tool-thread")
    assert pending.status is ToolStatus.APPROVAL_REQUIRED
    assert calls == []
    approval_id = pending.approval["approval_id"]
    decide_approval(approval_id, "user-1", "approved", factory)

    completed = runtime.invoke("write_test", _arguments(), context, approval_id=approval_id)
    repeated = runtime.invoke("write_test", _arguments(), context, approval_id=approval_id)

    assert completed.status is ToolStatus.SUCCESS
    assert calls == ["update"]
    assert repeated.status is ToolStatus.DENIED
    assert repeated.error.code == "approval_consumed"


def test_approved_arguments_cannot_be_changed(tmp_path):
    factory = _factory(tmp_path / "tampered.db")
    init_storage(factory)
    _seed(factory, admin=True)
    calls = []
    runtime = _runtime(factory, calls)
    context = ToolExecutionContext(user_id="user-1", agent_id="data-query-agent", conversation_id="c1")
    pending = runtime.invoke("write_test", _arguments(), context)
    approval_id = pending.approval["approval_id"]
    decide_approval(approval_id, "user-1", "approved", factory)

    result = runtime.invoke("write_test", _arguments("修改后的操作"), context, approval_id=approval_id)

    assert result.status is ToolStatus.DENIED
    assert result.error.code == "approval_tampered"
    assert calls == []


def test_langgraph_interrupt_resumes_only_after_persisted_decision(tmp_path):
    factory = _factory(tmp_path / "graph-approval.db")
    init_storage(factory)
    _seed(factory, admin=True)
    calls = []
    runtime = _runtime(factory, calls)
    graph = build_approval_graph(runtime, InMemorySaver())
    context = ToolExecutionContext(user_id="user-1", agent_id="data-query-agent", conversation_id="c1", call_id="call-graph")
    state = {
        "tool_id": "write_test", "arguments": _arguments(),
        "context": context.model_dump(mode="json"), "thread_id": "thread-graph",
    }
    config = {"configurable": {"thread_id": "thread-graph"}}

    pending = graph.invoke(state, config=config)
    assert pending["__interrupt__"][0].value["tool_name"] == "测试写入"
    assert calls == []
    approval_id = pending["__interrupt__"][0].value["approval_id"]
    decide_approval(approval_id, "user-1", "approved", factory)

    completed = graph.invoke(Command(resume={"approved": True}), config=config)

    assert completed["tool_result"]["status"] == "success"
    assert calls == ["update"]


def test_approval_result_is_persisted_for_page_refresh(tmp_path):
    factory = _factory(tmp_path / "result.db")
    init_storage(factory)
    _seed(factory, admin=True)
    runtime = _runtime(factory, [])
    context = ToolExecutionContext(
        user_id="user-1", agent_id="data-query-agent", conversation_id="c1"
    )
    pending = runtime.invoke("write_test", _arguments(), context)
    approval_id = pending.approval["approval_id"]

    save_approval_result(
        approval_id,
        {"status": "success", "data": {"affected_rows": 1}},
        factory,
    )

    assert get_approval(approval_id, factory)["result"] == {
        "status": "success", "data": {"affected_rows": 1}
    }
