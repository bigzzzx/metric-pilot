"""Registration of built-in Data Agent tools."""
from __future__ import annotations

from backend.memory_store import apply_memory_actions
from fastapi import HTTPException
from backend.rag import retrieve
from backend.tools.errors import ToolRuntimeError
from backend.tools.memory_tool import search_long_term_memory
from backend.tools.models import (
    CatalogRagInput,
    CatalogRagOutput,
    HighRiskOperationInput,
    HighRiskOperationOutput,
    MemoryForgetInput,
    MemoryMutationOutput,
    MemorySearchInput,
    MemorySearchOutput,
    MemoryWriteInput,
    ReadonlySqlInput,
    ReadonlySqlOutput,
    RiskLevel,
    SqlWriteInput,
    SqlWriteOutput,
    ToolExecutionContext,
    ToolSpec,
)
from backend.tools.registry import ToolRegistry, registry
from backend.tools.sql_tool import run_readonly_sql
from backend.tools.sql_write_tool import execute_sql_write
from backend.query_quality.schema_lookup import SchemaLookupInput, SchemaLookupOutput, lookup_schema


def _catalog_rag(payload: CatalogRagInput, context: ToolExecutionContext) -> dict:
    return {"chunks": retrieve(payload.query, limit=payload.limit, fallback=False, user_id=context.user_id)}


def _readonly_sql(payload: ReadonlySqlInput, context: ToolExecutionContext) -> dict:
    try:
        return {"rows": run_readonly_sql(payload.sql, context.user_id, payload.source_id)}
    except HTTPException as exc:
        code = "permission_denied" if exc.status_code == 403 else getattr(exc, "code", "sql_error")
        raise ToolRuntimeError(code, str(exc.detail)) from exc


def _memory_search(payload: MemorySearchInput, context: ToolExecutionContext) -> dict:
    return {"memories": search_long_term_memory(context.user_id, payload.query, payload.limit)}


def _memory_write(payload: MemoryWriteInput, context: ToolExecutionContext) -> dict:
    actions = [{
        "operation": "save", "memory_type": payload.memory_type, "key": payload.key,
        "value": payload.value, "scope": payload.scope, "confidence": payload.confidence,
        "sensitive": False,
    }]
    return {"outcomes": apply_memory_actions(
        context.user_id, context.conversation_id, None, actions
    )}


def _memory_forget(payload: MemoryForgetInput, context: ToolExecutionContext) -> dict:
    actions = [{
        "operation": "forget", "memory_type": payload.memory_type, "key": payload.key,
        "value": None, "scope": payload.scope, "confidence": 1.0, "sensitive": False,
    }]
    return {"outcomes": apply_memory_actions(
        context.user_id, context.conversation_id, None, actions
    )}


def _unavailable_high_risk(_payload: HighRiskOperationInput, _context: ToolExecutionContext) -> dict:
    raise ToolRuntimeError(
        "tool_unavailable",
        "该高风险工具的审批与审计能力已经就绪，但尚未连接可执行的数据源或 ETL 平台。",
    )


def _sql_write(payload: SqlWriteInput, context: ToolExecutionContext) -> dict:
    return execute_sql_write(payload, context)


def _render_value(value) -> str:
    rendered = repr(value)
    return rendered if len(rendered) <= 80 else rendered[:77] + "..."


def _sql_write_summary(payload: SqlWriteInput) -> dict:
    changes = ", ".join(
        f"{column} = {_render_value(value)}" for column, value in payload.values.items()
    )
    conditions = " AND ".join(
        f"{item.column} = {_render_value(item.value)}" for item in payload.where
    )
    effects = [
        f"操作：{payload.operation.upper()}，预计影响 {payload.estimated_rows} 行",
    ]
    if changes:
        effects.append("写入字段：" + changes)
    if conditions:
        effects.append("WHERE：" + conditions)
    return {
        "operation": payload.operation,
        "summary": payload.summary,
        "target": f"本地演示库 · {payload.table}",
        "estimated_effects": effects,
    }


def _risk_summary(payload: HighRiskOperationInput) -> dict:
    return {
        "operation": payload.operation,
        "summary": payload.summary,
        "target": payload.target,
        "estimated_effects": payload.estimated_effects,
    }


def register_builtin_tools(target: ToolRegistry | None = None) -> ToolRegistry:
    target = target or registry
    if target.all():
        return target
    for spec in (
        ToolSpec(
            id="catalog_schema", name="授权表结构查询",
            description="核实少量授权表的完整可查询字段，不读取样例数据。",
            input_schema=SchemaLookupInput, output_schema=SchemaLookupOutput,
            handler=lookup_schema, allowed_agents=frozenset({"data-query-agent"}),
            cache_ttl_seconds=300,
        ),
        ToolSpec(
            id="catalog_rag", name="元数据 RAG",
            description="检索当前用户有权发现的表、字段、关系、指标与知识库内容。",
            input_schema=CatalogRagInput, output_schema=CatalogRagOutput,
            handler=_catalog_rag, allowed_agents=frozenset({"data-query-agent", "data-analysis-agent"}),
        ),
        ToolSpec(
            id="readonly_sql", name="只读 SQL",
            description="在用户有权限的数据资产上执行一条 SELECT/WITH 查询。",
            input_schema=ReadonlySqlInput, output_schema=ReadonlySqlOutput,
            handler=_readonly_sql, allowed_agents=frozenset({"data-query-agent"}),
            cache_ttl_seconds=60,
        ),
        ToolSpec(
            id="memory_search", name="长期记忆检索",
            description="仅检索当前登录用户自己的长期记忆。",
            input_schema=MemorySearchInput, output_schema=MemorySearchOutput,
            handler=_memory_search,
            allowed_agents=frozenset({"general-agent", "data-query-agent", "data-analysis-agent"}),
        ),
        ToolSpec(
            id="memory_write", name="长期记忆写入",
            description="保存用户明确表达的非敏感长期偏好或约定。",
            input_schema=MemoryWriteInput, output_schema=MemoryMutationOutput,
            handler=_memory_write, risk_level=RiskLevel.MEDIUM,
            allowed_agents=frozenset({"general-agent", "data-query-agent", "data-analysis-agent"}),
        ),
        ToolSpec(
            id="memory_forget", name="长期记忆删除",
            description="按用户明确请求删除一条长期记忆。",
            input_schema=MemoryForgetInput, output_schema=MemoryMutationOutput,
            handler=_memory_forget, risk_level=RiskLevel.MEDIUM,
            allowed_agents=frozenset({"general-agent", "data-query-agent", "data-analysis-agent"}),
        ),
        ToolSpec(
            id="sql_write", name="SQL 数据写入",
            description="在本地演示库执行参数化 INSERT/UPDATE/DELETE；必须具有写入权限并逐次审批。",
            input_schema=SqlWriteInput, output_schema=SqlWriteOutput,
            handler=_sql_write, risk_level=RiskLevel.HIGH,
            required_permission="data.write", allowed_agents=frozenset({"data-query-agent"}),
            redact_input=_sql_write_summary,
        ),
        ToolSpec(
            id="etl_execute", name="ETL 任务执行",
            description="启动或修改已登记的 ETL 任务；必须具有执行权限并逐次审批。",
            input_schema=HighRiskOperationInput, output_schema=HighRiskOperationOutput,
            handler=_unavailable_high_risk, risk_level=RiskLevel.HIGH,
            required_permission="etl.execute", allowed_agents=frozenset({"data-query-agent"}),
            redact_input=_risk_summary,
        ),
    ):
        target.register(spec)
    return target


register_builtin_tools()
