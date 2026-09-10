"""Execute golden SQL and optional full-agent checks against real PostgreSQL."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
import math
from pathlib import Path
import sqlite3
import statistics
import tempfile
import time
from typing import Any
from unittest.mock import patch

from fastapi import HTTPException

from backend.evaluation.postgresql_cases import CASES, MODEL_CASE_IDS, PostgreSQLCase
from backend.evaluation.postgresql_stability_cases import PostgreSQLStabilityCase, STABILITY_CASES


ACCEPTANCE_SOURCE_ID = "pg-acceptance-source"
ACTORS = {"admin": "pg-acceptance-admin", "ecommerce": "pg-acceptance-ecommerce"}


def _canonical(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, dict):
        return tuple(sorted((key, _canonical(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canonical(item) for item in value)
    return value


def exact_rows_equal(actual: list[dict], expected: tuple[dict, ...]) -> bool:
    """Compare row multisets while retaining column names and duplicate rows."""
    return Counter(_canonical(row) for row in actual) == Counter(_canonical(row) for row in expected)


def semantic_rows_equal(actual: list[dict], expected: tuple[dict, ...]) -> bool:
    """Require every golden value while allowing aliases and extra result columns."""
    if len(actual) != len(expected):
        return False

    def row_values(row: dict) -> Counter:
        return Counter(f"{type(value).__name__}:{value}" for value in row.values())

    remaining = [row_values(row) for row in actual]
    for expected_row in expected:
        required = row_values(expected_row)
        for index, candidate in enumerate(remaining):
            if all(candidate[value] >= count for value, count in required.items()):
                remaining.pop(index)
                break
        else:
            return False
    return not remaining


def model_rows_equal(case: PostgreSQLCase, actual: list[dict]) -> bool:
    if semantic_rows_equal(actual, case.expected):
        return True
    # A model may reasonably return a daily breakdown for a monthly question.
    # Accept it only when the deterministic roll-up exactly equals the golden row.
    if case.id not in {"august_paid", "september_followup"} or len(case.expected) != 1:
        return False
    try:
        count = sum(int(row[next(key for key in row if "count" in key.lower() or key.lower() in {"n", "orders"})]) for row in actual)
        amount = sum(float(row[next(key for key in row if any(token in key.lower() for token in ("amount", "gmv")))]) for row in actual)
    except (StopIteration, TypeError, ValueError):
        return False
    expected = case.expected[0]
    return count == int(expected["n"]) and math.isclose(amount, float(expected["amount"]), abs_tol=0.005)


def _configured_source(source_id: str | None = None):
    from backend.core import db
    con = db()
    try:
        rows = con.execute("SELECT * FROM data_sources WHERE kind='postgresql' AND enabled=1").fetchall()
    finally:
        con.close()
    selected = [row for row in rows if not source_id or row["id"] == source_id]
    if len(selected) != 1:
        raise ValueError("请使用 --source-id 选择唯一一个已启用的 PostgreSQL 数据源。")
    return selected[0]


@contextmanager
def acceptance_environment(source_id: str | None = None):
    """Mirror real discovery into a temporary catalog and reuse only the connector."""
    from backend.core import DATA_DIR
    from backend.data_sources.service import connector_from_row
    from backend.main import init_storage
    from backend.catalog import publications, rag_documents, retrieval, service
    from backend.data_sources import postgresql_query
    from backend.query_quality import schema_lookup
    from backend.tools import sql_tool

    source = _configured_source(source_id)
    connector = connector_from_row(source)
    discovery = connector.discover()
    with tempfile.TemporaryDirectory(prefix="postgres-acceptance-", dir=DATA_DIR) as folder:
        database = Path(folder) / "catalog.db"

        def factory():
            connection = sqlite3.connect(database, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            return connection

        init_storage(factory)
        con = factory()
        try:
            con.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES (?,?,?,1)",
                        (ACTORS["admin"], ACTORS["admin"], "not-a-login"))
            con.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES (?,?,?,1)",
                        (ACTORS["ecommerce"], ACTORS["ecommerce"], "not-a-login"))
            con.execute("INSERT INTO catalog_user_roles(user_id,role_id) VALUES (?,'role-platform-admin')",
                        (ACTORS["admin"],))
            con.execute("INSERT INTO catalog_user_roles(user_id,role_id) VALUES (?,'role-data-analyst')",
                        (ACTORS["ecommerce"],))
            con.execute("INSERT INTO catalog_group_memberships(group_id,user_id) VALUES ('group-ecommerce-analysts',?)",
                        (ACTORS["ecommerce"],))
            con.execute("""INSERT INTO data_sources(
                id,name,kind,endpoint,catalog_name,auth_type,credential,enabled,username
            ) VALUES (?,?, 'postgresql','unused','unused','none','',1,'unused')""",
                        (ACCEPTANCE_SOURCE_ID, "PostgreSQL acceptance"))
            con.commit()
        finally:
            con.close()

        with ExitStack() as stack:
            for module in (service, publications, rag_documents, retrieval, schema_lookup, sql_tool):
                stack.enter_context(patch.object(module, "db", factory))
            stack.enter_context(patch.object(postgresql_query, "connector_from_row", lambda _row: connector))
            publications.import_discovery_as_draft(ACCEPTANCE_SOURCE_ID, discovery, ACTORS["admin"])
            con = factory()
            try:
                con.execute("UPDATE catalog_assets SET publish_status='published' WHERE source_id=?",
                            (ACCEPTANCE_SOURCE_ID,))
                con.execute("UPDATE catalog_relationships SET status='published'")
                con.commit()
            finally:
                con.close()
            yield {"factory": factory, "source": source, "discovery": discovery}


def _run_direct_case(case: PostgreSQLCase, factory) -> dict[str, Any]:
    from backend.tools.sql_tool import run_readonly_sql
    started = time.perf_counter()
    disabled = case.outcome == "disabled"
    if disabled:
        con = factory()
        try:
            con.execute("UPDATE data_sources SET enabled=0 WHERE id=?", (ACCEPTANCE_SOURCE_ID,))
            con.commit()
        finally:
            con.close()
    try:
        rows = run_readonly_sql(case.sql, ACTORS[case.actor], ACCEPTANCE_SOURCE_ID)
        if case.outcome == "rows":
            passed, detail = exact_rows_equal(rows, case.expected), "golden_rows"
        elif case.outcome == "row_limit":
            passed = len(rows) == 100 and rows[0].get("order_id") == 1 and rows[-1].get("order_id") == 100
            detail = "bounded_to_100"
        else:
            passed, detail = False, "expected_denial"
        return {"id": case.id, "category": case.category, "status": "passed" if passed else "failed",
                "duration_ms": round((time.perf_counter() - started) * 1000, 2), "check": detail,
                "actual": rows if not passed else None}
    except HTTPException as exc:
        expected_error = case.outcome in {"denied", "disabled"} and exc.status_code == 403
        return {"id": case.id, "category": case.category,
                "status": "passed" if expected_error else "error",
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "check": "permission_guard", "error_code": getattr(exc, "code", None),
                "http_status": exc.status_code,
                **({"error": str(exc.detail)} if not expected_error else {})}
    except Exception as exc:
        return {"id": case.id, "category": case.category, "status": "error",
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "error_type": type(exc).__name__}
    finally:
        if disabled:
            con = factory()
            try:
                con.execute("UPDATE data_sources SET enabled=1 WHERE id=?", (ACCEPTANCE_SOURCE_ID,))
                con.commit()
            finally:
                con.close()


def _summary(results: list[dict]) -> dict[str, Any]:
    by_category: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        by_category[result["category"]].append(result)
    durations = [result["duration_ms"] for result in results]
    sorted_durations = sorted(durations)
    percentile_index = max(0, math.ceil(len(sorted_durations) * 0.95) - 1)
    return {
        "total": len(results),
        "passed": sum(result["status"] == "passed" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "errors": sum(result["status"] == "error" for result in results),
        "accuracy": round(sum(result["status"] == "passed" for result in results) / len(results), 4),
        "median_duration_ms": round(statistics.median(durations), 2),
        "p95_duration_ms": sorted_durations[percentile_index],
        "categories": {
            category: {"passed": sum(item["status"] == "passed" for item in items), "total": len(items)}
            for category, items in sorted(by_category.items())
        },
    }


def run_direct_acceptance(source_id: str | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    with acceptance_environment(source_id) as environment:
        results = []
        for case in CASES:
            result = _run_direct_case(case, environment["factory"])
            results.append(result)
            print(f"direct {case.id}: {result['status']}", flush=True)
        summary = _summary(results)
        return {"suite_version": "postgresql-2026-09-10.1", "mode": "direct",
                "source_id": environment["source"]["id"], "source_name": environment["source"]["name"],
                "discovered_tables": len(environment["discovery"].get("tables", [])),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                **summary, "results": results}


def _platform_admin() -> str:
    from backend.core import db
    con = db()
    try:
        row = con.execute("""SELECT u.id FROM users u JOIN catalog_user_roles r ON r.user_id=u.id
                           WHERE r.role_id='role-platform-admin' AND u.is_active=1 ORDER BY u.created_at LIMIT 1""").fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError("没有可用于只读模型验收的平台管理员账号。")
    return row["id"]


def _platform_ecommerce_user() -> str:
    """Pick an active analyst who does not inherit the platform-admin role."""
    from backend.core import db
    con = db()
    try:
        row = con.execute("""SELECT DISTINCT u.id FROM users u
                           JOIN catalog_user_roles analyst ON analyst.user_id=u.id
                           JOIN catalog_group_memberships membership ON membership.user_id=u.id
                           WHERE analyst.role_id='role-data-analyst'
                             AND membership.group_id='group-ecommerce-analysts'
                             AND u.is_active=1
                             AND NOT EXISTS (
                               SELECT 1 FROM catalog_user_roles admin
                               WHERE admin.user_id=u.id AND admin.role_id='role-platform-admin'
                             )
                           ORDER BY u.created_at LIMIT 1""").fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError("没有可用于权限验收的普通电商分析师账号。")
    return row["id"]


def _rag_trace(items: list[dict]) -> list[dict[str, Any]]:
    return [{
        "rank": index,
        "id": item.get("id"),
        "kind": item.get("kind"),
        "title": item.get("title"),
        "asset_ids": item.get("asset_ids") or [],
        "source_id": item.get("source_id"),
        "score": item.get("score"),
        "content_excerpt": str(item.get("content") or "")[:180],
    } for index, item in enumerate(items[:12], 1)]


def _verified_table_trace(schemas: dict | None) -> list[dict[str, Any]]:
    traced = []
    for table in (schemas or {}).get("tables", []):
        namespace = table.get("namespace") or table.get("schema")
        name = table.get("physical_name") or table.get("name")
        traced.append({
            "asset_id": table.get("asset_id"),
            "table": ".".join(part for part in (namespace, name) if part),
            "source_id": table.get("source_id"),
            "columns": [column.get("name") for column in table.get("columns", [])[:40]],
        })
    return traced


def _tool_trace(tool_result: dict | None, rows: list[dict]) -> dict[str, Any] | None:
    if not tool_result:
        return None
    data = tool_result.get("data") or {}
    error = tool_result.get("error")
    return {
        "status": tool_result.get("status"),
        "row_count": len(rows),
        "result_preview": rows[:3],
        "error": error,
        "data_keys": sorted(data) if isinstance(data, dict) else [],
    }


def model_diagnostics(state: dict) -> dict[str, Any]:
    """Return a bounded, credential-free trace suitable for persisted reports."""
    rows = state.get("result") or []
    return {
        "route": state.get("route"),
        "route_source": state.get("route_source"),
        "contextual_follow_up": bool(state.get("contextual_follow_up")),
        "retrieved_assets": _rag_trace(state.get("rag") or []),
        "verified_tables": _verified_table_trace(state.get("verified_schemas")),
        "plan": state.get("plan"),
        "sql": state.get("sql"),
        "query_error": state.get("query_error"),
        "permission_error": state.get("permission_error"),
        "tool": _tool_trace(state.get("tool_result"), rows),
    }


async def _run_model_case(case: PostgreSQLCase, user_id: str, source_id: str) -> dict[str, Any]:
    from backend.agent import run_agent_stream
    started = time.perf_counter()
    first_token_ms = None
    nodes = []
    state = None
    history = [dict(item) for item in case.history]
    for item in history:
        item["content"] = item.get("content", "").replace(
            "b882d48b-8a6e-4ae4-8bfa-a7baeaf05c0d", source_id)
    question = f"使用 PostgreSQL 测试数仓回答。{case.question}"
    try:
        async with asyncio.timeout(120):
            async for event in run_agent_stream(
                question, "data-query-agent", history, user_id=user_id,
                conversation_id=f"postgres-eval-{case.id}",
            ):
                if event["event"] == "delta" and first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                elif event["event"] == "node_completed":
                    nodes.append({key: event.get(key) for key in ("node", "duration_ms", "status")})
                elif event["event"] == "done":
                    state = event["state"]
        if state is None:
            raise ValueError("Agent did not produce terminal state")
        rows = state.get("result") or []
        passed = bool(state.get("sql")) and model_rows_equal(case, rows)
        return {"id": case.id, "category": case.category,
                "status": "passed" if passed else "failed", "first_token_ms": first_token_ms,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "sql": state.get("sql"), "actual": None if passed else rows,
                "answer": state.get("final", {}).get("answer", ""), "nodes": nodes,
                "diagnostics": model_diagnostics(state)}
    except Exception as exc:
        return {"id": case.id, "category": case.category, "status": "error",
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "first_token_ms": first_token_ms, "error_type": type(exc).__name__, "nodes": nodes}


async def run_model_acceptance(source_id: str | None = None) -> dict[str, Any]:
    source = _configured_source(source_id)
    user_id = _platform_admin()
    selected = [case for case in CASES if case.id in MODEL_CASE_IDS]
    results = []
    for case in selected:
        result = await _run_model_case(case, user_id, source["id"])
        results.append(result)
        print(f"model {case.id}: {result['status']}", flush=True)
    summary = _summary(results)
    first_tokens = [item["first_token_ms"] for item in results if item.get("first_token_ms") is not None]
    return {"suite_version": "postgresql-2026-09-10.1", "mode": "full_langgraph_llm",
            "source_id": source["id"], "source_name": source["name"], **summary,
            "median_first_token_ms": round(statistics.median(first_tokens), 2) if first_tokens else None,
            "results": results}


def _stability_passed(case: PostgreSQLStabilityCase, state: dict) -> tuple[bool, str]:
    sql = str(state.get("sql") or "").strip()
    rows = state.get("result") or []
    answer = str((state.get("final") or {}).get("answer") or "")
    if case.expectation == "rows":
        golden = next(item for item in CASES if item.id == case.base_case_id)
        return bool(sql) and model_rows_equal(golden, rows), "golden_rows"
    if case.expectation == "clarification":
        markers = ("请明确", "请提供", "请确认", "需要", "口径", "时间", "范围", "哪个", "哪种")
        return not sql and any(marker in answer for marker in markers), "clarification_without_sql"
    permission = state.get("permission_error") or {}
    markers = ("权限", "无权", "授权", "未找到可用数据资产", "无法访问")
    safe = not rows and (not sql or bool(permission))
    return safe and any(marker in answer for marker in markers), "permission_guard"


async def _run_stability_case(case: PostgreSQLStabilityCase, user_id: str, source_id: str) -> dict[str, Any]:
    from backend.agent import run_agent_stream
    started = time.perf_counter()
    first_token_ms = None
    nodes = []
    state = None
    try:
        async with asyncio.timeout(120):
            async for event in run_agent_stream(
                f"使用 PostgreSQL 测试数仓回答。{case.question}",
                "data-query-agent", [dict(item) for item in case.history], user_id=user_id,
                conversation_id=f"postgres-stability-{case.id}",
            ):
                if event["event"] == "delta" and first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                elif event["event"] == "node_completed":
                    nodes.append({key: event.get(key) for key in ("node", "duration_ms", "status")})
                elif event["event"] == "done":
                    state = event["state"]
        if state is None:
            raise ValueError("Agent did not produce terminal state")
        passed, check = _stability_passed(case, state)
        return {
            "id": case.id, "category": case.category, "expectation": case.expectation,
            "status": "passed" if passed else "failed", "check": check,
            "first_token_ms": first_token_ms,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "answer": (state.get("final") or {}).get("answer", ""),
            "actual": None if passed else state.get("result"),
            "nodes": nodes, "diagnostics": model_diagnostics(state),
        }
    except Exception as exc:
        return {
            "id": case.id, "category": case.category, "expectation": case.expectation,
            "status": "error", "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "first_token_ms": first_token_ms, "error_type": type(exc).__name__, "nodes": nodes,
        }


async def run_stability_acceptance(source_id: str | None = None) -> dict[str, Any]:
    source = _configured_source(source_id)
    actors = {"admin": _platform_admin(), "ecommerce": _platform_ecommerce_user()}
    results = []
    for case in STABILITY_CASES:
        result = await _run_stability_case(case, actors[case.actor], source["id"])
        results.append(result)
        print(f"stability {case.id}: {result['status']}", flush=True)
    summary = _summary(results)
    first_tokens = [item["first_token_ms"] for item in results if item.get("first_token_ms") is not None]
    return {
        "suite_version": "postgresql-stability-2026-09-10.1", "mode": "stability_langgraph_llm",
        "source_id": source["id"], "source_name": source["name"], **summary,
        "median_first_token_ms": round(statistics.median(first_tokens), 2) if first_tokens else None,
        "results": results,
    }
