"""Durable HITL acceptance: HTTP decisions, SQLite checkpoints, real demo writes."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import backend.api.tools as api
import backend.tools.approvals as approvals
from backend.auth import require_user
from backend.main import init_storage
from backend.tools.approval_graph import build_approval_graph
from backend.tools.executor import ToolExecutor
from backend.tools.models import RiskLevel, SqlWriteInput, SqlWriteOutput, ToolExecutionContext, ToolSpec
from backend.tools.registry import ToolRegistry
from backend.tools.sql_write_tool import execute_sql_write, prepare_sql_write


@pytest.fixture
def durable_approval(tmp_path, monkeypatch):
    def factory():
        con = sqlite3.connect(tmp_path / "platform.db")
        con.row_factory = sqlite3.Row
        return con

    init_storage(factory)
    con = factory()
    con.execute("INSERT INTO users(id,username,password_hash) VALUES ('owner','owner','unused')")
    con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','owner')")
    con.execute("INSERT INTO agents(id,tools,enabled) VALUES ('data-query-agent',?,1)", (json.dumps(["sql_write"]),))
    con.commit()
    con.close()
    warehouse = tmp_path / "warehouse.db"
    with sqlite3.connect(warehouse) as con:
        con.executescript("""
            CREATE TABLE orders(order_id INTEGER PRIMARY KEY, status TEXT);
            INSERT INTO orders VALUES (1001,'paid');
            CREATE TABLE effects(value TEXT);
            CREATE TRIGGER count_effect AFTER UPDATE ON orders
            BEGIN INSERT INTO effects VALUES (new.status); END;
        """)
    registry = ToolRegistry()
    registry.register(ToolSpec(
        id="sql_write", name="验收写入", description="仅隔离演示数据库",
        input_schema=SqlWriteInput, output_schema=SqlWriteOutput,
        handler=lambda payload, context: execute_sql_write(payload, context, warehouse),
        risk_level=RiskLevel.HIGH, required_permission="data.write",
        allowed_agents=frozenset({"data-query-agent"}),
    ))
    runtime = ToolExecutor(registry, factory)
    checkpoint = tmp_path / "checkpoints.db"
    config = {"configurable": {"thread_id": "acceptance-write"}}
    context = ToolExecutionContext(user_id="owner", agent_id="data-query-agent",
                                   conversation_id="conversation", call_id="acceptance-call")
    arguments = prepare_sql_write({"operation": "update", "table": "orders",
                                  "values": {"status": "cancelled"},
                                  "where": [{"column": "order_id", "value": 1001}],
                                  "summary": "验收取消订单"}, warehouse)
    with sqlite3.connect(checkpoint, check_same_thread=False) as checkpoint_connection:
        graph = build_approval_graph(runtime, SqliteSaver(checkpoint_connection))
        pending = graph.invoke({"tool_id": "sql_write", "arguments": arguments,
                                "context": context.model_dump(mode="json"),
                                "thread_id": "acceptance-write"}, config=config)
        approval_id = pending["__interrupt__"][0].value["approval_id"]
    checkpoint_connection.close()
    # A fresh graph and fresh checkpoint connection must resume the persisted interrupt.
    resumed_connection = sqlite3.connect(checkpoint, check_same_thread=False)
    graph = build_approval_graph(runtime, SqliteSaver(resumed_connection))
    monkeypatch.setattr(approvals, "db", factory)
    monkeypatch.setattr(api, "initialized_registry", lambda: registry)

    def resume(thread_id, approved):
        from backend.tools.models import ToolResult
        result = graph.invoke(Command(resume={"approved": approved}),
                              config={"configurable": {"thread_id": thread_id}})
        return ToolResult.model_validate(result["tool_result"])

    monkeypatch.setattr(api, "resume_approval_flow", resume)
    active_user = {"id": "owner"}
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: active_user

    def effects():
        with sqlite3.connect(warehouse) as con:
            return con.execute("SELECT COUNT(*) FROM effects").fetchone()[0]

    with TestClient(app) as client:
        try:
            yield client, approval_id, effects, active_user
        finally:
            resumed_connection.close()


@pytest.mark.parametrize("decision,expected,count", [("approved", "success", 1), ("rejected", "rejected", 0)])
def test_decision_survives_graph_reopen_and_repeat(durable_approval, decision, expected, count):
    client, approval_id, effects, active_user = durable_approval
    path = f"/api/tool-approvals/{approval_id}"
    assert effects() == 0
    active_user["id"] = "other-user"
    assert client.get(path).status_code == 404
    assert client.post(path + "/decision", json={"decision": decision}).status_code == 404
    active_user["id"] = "owner"
    first = client.post(path + "/decision", json={"decision": decision})
    assert first.status_code == 200, first.text
    assert first.json()["tool_result"]["status"] == expected
    repeated = client.post(path + "/decision", json={"decision": decision})
    assert repeated.status_code in (200, 409), repeated.text
    assert effects() == count
    assert client.get(path).json()["result"]["status"] == expected


def test_simultaneous_approval_clicks_do_not_duplicate_write(durable_approval):
    client, approval_id, effects, _user = durable_approval
    path = f"/api/tool-approvals/{approval_id}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(path + "/decision", json={"decision": "approved"}), range(2)))
    assert all(response.status_code in (200, 409) for response in responses)
    assert effects() == 1
    assert client.get(path).json()["result"]["status"] == "success"
