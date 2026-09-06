"""Run offline golden SQL checks or real-model graph evaluation, never conflate them."""
import asyncio
import json
import statistics
import time

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
    from backend.agent import run_agent_stream
    from backend.evaluation.environment import SQL_ATTEMPTS
    SQL_ATTEMPTS.clear()
    started = time.perf_counter()
    first = None
    nodes = []
    state = None
    try:
        async with asyncio.timeout(90):
            async for event in run_agent_stream(case.question, "data-query-agent", case.history,
                                                user_id="eval-analyst", conversation_id="eval-" + case.id):
                if event["event"] == "delta" and first is None:
                    first = round((time.perf_counter() - started) * 1000, 2)
                if event["event"] == "node_completed":
                    nodes.append({key: event[key] for key in ("node", "duration_ms")})
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
        return {"id": case.id, "status": "passed" if passed else "failed", "first_response_ms": first,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2), "nodes": nodes,
                "sql": state.get("sql"), "rows": state.get("result"), "answer": answer,
                "sql_attempts": list(SQL_ATTEMPTS)}
    except Exception as exc:
        return {"id": case.id, "status": "error", "error_type": type(exc).__name__, "nodes": nodes,
                "sql_attempts": list(SQL_ATTEMPTS),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


async def evaluate(mode="offline", case_ids=None):
    unknown = (case_ids or set()) - {case.id for case in CASES}
    if unknown:
        raise ValueError("Unknown evaluation cases: " + ", ".join(sorted(unknown)))
    cases = [case for case in CASES if not case_ids or case.id in case_ids]
    results = []
    for case in cases:
        result = await live_case(case) if mode == "live" else offline_case(case)
        results.append(result)
        print(json.dumps({"case": case.id, "status": result["status"]}), flush=True)
    completed = [item for item in results if item["status"] != "skipped"]
    durations = [item["duration_ms"] for item in completed if "duration_ms" in item]
    return {"suite_version": "2026-09-06.2-explicit-order-date",
            "mode": mode, "retrieval": "permission-scoped fixture BM25 (no production vectors)",
            "model_accuracy_measured": mode == "live", "total": len(results),
            "passed": sum(item["status"] == "passed" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "errors": sum(item["status"] == "error" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
            "median_duration_ms": statistics.median(durations) if durations else None,
            "answer_quality": "row correctness checked; prose requires review",
            "results": results}
