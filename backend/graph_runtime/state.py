"""Public input/output contracts and shared graph state."""
from typing import Literal, TypedDict


class AgentInputState(TypedDict):
    """Fields accepted when a new LangGraph run begins."""

    message: str
    agent_id: str
    model_id: str | None
    history: list[dict]
    conversation_summary: str
    user_memories: list[dict]
    user_id: str
    conversation_id: str
    source_message_id: str | None
    last_query_result: list[dict] | None


class AgentOutputState(TypedDict, total=False):
    """Fields exposed after a LangGraph run completes."""

    rag: list[dict]
    query_mode: Literal['text_to_metrics', 'text_to_sql']
    metric_resolution: dict
    metric_parameter_model_used: bool
    visualizations: list[dict]
    visualization_notice: str
    visualization_reused: bool
    plan: dict
    sql: str
    result: list[dict]
    final: dict
    memory_actions: list[dict]
    approval: dict
    permission_error: dict
    tool_result: dict
    verified_schemas: dict
    quality_risks: list[dict]
    quality_notice: str
    plan_cache_hit: bool
    answer_cache_hit: bool
    route_cache_hit: bool
    route: Literal["general", "data_query", "data_analysis", "data_write", "etl", "attachment"]
    route_source: Literal["model", "fast_path"]
    contextual_follow_up: bool
    retrieval_query: str | None
    retrieval_query_used: str
    node_trace: list[dict]


class AgentOverallState(AgentInputState, AgentOutputState, total=False):
    """Complete shared state for the whole Agent graph execution."""

    route: Literal["general", "data_query", "data_analysis", "data_write", "etl", "attachment"]
    route_source: Literal["model", "fast_path"]
    contextual_follow_up: bool
    retrieval_query: str | None
    retrieval_query_used: str
    context_reference: Literal["none", "recent_data_topic", "recent_query_result", "conversation_summary"]
    requires_data_evidence: bool
    query_error: str
    repair_attempts: int
    memory_retrieval: Literal["none", "search"]
    memory_query: str | None
    memory_search_count: int
    selected_skill_ids: list[str]
    skill_context: str
    mcp_tool_call: dict
    mcp_tool_result: dict
    query_metrics: list[dict]
    query_terminal: bool
    visualization_request: dict | None
    visualization_only: bool
