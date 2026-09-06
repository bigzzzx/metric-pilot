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
