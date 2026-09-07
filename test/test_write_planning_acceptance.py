"""Complete parameters must reach the approval system, not a second LLM approval."""
import asyncio

from backend import agent
from backend.schemas import DataWritePlanOutput
from backend.tools.models import ToolResult, ToolStatus


def test_complete_write_plan_enters_approval_flow_without_executing(monkeypatch):
    called = []

    async def plan(_agent_id, messages, _schema, _model):
        system = messages[0]["content"]
        assert "PREPARE AN APPROVAL REQUEST" in system
        assert "set ready=true even if the user says to wait for approval" in system
        return DataWritePlanOutput(ready=True, operation="update", table="orders",
                                   values={"status": "CANCELLED"}, where=[{"column": "order_id", "value": 8}],
                                   summary="取消隔离订单")

    def pending(tool_id, arguments, context):
        called.append((tool_id, arguments, context.user_id))
        return ToolResult(status=ToolStatus.APPROVAL_REQUIRED, tool_id=tool_id, call_id=context.call_id,
                          approval={"approval_id": "pending", "status": "pending"})

    monkeypatch.setattr(agent, "complete_structured", plan)
    monkeypatch.setattr(agent, "writable_schema_summary", lambda: "orders(order_id INTEGER,status TEXT)")
    monkeypatch.setattr(agent, "prepare_sql_write", lambda args: {**args, "estimated_rows": 1})
    monkeypatch.setattr(agent, "start_approval_flow", pending)
    result = asyncio.run(agent.data_write_agent({"message": "更新订单8，等待批准", "history": [],
                                                "user_id": "owner", "conversation_id": "test"}))
    assert result["approval"]["status"] == "pending"
    assert called[0][0] == "sql_write"
    assert called[0][2] == "owner"
    assert result["tool_result"]["status"] == "approval_required"
