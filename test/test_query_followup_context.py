import asyncio

from backend import agent
from backend.query_quality.evidence import retrieval_question
from backend.tools.models import ToolResult, ToolStatus


def test_contextual_followup_retrieves_active_topic_without_model_call(monkeypatch):
    captured = []
    def invoke(tool_id, payload, context):
        captured.append(payload["query"])
        return ToolResult(status=ToolStatus.SUCCESS, tool_id=tool_id, call_id=context.call_id, data={"chunks": []})
    monkeypatch.setattr(agent.tool_executor, "invoke", invoke)
    state = {"message": "换成9月呢？", "route": "data_query", "contextual_follow_up": True,
             "history": [{"role": "user", "content": "8月已支付GMV是多少？"}],
             "conversation_summary": "当前分析电商订单", "user_id": "a"}
    asyncio.run(agent.rag_retrieve(state))
    assert "GMV" in captured[0] and "9月" in captured[0]
    assert "电商订单" in captured[0]


def test_standalone_query_does_not_inherit_previous_data_topic():
    state = {"message": "营销ROI", "contextual_follow_up": False,
             "history": [{"role": "user", "content": "电商GMV"}]}
    assert retrieval_question(state) == "营销ROI"


def test_retrieval_expansion_is_bounded_and_omits_tool_rows():
    state = {"message": "继续", "contextual_follow_up": True,
             "history": [{"role": "user", "content": "a" * 10000},
                         {"role": "user", "content": "Tool result: secret-row"}],
             "conversation_summary": "b" * 10000}
    query = retrieval_question(state)
    assert len(query) < 1300
    assert "secret-row" not in query

def test_router_rewrite_reaches_rag_without_extra_model_call(monkeypatch):
    from backend.attachments import repository
    from backend.schemas import RouteOutput

    monkeypatch.setattr(repository, "list_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(agent, "agent_cache", None)
    monkeypatch.setattr(agent, "has_tool", lambda *args: False)
    monkeypatch.setattr(agent, "bound_skill_summaries", lambda: [])
    monkeypatch.setattr(agent, "tool_catalog_for_router", lambda: [])
    monkeypatch.setattr(agent, "selected_skill_context", lambda *args: "")
    calls = []

    async def complete(*args):
        calls.append(args)
        return RouteOutput(
            route="data_query", contextual_follow_up=True,
            context_reference="recent_data_topic", requires_data_evidence=True,
            retrieval_query="9月已支付GMV是多少？",
        )

    searched = []

    def invoke(tool_id, payload, context):
        searched.append(payload["query"])
        return ToolResult(status=ToolStatus.SUCCESS, tool_id=tool_id,
                          call_id=context.call_id, data={"chunks": []})

    monkeypatch.setattr(agent, "complete_structured", complete)
    monkeypatch.setattr(agent.tool_executor, "invoke", invoke)
    state = {
        "message": "换成9月呢？", "route": "data_query", "history": [
            {"role": "user", "content": "8月已支付GMV是多少？"}
        ], "conversation_summary": "", "user_memories": [],
        "user_id": "user-a", "conversation_id": "conversation-a",
        "source_message_id": "message-a", "model_id": None,
        "last_query_result": None,
    }
    routed = asyncio.run(agent.router(state))
    result = asyncio.run(agent.rag_retrieve(routed))
    assert len(calls) == 1
    assert searched == ["9月已支付GMV是多少？"]
    assert result["retrieval_query_used"] == searched[0]
    assert result["message"] == "换成9月呢？"


def test_rewrite_rejects_invented_metric_and_changed_date():
    state = {
        "message": "那退款算进去吗？", "contextual_follow_up": True,
        "retrieval_query": "GMV是否包含退款？",
        "history": [{"role": "user", "content": "订单量怎么算？"}],
        "conversation_summary": "",
    }
    query = retrieval_question(state)
    assert query.startswith("那退款算进去吗？")
    assert "GMV" not in query

    state.update(message="换成9月呢？", retrieval_query="10月已支付GMV是多少？",
                 history=[{"role": "user", "content": "8月已支付GMV是多少？"}])
    query = retrieval_question(state)
    assert query.startswith("换成9月呢？")
    assert "10月" not in query


def test_noncontextual_turn_ignores_router_rewrite():
    state = {"message": "查询订单量", "contextual_follow_up": False,
             "retrieval_query": "GMV是多少？"}
    assert retrieval_question(state) == "查询订单量"
