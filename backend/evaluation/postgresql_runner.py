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
                **({} if passed else {
                    "plan": state.get("plan"),
                    "query_error": state.get("query_error"),
                    "tool_result": state.get("tool_result"),
                })}
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
