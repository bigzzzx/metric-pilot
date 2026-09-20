"""Run offline golden SQL checks or real-model graph evaluation, never conflate them."""
import asyncio
import json
import statistics
import time
import uuid

from fastapi import HTTPException

from backend.evaluation.cases import CASES
from backend.evaluation.scoring import rows_equal


def offline_case(case):
    from backend.tools.sql_tool import run_readonly_sql
    if case.outcome in {"chat", "clarification"}:
        return {"id": case.id, "status": "skipped", "reason": "Requires model/graph behavior testing"}
    started = time.perf_counter()
    try:
        rows = run_readonly_sql(case.sql, "eval-analyst")
        passed = case.outcome == "rows" and rows_equal(rows, case.expected)
    except HTTPException as exc:
        passed = case.outcome == "denied" and exc.status_code == 403
        rows = []
    return {"id": case.id, "status": "passed" if passed else "failed", "rows": rows,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


async def live_case(case):
    from backend.model_metrics import collected_metrics
    metrics = []
    token = collected_metrics.set(metrics)
    try:
        result = await _live_case(case)
        return {**result, "model_calls": metrics}
    finally:
        collected_metrics.reset(token)


async def _live_case(case):
    from backend.agent import run_agent_stream
    from backend.conversations import create_conversation
    from backend.evaluation.environment import SQL_ATTEMPTS
    SQL_ATTEMPTS.clear()
    started = time.perf_counter()
    first = None
    nodes = []
    state = None
    conversation_id = "eval-" + case.id + "-" + uuid.uuid4().hex
    try:
        create_conversation("eval-analyst", conversation_id)
        async with asyncio.timeout(90):
            async for event in run_agent_stream(case.question, "data-query-agent", case.history,
                                                user_id="eval-analyst", conversation_id=conversation_id):
                if event["event"] == "delta" and first is None:
                    first = round((time.perf_counter() - started) * 1000, 2)
                if event["event"] == "node_completed":
                    nodes.append({key: event.get(key) for key in ("node", "duration_ms", "status")})
                if event["event"] == "done":
                    state = event["state"]
        if state is None:
            raise ValueError("No terminal state")
        answer = state.get("final", {}).get("answer", "")
        if case.outcome == "rows":
            passed = bool(state.get("sql")) and rows_equal(state.get("result", []), case.expected)
        elif case.outcome == "denied":
            passed = not state.get("result") and (bool(state.get("permission_error")) or any(word in answer for word in ("权限", "敏感", "无权", "未授权")))
        elif case.outcome == "clarification":
            passed = not state.get("sql") and any(word in answer for word in ("请", "需要确认", "哪个", "哪种"))
        else:
            passed = bool(answer) and not state.get("sql") and not any(item["node"] == "rag" for item in nodes)
        return {"id": case.id, "conversation_id": conversation_id,
                "status": "passed" if passed else "failed", "first_response_ms": first,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2), "nodes": nodes,
                "sql": state.get("sql"), "rows": state.get("result"), "answer": answer,
                "query_mode": state.get("query_mode"),
                "metric_resolution": state.get("metric_resolution"),
                "planner_source_id": state.get("planner_source_id"),
                "source_resolution_reason": state.get("source_resolution_reason"),
                "sql_attempts": list(SQL_ATTEMPTS), "tool_error": state.get("tool_result", {}).get("error")}
    except Exception as exc:
        return {"id": case.id, "conversation_id": conversation_id,
                "status": "error", "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None), "http_status": getattr(exc, "status", None),
                "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None, "nodes": nodes,
                "sql_attempts": list(SQL_ATTEMPTS),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


def _model_runtime() -> dict:
    from backend.model_client import _configured_model
    from backend.provider_transport import connection_label
    row = _configured_model("data-query-agent", None)
    return {
        "provider": row["provider_name"],
        "provider_kind": row["kind"],
        "model": row["model_id"],
        "connection": connection_label(row),
    }


async def evaluate(mode="offline", case_ids=None, *, no_cache=False):
    from backend import agent
    unknown = (case_ids or set()) - {case.id for case in CASES}
    if unknown:
        raise ValueError("Unknown evaluation cases: " + ", ".join(sorted(unknown)))
    cases = [case for case in CASES if not case_ids or case.id in case_ids]
    results = []
    configured_cache = agent.agent_cache
    configured_tool_cache = agent.tool_executor.cache
    if mode == "live" and no_cache:
        agent.agent_cache = None
        agent.tool_executor.cache = None
    try:
        for case in cases:
            result = await live_case(case) if mode == "live" else offline_case(case)
            results.append(result)
            print(json.dumps({"case": case.id, "status": result["status"]}), flush=True)
    finally:
        agent.agent_cache = configured_cache
        agent.tool_executor.cache = configured_tool_cache
    completed = [item for item in results if item["status"] != "skipped"]
    durations = [item["duration_ms"] for item in completed if "duration_ms" in item]
    return {"suite_version": "2026-09-06.3-fixture-schema-consistency",
            "mode": mode, "retrieval": "permission-scoped fixture BM25 (no production vectors)",
            "cache_mode": "disabled" if no_cache else "configured",
            "model_runtime": _model_runtime() if mode == "live" else None,
            "model_accuracy_measured": mode == "live", "total": len(results),
            "passed": sum(item["status"] == "passed" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "errors": sum(item["status"] == "error" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
            "median_duration_ms": statistics.median(durations) if durations else None,
            "answer_quality": "row correctness checked; prose requires review",
            "results": results}
