"""Strict contracts for model outputs exchanged by LangGraph nodes."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.models import SqlScalar, SqlWritePredicate


class ModelOutput(BaseModel):
    """Reject surprise fields so a valid JSON object is not mistaken for a valid plan."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MemoryAction(ModelOutput):
    """A model-declared call to the server-owned long-term memory service."""

    operation: Literal["save", "forget"]
    memory_type: Literal["profile", "preference", "working_convention", "goal", "constraint", "episodic"]
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value: str | None = Field(default=None, max_length=500)
    scope: Literal["user", "project"] = "user"
    confidence: float = Field(default=0.9, ge=0, le=1)
    sensitive: bool = False
    reason: str = Field(default="", max_length=180)


class PlannedToolCall(ModelOutput):
    tool_id: str = Field(min_length=1, max_length=300)
    arguments: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=300)


class RouteOutput(ModelOutput):
    route: Literal["general", "data_query", "data_analysis", "data_write", "etl"]
    contextual_follow_up: bool = False
    context_reference: Literal[
        "none", "recent_data_topic", "recent_query_result", "conversation_summary"
    ] = "none"
    requires_data_evidence: bool = False
    memory_retrieval: Literal["none", "search"] = "none"
    memory_query: str | None = Field(default=None, max_length=300)
    memory_actions: list[MemoryAction] = Field(default_factory=list, max_length=3)
    skill_ids: list[str] = Field(default_factory=list, max_length=3)
    tool_call: PlannedToolCall | None = None


class AnswerOutput(ModelOutput):
    answer: str = Field(min_length=1)


class MetricReference(ModelOutput):
    id: str = Field(min_length=1)
    version: str = Field(pattern=r"^[0-9a-f]{64}$")


class QueryPlanOutput(AnswerOutput):
    sql: str | None = None
    source_id: str | None = Field(default=None, max_length=200)
    schema_request: list[str] = Field(default_factory=list, max_length=3)
    metric_refs: list[MetricReference] = Field(default_factory=list, max_length=5)
    time_column: str | None = None
    filters: list[str] = Field(default_factory=list, max_length=20)
    join_relationships: list[str] = Field(default_factory=list, max_length=10)


class SQLRepairOutput(ModelOutput):
    sql: str = Field(min_length=1)


class DataWritePlanOutput(ModelOutput):
    ready: bool
    operation: Literal["insert", "update", "delete"] | None = None
    table: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    values: dict[str, SqlScalar] = Field(default_factory=dict, max_length=50)
    where: list[SqlWritePredicate] = Field(default_factory=list, max_length=20)
    summary: str = Field(min_length=1, max_length=500)
    clarification: str = Field(default="", max_length=500)


class ConversationSummaryOutput(ModelOutput):
    summary: str = Field(min_length=1, max_length=4000)


class AnalysisOutput(AnswerOutput):
    summary: str = Field(min_length=1)
    insights: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    answer: str
    source_id: str | None = None
    intent: Literal["CHAT", "DATA_QUERY"]
    sql: str = ""
    result: list[dict] = Field(default_factory=list)
    analysis: str = ""
    catalog: list[dict] = Field(default_factory=list)
    modelMode: Literal["configured"] = "configured"
    steps: list[dict] = Field(default_factory=list)
    approval: dict | None = None
    permission_error: dict | None = None
    tool_result: dict | None = None
