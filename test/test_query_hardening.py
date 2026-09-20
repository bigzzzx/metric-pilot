import asyncio

import pytest

from backend import knowledge_base, rag
from backend.evaluation.environment import evaluation_environment
from backend.hybrid_retrieval import exact_identifier_rank
from backend.query_fast_path import has_explicit_physical_field_intent, qualified_table_names
from backend.query_quality.evidence import executable_planning_rag, metric_context
from backend.query_quality.planner_context import sql_referenced_rag_tables
from backend.query_quality.source_resolution import resolve_source_id
from backend.semantic_metrics.resolver import (
    resolve_metric_query,
    resolve_metric_references,
)
from backend.semantic_metrics.repository import metric_records
from backend.schemas import QueryPlanOutput


@pytest.fixture
def isolated(tmp_path):
    with evaluation_environment(tmp_path):
        yield


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "把订单与客户关联，分区域给出 PAID 订单量及 ods.orders.order_amount 的总和。",
            True,
        ),
        (
            "直接汇总 ads.daily_trade 的原始物理字段：gmv、paid_order_count。",
            True,
        ),
        ("查询 GMV 怎么算", False),
        ("请按原始字段统计 GMV", False),
    ],
)
def test_physical_intent_requires_explicit_table_binding(query, expected):
    assert has_explicit_physical_field_intent(query) is expected


def test_physical_queries_bypass_metric_identity_but_business_metric_does_not(isolated):
    physical = "直接汇总 ads.daily_trade 的原始物理字段：gmv、paid_order_count。"
    assert resolve_metric_query(physical, "eval-analyst").status == "unmatched"
    assert metric_context(physical, [], "eval-analyst") == []
    assert resolve_metric_query("查询 GMV 怎么算", "eval-analyst").status != "unmatched"


def test_qualified_column_yields_only_its_own_table_binding():
    assert qualified_table_names(
        "汇总 ods.orders.order_amount 并关联 ods.customers"
    ) == {"ods.orders", "ods.customers"}


def test_metric_references_are_all_or_nothing(isolated):
    metric = next(
        item for item in metric_records("eval-analyst")
        if item.can_query and item.certification_status == "certified"
        and item.contract and item.contract.status == "active"
    )
    valid = {"id": metric.document_id, "version": metric.version}
    assert resolve_metric_references([valid], "eval-analyst").status == "matched"
    missing = {"id": "catalog:metric:not-visible", "version": "0" * 64}
    denied = resolve_metric_references([valid, missing], "eval-analyst")
    assert denied.status == "denied"
    assert "不会回退" in denied.message


def test_metric_reference_version_is_revalidated(isolated):
    metric = next(
        item for item in metric_records("eval-analyst")
        if item.can_query and item.certification_status == "certified"
        and item.contract and item.contract.status == "active"
    )
    valid = {"id": metric.document_id, "version": metric.version}
    stale = {**valid, "version": "0" * 64}
    assert resolve_metric_references([stale], "eval-analyst").status == "unsupported"


def test_ad_hoc_planning_hides_metrics_without_active_contract(isolated):
    rag_rows = [
        {"id": "catalog:metric:metric-gmv", "kind": "metric"},
        {"id": "catalog:metric-wiki:metric-gmv", "kind": "metric_wiki"},
        {"id": "catalog:asset:orders", "kind": "table"},
    ]
    filtered = executable_planning_rag(rag_rows, "eval-analyst")
    assert [item["id"] for item in filtered] == ["catalog:asset:orders"]
    assert metric_context("GMV", rag_rows, "eval-analyst") == []


def _schema(name, source_id="source-a", namespace="ods"):
    return {
        "asset_id": source_id + ":" + namespace + "." + name,
        "name": name,
        "namespace": namespace,
        "source_id": source_id,
        "columns": [],
    }


@pytest.mark.parametrize(
    ("planner_source", "reason"),
    [
        (None, "inferred_from_verified_sql_tables"),
        ("wrong-source", "corrected_from_verified_sql_tables"),
        ("source-a", "planner_source_verified"),
    ],
)
def test_source_is_bound_only_from_tables_used_by_parsed_sql(planner_source, reason):
    schemas = {"tables": [_schema("orders"), _schema("unused", "source-b")]}
    result = resolve_source_id(
        "SELECT COUNT(*) FROM ods.orders", planner_source, schemas,
    )
    assert result.valid
    assert result.source_id == "source-a"
    assert result.reason == reason


def test_source_resolution_rejects_unknown_ambiguous_and_cross_source_tables():
    assert not resolve_source_id(
        "SELECT * FROM ods.missing", None, {"tables": [_schema("orders")]},
    ).valid
    ambiguous = {"tables": [_schema("orders", "source-a"), _schema("orders", "source-b")]}
    assert resolve_source_id("SELECT * FROM orders", None, ambiguous).reason == "sql_table_mapping_ambiguous"
    cross = {"tables": [_schema("orders"), _schema("customers", "source-b")]}
    resolution = resolve_source_id(
        "SELECT * FROM ods.orders o JOIN ods.customers c ON c.id=o.id", None, cross,
    )
    assert resolution.reason == "cross_source_sql"


def test_model_sql_can_request_only_exact_tables_already_in_authorized_rag():
    rag_rows = [
        {
            "id": "orders",
            "kind": "table",
            "asset_ids": ["asset-orders"],
            "source_id": "source-a",
            "namespace": "ods",
            "physical_name": "orders",
        },
        {
            "id": "other-orders",
            "kind": "table",
            "asset_ids": ["asset-other"],
            "source_id": "source-b",
            "namespace": "other",
            "physical_name": "orders",
        },
    ]
    assert sql_referenced_rag_tables(
        "SELECT COUNT(*) FROM ods.orders", rag_rows,
    ) == ["asset-orders"]
    assert sql_referenced_rag_tables(
        "SELECT COUNT(*) FROM missing.orders", rag_rows,
    ) == []


def test_exact_identifier_matching_keeps_hyphens_underscores_and_boundaries():
    documents = [
        {"id": "exact", "content": "故障码 AT-42，字段 paid_order_count。"},
        {"id": "suffix", "content": "故障码 AT-420，字段 paid_order_count_old。"},
        {
            "id": "structured",
            "content": "结构化目录记录",
            "namespace": "ads",
            "physical_name": "daily_trade",
            "source_id": "warehouse-prod",
        },
    ]
    assert [item["id"] for item in exact_identifier_rank("排查 AT-42", documents, 5)] == ["exact"]
    assert [item["id"] for item in exact_identifier_rank("paid_order_count", documents, 5)] == ["exact"]
    assert [item["id"] for item in exact_identifier_rank("warehouse-prod", documents, 5)] == ["structured"]


def test_knowledge_reranker_receives_more_than_ten_fused_candidates(monkeypatch):
    rows = [
        {"id": str(index), "content": "订单退款规则", "title": str(index)}
        for index in range(18)
    ]
    seen = []
    monkeypatch.setattr(knowledge_base, "init_knowledge_schema", lambda: None)
    monkeypatch.setattr(knowledge_base, "using_milvus", lambda: False)
    monkeypatch.setattr(knowledge_base, "_sqlite_retrieve", lambda *_args: rows)
    monkeypatch.setattr(knowledge_base, "_authorized_chunks", lambda _user: rows)
    monkeypatch.setattr(
        knowledge_base,
        "rerank_or_fallback",
        lambda _query, candidates, limit: seen.extend(candidates) or candidates[:limit],
    )
    knowledge_base.retrieve("订单退款规则", limit=12, user_id="user")
    assert len(seen) > 10


def test_relation_evidence_includes_both_endpoints_without_unrelated_edge(monkeypatch):
    documents = [
        {"id": "orders", "kind": "table", "title": "ods.orders", "content": "PAID 订单", "keywords": [], "asset_ids": ["orders"]},
        {"id": "customers", "kind": "table", "title": "ods.customers", "content": "客户区域 region", "keywords": [], "asset_ids": ["customers"]},
        {"id": "items", "kind": "table", "title": "ods.order_items", "content": "订单商品明细", "keywords": [], "asset_ids": ["items"]},
        {"id": "order-customer", "kind": "relation", "title": "订单客户关联", "content": "订单关联客户区域", "keywords": ["关联"], "asset_ids": ["orders", "customers"]},
        {"id": "order-items", "kind": "relation", "title": "订单明细关联", "content": "订单关联商品明细", "keywords": ["关联"], "asset_ids": ["orders", "items"]},
    ]
    monkeypatch.setattr(rag, "authorized_catalog_documents", lambda _user: documents)
    monkeypatch.setattr(rag, "retrieve_catalog_vectors", lambda *_args: documents[:2])
    monkeypatch.setattr(rag, "retrieve_knowledge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag, "rerank_or_fallback", lambda _query, rows, limit: rows[:limit])
    result = rag.retrieve(
        "把订单与客户关联，分区域给出 PAID 订单量及 ods.orders.order_amount 的总和",
        limit=8,
        user_id="user",
    )
    ids = {item["id"] for item in result}
    assert {"order-customer", "orders", "customers"} <= ids
    assert "order-items" not in ids


def test_qualified_fast_path_uses_structured_identity_not_full_text(monkeypatch):
    documents = [{
        "id": "misleading",
        "kind": "table",
        "title": "别的表",
        "content": "说明中提到了 ods.orders，但这不是该表。",
        "keywords": ["ods.orders"],
        "asset_ids": ["customers"],
        "namespace": "ods",
        "physical_name": "customers",
    }]
    monkeypatch.setattr(rag, "authorized_catalog_documents", lambda _user: documents)
    monkeypatch.setattr(
        rag,
        "retrieve_catalog_vectors",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dense retrieval must be skipped")),
    )
    monkeypatch.setattr(
        rag,
        "retrieve_knowledge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("knowledge retrieval must be skipped")),
    )
    assert rag.retrieve("查询 ods.orders 一共有多少行", user_id="user") == []


def test_planner_metric_reference_failure_does_not_fall_back_to_sql(isolated, monkeypatch):
    from backend import agent
    from backend.tools.models import ToolResult, ToolStatus

    schema = _schema("orders")

    def invoke(tool_id, _payload, context):
        assert tool_id == "catalog_schema"
        return ToolResult(
            status=ToolStatus.SUCCESS,
            tool_id=tool_id,
            call_id=context.call_id,
            data={"tables": [schema]},
        )

    async def complete(*_args):
        return QueryPlanOutput(
            answer="planned",
            sql="SELECT COUNT(*) FROM ods.orders",
            source_id="source-a",
            metric_refs=[{"id": "catalog:metric:not-visible", "version": "0" * 64}],
        )

    monkeypatch.setattr(agent, "candidate_tables", lambda *_args: ["ods.orders"])
    monkeypatch.setattr(agent.tool_executor, "invoke", invoke)
    monkeypatch.setattr(agent, "complete_structured", complete)
    result = asyncio.run(agent.data_query_agent({
        "message": "query",
        "history": [],
        "user_id": "eval-analyst",
        "rag": [],
        "route": "data_query",
    }))
    assert result["plan"]["sql"] is None
    assert result["metric_resolution"]["status"] == "denied"
    assert "不会回退" in result["final"]["answer"]


def test_valid_single_metric_reference_compiles_contract_before_model_sql_source(isolated, monkeypatch):
    from backend import agent

    metric = next(
        item for item in metric_records("eval-analyst")
        if item.can_query and item.certification_status == "certified"
        and item.contract and item.contract.status == "active"
    )

    async def complete(*_args):
        return QueryPlanOutput(
            answer="model sql is ignored",
            sql="SELECT * FROM missing.table",
            source_id="wrong-source",
            metric_refs=[{"id": metric.document_id, "version": metric.version}],
        )

    async def compile_contract(state):
        assert state["metric_resolution"]["status"] == "matched"
        assert state["source_resolution_reason"] == "metric_reference_validated"
        return {**state, "compiled_contract": True}

    monkeypatch.setattr(agent, "candidate_tables", lambda *_args: [])
    monkeypatch.setattr(agent, "complete_structured", complete)
    monkeypatch.setattr(agent, "metric_plan", compile_contract)
    result = asyncio.run(agent.data_query_agent({
        "message": "query",
        "history": [],
        "user_id": "eval-analyst",
        "rag": [],
        "route": "data_query",
    }))
    assert result["compiled_contract"]


def test_execution_revalidates_source_before_sql_tool(monkeypatch):
    from backend import agent

    monkeypatch.setattr(
        agent.tool_executor,
        "invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SQL tool must not run after a source mismatch")
        ),
    )
    state = {
        "plan": {"sql": "SELECT COUNT(*) FROM ods.orders", "source_id": "wrong"},
        "verified_schemas": {"tables": [_schema("orders")]},
        "user_id": "user",
        "route": "data_query",
    }
    result = agent.query_tool(state)
    assert result["query_terminal"]
    assert result["source_resolution_reason"].startswith("execution_revalidation_failed:")


def test_missing_mcp_tool_falls_back_only_for_data_query(monkeypatch):
    from backend import agent
    from backend.tools.models import ToolError, ToolResult, ToolStatus

    async def missing(*_args, **_kwargs):
        return ToolResult(
            status=ToolStatus.FAILED,
            tool_id="mcp:stale:lookup_metric",
            call_id="call",
            error=ToolError(code="tool_not_found", message="missing"),
        )

    monkeypatch.setattr(agent.async_executor, "ainvoke", missing)
    state = {
        "message": "query",
        "history": [],
        "user_id": "user",
        "route": "data_query",
        "mcp_tool_call": {"tool_id": "mcp:stale:lookup_metric", "arguments": {}},
    }
    result = asyncio.run(agent.execute_mcp_tool(state))
    assert result["mcp_fallback"]
    assert result["mcp_tool_call"] is None
    assert "final" not in result
    assert agent.route_after_mcp_tool(result) == "metric_router"


def test_live_no_cache_disables_and_restores_agent_and_tool_caches(monkeypatch):
    from backend import agent
    from backend.evaluation import runner
    from backend.evaluation.cases import CASES

    agent_cache = object()
    tool_cache = object()
    monkeypatch.setattr(agent, "agent_cache", agent_cache)
    monkeypatch.setattr(agent.tool_executor, "cache", tool_cache)
    monkeypatch.setattr(runner, "_model_runtime", lambda: {"model": "test"})

    async def live_case(case):
        assert agent.agent_cache is None
        assert agent.tool_executor.cache is None
        return {"id": case.id, "status": "passed", "duration_ms": 1}

    monkeypatch.setattr(runner, "live_case", live_case)
    report = asyncio.run(runner.evaluate("live", {CASES[0].id}, no_cache=True))
    assert report["cache_mode"] == "disabled"
    assert agent.agent_cache is agent_cache
    assert agent.tool_executor.cache is tool_cache
