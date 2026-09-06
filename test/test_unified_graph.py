import asyncio

import pytest

from backend import agent
from backend.graph_runtime.events import streaming_answer
from backend.schemas import AnswerOutput, AnalysisOutput, QueryPlanOutput, SQLRepairOutput
from backend.tools.models import ToolResult, ToolStatus


@pytest.mark.parametrize("scenario", ["general", "query", "metadata", "repair", "failed", "analysis", "memory", "mcp", "write", "etl"])
def test_both_entry_points_execute_same_nodes_and_business_outcome(monkeypatch, scenario):
    calls = []
    attempts = []
    async def router(state):
        route = {"query": "data_query", "metadata": "data_query", "repair": "data_query", "failed": "data_query",
                 "analysis": "data_analysis", "write": "data_write", "etl": "etl"}.get(scenario, "general")
        return {**state, "route": route, "memory_retrieval": "search" if scenario == "memory" else "none",
                "memory_query": "preference", "mcp_tool_call": {"tool_id": "x"} if scenario == "mcp" else None}
    async def rag(state):
        calls.append("rag")
        return {**state, "rag": [{"asset_ids": ["orders"], "kind": "table", "title": "orders", "content": "orders(amount)"}]}
    async def memory(state):
        calls.append("memory")
        return state
    async def terminal(state):
        calls.append("approval")
        return {**state, "approval": {"approval_id": "a1", "status": "pending"}, "final": {"answer": "等待批准"}}
    async def complete(_id, messages, schema, _model):
        if schema is QueryPlanOutput:
            calls.append("plan")
            return schema(answer="说明", sql=None if scenario == "metadata" else "SELECT amount FROM orders")
        if schema is SQLRepairOutput:
            calls.append("repair")
            return schema(sql="SELECT amount FROM orders")
        calls.append("answer")
        return schema(answer="回答", **({"summary": "摘要"} if schema is AnalysisOutput else {}))
    async def stream(*args, **kwargs):
        calls.append("answer")
        yield "回"
        yield "答"
    def invoke(tool_id, payload, context):
        if tool_id == "catalog_schema":
            return ToolResult(status=ToolStatus.SUCCESS, tool_id=tool_id, call_id=context.call_id,
                              data={"tables": [{"asset_id": "orders", "name": "orders", "columns": []}]})
        calls.append("query")
        attempts.append(1)
        if scenario == "failed" or (scenario == "repair" and len(attempts) == 1):
            return ToolResult(status=ToolStatus.FAILED, tool_id=tool_id, call_id=context.call_id,
                              error={"code": "sql_error", "message": "bad column"})
        return ToolResult(status=ToolStatus.SUCCESS, tool_id=tool_id, call_id=context.call_id, data={"rows": [{"amount": 150}]})
    monkeypatch.setattr(agent, "router", router)
    monkeypatch.setattr(agent, "rag_retrieve", rag)
    monkeypatch.setattr(agent, "retrieve_long_term_memory", memory)
    monkeypatch.setattr(agent, "has_tool", lambda *_args: True)
    monkeypatch.setattr(agent, "data_write_agent", terminal)
    monkeypatch.setattr(agent, "etl_agent", terminal)
    monkeypatch.setattr(agent, "execute_mcp_tool", terminal)
    monkeypatch.setattr(agent, "complete_structured", complete)
    monkeypatch.setattr(agent, "stream_text", stream)
    monkeypatch.setattr(agent.tool_executor, "invoke", invoke)
    history = [{"role": "user", "content": 'Tool result: [{"amount":999}]'}]
    normal = asyncio.run(agent.run_agent("test", "data-query-agent", history))
    normal_calls = calls.copy()
    calls.clear()
    attempts.clear()
    async def collect():
        return [event async for event in agent.run_agent_stream("test", "data-query-agent", history)]
    events = asyncio.run(collect())
    streamed = events[-1]["state"]
    assert calls == normal_calls
    assert streamed.get("sql") == normal.get("sql")
    assert streamed.get("result") == normal.get("result")
    assert streamed["final"]["answer"] == normal["final"]["answer"]
    if scenario in {"write", "etl", "mcp"}:
        assert sum(item["event"] == "approval_required" for item in events) == 1
    if scenario in {"query", "repair"}:
        assert streamed["result"] == [{"amount": 150}]
    assert not streaming_answer.get()


def test_first_token_is_visible_before_model_finishes(monkeypatch):
    async def run():
        release = asyncio.Event()
        async def stream(*args, **kwargs):
            yield "第一段"
            await release.wait()
            yield "第二段"
        monkeypatch.setattr(agent, "stream_text", stream)
        async with asyncio.timeout(5):
            events = []
            async for event in agent.run_agent_stream("你好", "general-agent", []):
                events.append(event)
                if event["event"] == "delta":
                    release.set()
        assert [item["content"] for item in events if item["event"] == "delta"] == ["第一段", "第二段"]
        assert events[-1]["state"]["final"]["answer"] == "第一段第二段"
    asyncio.run(run())


def test_current_result_replaces_old_explicit_result_context(monkeypatch):
    captured = []
    async def complete(_agent, messages, schema, _model):
        captured.extend(messages)
        return schema(answer="新结果150", summary="150")
    monkeypatch.setattr(agent, "complete_structured", complete)
    asyncio.run(agent.data_analysis_agent({"message": "查询", "history": [], "rag": [],
                                           "last_query_result": [{"amount": 999}], "result": [{"amount": 150}]}))
    assert "999" not in str(captured)
    assert "150" in str(captured)


def test_stream_failure_does_not_emit_success_done(monkeypatch):
    async def stream(*args, **kwargs):
        yield "partial"
        raise RuntimeError("provider disconnected")
    monkeypatch.setattr(agent, "stream_text", stream)
    events = []
    async def run():
        async for event in agent.run_agent_stream("你好", "general-agent", []):
            events.append(event)
    with pytest.raises(RuntimeError, match="provider disconnected"):
        asyncio.run(run())
    assert not any(event["event"] == "done" for event in events)
    assert not streaming_answer.get()
