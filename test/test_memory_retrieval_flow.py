import asyncio

from backend import agent
from backend.schemas import DataWritePlanOutput, RouteOutput
from backend.tools.models import ToolResult, ToolStatus


def _state(**overrides):
    state = {
        "message": "我平时喜欢什么回答方式",
        "agent_id": "data-query-agent",
        "model_id": None,
        "history": [],
        "conversation_summary": "",
        "user_memories": [
            {
                "id": "core-1",
                "memory_type": "preference",
                "content": "用户偏好：直接说重点",
            }
        ],
        "user_id": "user-a",
        "conversation_id": "conversation-a",
        "source_message_id": "message-a",
        "last_query_result": None,
        "repair_attempts": 0,
        "route": "general",
        "memory_retrieval": "search",
        "memory_query": "用户平时的回答习惯",
        "memory_search_count": 0,
    }
    state.update(overrides)
    return state


def test_route_output_supports_structured_memory_search_decision():
    decision = RouteOutput.model_validate(
        {
            "route": "general",
            "memory_retrieval": "search",
            "memory_query": "用户平时的回答偏好",
            "memory_actions": [],
        }
    )

    assert decision.memory_retrieval == "search"
    assert decision.memory_query == "用户平时的回答偏好"


def test_route_output_records_context_and_evidence_decision():
    decision = RouteOutput.model_validate(
        {
            "route": "general",
            "contextual_follow_up": True,
            "context_reference": "recent_data_topic",
            "requires_data_evidence": False,
            "memory_retrieval": "none",
            "memory_actions": [],
        }
    )

    assert decision.contextual_follow_up is True
    assert decision.context_reference == "recent_data_topic"
    assert decision.requires_data_evidence is False


def test_route_output_has_a_separate_high_risk_data_write_branch():
    decision = RouteOutput.model_validate(
        {
            "route": "data_write",
            "memory_retrieval": "none",
            "memory_query": None,
            "memory_actions": [],
        }
    )

    assert decision.route == "data_write"


def test_memory_search_node_uses_server_owned_user_and_deduplicates(monkeypatch):
    captured = {}

    def fake_invoke(tool_id, payload, context):
        captured.update(
            tool_id=tool_id,
            user_id=context.user_id,
            query=payload["query"],
            limit=payload["limit"],
        )
        memories = [
            {
                "id": "core-1",
                "memory_type": "preference",
                "content": "用户偏好：直接说重点",
            },
            {
                "id": "event-1",
                "memory_type": "working_convention",
                "content": "默认约定：分析报告先给结论",
            },
        ]
        return ToolResult(
            status=ToolStatus.SUCCESS,
            tool_id=tool_id,
            call_id=context.call_id,
            data={"memories": memories},
        )

    monkeypatch.setattr(agent, "has_tool", lambda *_args: True)
    monkeypatch.setattr(agent.tool_executor, "invoke", fake_invoke)

    result = asyncio.run(agent.retrieve_long_term_memory(_state()))

    assert captured == {
        "tool_id": "memory_search",
        "user_id": "user-a",
        "query": "用户平时的回答习惯",
        "limit": 5,
    }
    assert [item["id"] for item in result["user_memories"]] == [
        "core-1",
        "event-1",
    ]
    assert result["memory_search_count"] == 1


def test_memory_search_is_skipped_when_agent_lacks_permission(monkeypatch):
    monkeypatch.setattr(agent, "has_tool", lambda *_args: False)

    result = asyncio.run(agent.retrieve_long_term_memory(_state()))

    assert result["user_memories"] == _state()["user_memories"]
    assert result["memory_search_count"] == 0


def test_router_dispatches_to_memory_node_only_for_search_decision(monkeypatch):
    monkeypatch.setattr(agent, "has_tool", lambda *_args: True)

    assert agent.route_after_router(_state()) == "memory"
    assert agent.route_after_router(
        _state(memory_retrieval="none", memory_query=None)
    ) == "general"


def test_social_fast_path_skips_router_model_and_keeps_context(monkeypatch):
    from backend.attachments import repository
    monkeypatch.setattr(repository, 'list_files', lambda *a, **kw: [])
    async def should_not_call_model(*_args, **_kwargs):
        raise AssertionError("unambiguous greeting must not call Router model")

    monkeypatch.setattr(agent, "complete_structured", should_not_call_model)
    state = _state(
        message="你好！",
        history=[{"role": "user", "content": "之前在分析华东复购率"}],
        conversation_summary="用户此前在讨论华东复购率。",
    )

    result = asyncio.run(agent.router(state))

    assert result["route"] == "general"
    assert result["route_source"] == "fast_path"
    assert result["history"] == state["history"]
    assert result["conversation_summary"] == state["conversation_summary"]
    assert agent.is_unambiguous_social_message("那它怎么算？") is False


def test_router_receives_history_and_records_contextual_data_decision(monkeypatch):
    from backend.attachments import repository
    monkeypatch.setattr(repository, 'list_files', lambda *a, **kw: [])
    captured = {}

    async def fake_complete(_agent_id, prompt_messages, _schema, _model_id):
        captured["messages"] = prompt_messages
        return RouteOutput.model_validate(
            {
                "route": "general",
                "contextual_follow_up": True,
                "context_reference": "recent_data_topic",
                "requires_data_evidence": False,
                "memory_retrieval": "none",
                "memory_actions": [],
                "skill_ids": [],
                "tool_call": None,
            }
        )

    monkeypatch.setattr(agent, "complete_structured", fake_complete)
    monkeypatch.setattr(agent, "has_tool", lambda *_args: False)
    monkeypatch.setattr(agent, "bound_skill_summaries", lambda: [])
    monkeypatch.setattr(agent, "tool_catalog_for_router", lambda: [])
    monkeypatch.setattr(agent, "selected_skill_context", lambda *_args: "")

    result = asyncio.run(
        agent.router(
            _state(
                message="那它怎么算？",
                history=[{"role": "user", "content": "请分析华东复购率"}],
                conversation_summary="当前活跃数据话题：华东地区复购率。",
            )
        )
    )

    rendered = "\n".join(item["content"] for item in captured["messages"])
    assert "华东地区复购率" in rendered
    assert "请分析华东复购率" in rendered
    assert result["route"] == "general"
    assert result["contextual_follow_up"] is True
    assert result["context_reference"] == "recent_data_topic"
    assert result["requires_data_evidence"] is False


def test_data_write_node_pauses_for_a_one_time_approval(monkeypatch):
    captured = {}

    async def fake_complete(*_args, **_kwargs):
        return DataWritePlanOutput.model_validate({
            "ready": True,
            "operation": "update",
            "table": "orders",
            "values": {"status": "cancelled"},
            "where": [{"column": "order_id", "value": 1001}],
            "summary": "取消订单 1001",
            "clarification": "",
        })

    def fake_start(tool_id, arguments, context):
        captured.update(tool_id=tool_id, arguments=arguments, context=context)
        return ToolResult(
            status=ToolStatus.APPROVAL_REQUIRED,
            tool_id=tool_id,
            call_id=context.call_id,
            approval={
                "approval_id": "approval-1",
                "tool_name": "SQL 数据写入",
                "status": "pending",
            },
        )

    monkeypatch.setattr(agent, "complete_structured", fake_complete)
    monkeypatch.setattr(agent, "writable_schema_summary", lambda: "orders(order_id, status)")
    monkeypatch.setattr(agent, "prepare_sql_write", lambda plan: {**plan, "estimated_rows": 1})
    monkeypatch.setattr(agent, "start_approval_flow", fake_start)

    result = asyncio.run(agent.data_write_agent(_state(route="data_write")))

    assert captured["tool_id"] == "sql_write"
    assert captured["context"].user_id == "user-a"
    assert result["approval"]["approval_id"] == "approval-1"
    assert "等待你批准" in result["final"]["answer"]
