"""Stable contracts for registered tools, policy decisions, and execution results."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    REJECTED = "rejected"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class ToolExecutionContext(StrictModel):
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    conversation_id: str = ""
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    resource_type: str | None = None
    resource_id: str | None = None
    domain_id: str | None = None
    cache_mode: Literal["default", "refresh", "bypass"] = "default"


class ToolError(StrictModel):
    code: str
    message: str
    required_permission: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None


class ToolResult(StrictModel):
    status: ToolStatus
    tool_id: str
    call_id: str
    data: Any = None
    error: ToolError | None = None
    approval: dict[str, Any] | None = None
    audit_id: str | None = None
    cache_hit: bool = False


Handler = Callable[[BaseModel, ToolExecutionContext], Any]
Redactor = Callable[[BaseModel], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    id: str
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    handler: Handler
    risk_level: RiskLevel = RiskLevel.LOW
    required_permission: str = ""
    allowed_agents: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: int = 30
    enabled: bool = True
    category: str = "builtin"
    source: str = "builtin"
    version: str = "1"
    redact_input: Redactor | None = None
    cache_ttl_seconds: int = 0

    def catalog_item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "required_permission": self.required_permission,
            "allowed_agents": sorted(self.allowed_agents),
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
            "category": self.category,
            "source": self.source,
            "version": self.version,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "input_schema": self.input_schema.model_json_schema(),
            "output_schema": self.output_schema.model_json_schema(),
        }


class CatalogRagInput(StrictModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=8, ge=1, le=20)


class CatalogRagOutput(StrictModel):
    chunks: list[dict[str, Any]] = Field(default_factory=list)


class ReadonlySqlInput(StrictModel):
    sql: str = Field(min_length=1, max_length=20_000)
    source_id: str | None = Field(default=None, max_length=200)


class ReadonlySqlOutput(StrictModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)


class MemorySearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=5, ge=1, le=5)


class MemorySearchOutput(StrictModel):
    memories: list[dict[str, Any]] = Field(default_factory=list)


class MemoryWriteInput(StrictModel):
    memory_type: str
    key: str
    value: str = Field(min_length=1, max_length=500)
    scope: str = "user"
    confidence: float = Field(default=1.0, ge=0.75, le=1)


class MemoryForgetInput(StrictModel):
    memory_type: str
    key: str
    scope: str = "user"


class MemoryMutationOutput(StrictModel):
    outcomes: list[dict[str, Any]] = Field(default_factory=list)


class HighRiskOperationInput(StrictModel):
    operation: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=500)
    target: str = Field(default="", max_length=200)
    estimated_effects: list[str] = Field(default_factory=list, max_length=10)


class HighRiskOperationOutput(StrictModel):
    accepted: bool
    message: str


SqlScalar = str | int | float | bool | None


class SqlWritePredicate(StrictModel):
    column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    value: SqlScalar = None


class SqlWriteDraft(StrictModel):
    operation: Literal["insert", "update", "delete"]
    table: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    values: dict[str, SqlScalar] = Field(default_factory=dict, max_length=50)
    where: list[SqlWritePredicate] = Field(default_factory=list, max_length=20)
    summary: str = Field(min_length=1, max_length=500)


class SqlWriteInput(SqlWriteDraft):
    estimated_rows: int = Field(ge=0, le=100)


class SqlWriteOutput(StrictModel):
    accepted: bool
    message: str
    operation: Literal["insert", "update", "delete"]
    table: str
    affected_rows: int = Field(ge=0)
