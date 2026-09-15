"""Build a bounded, user-visible trace from permission-scoped Agent state."""
from __future__ import annotations

from typing import Any


_SOURCE_LIMIT = 12
_TABLE_LIMIT = 6
_NODE_LIMIT = 16


def _locator(item: dict) -> str:
    parts: list[str] = []
    section = item.get("section_path") or []
    if section:
        parts.append(" > ".join(str(value) for value in section))
    locator = item.get("locator") or {}
    if locator.get("page") is not None:
        parts.append(f"第 {locator['page']} 页")
    if locator.get("slide") is not None:
        parts.append(f"第 {locator['slide']} 页幻灯片")
    if locator.get("sheet"):
        parts.append(f"工作表 {locator['sheet']}")
    if locator.get("row_start") is not None:
        end = locator.get("row_end", locator["row_start"])
        parts.append(f"第 {locator['row_start']}-{end} 行")
    return " · ".join(parts)


def _sources(items: list[dict]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        identifier = str(item.get("id") or "")
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        result.append({
            "id": identifier,
            "kind": str(item.get("kind") or "catalog"),
            "title": str(item.get("title") or identifier)[:180],
            "asset_ids": [str(value) for value in (item.get("asset_ids") or [])[:4]],
            "domain_id": item.get("domain_id"),
            "source_id": item.get("source_id"),
            "locator": _locator(item),
        })
        if len(result) >= _SOURCE_LIMIT:
            break
    return result


def _tables(schemas: dict) -> list[dict[str, Any]]:
    result = []
    for table in schemas.get("tables", [])[:_TABLE_LIMIT]:
        namespace = str(table.get("namespace") or "").strip()
        name = str(table.get("physical_name") or table.get("name") or "").strip()
        result.append({
            "asset_id": table.get("asset_id"),
            "name": ".".join(value for value in (namespace, name) if value),
            "source_id": table.get("source_id"),
            "source_name": table.get("source_name"),
            "catalog_version": table.get("catalog_version"),
        })
    return result


def _nodes(items: list[dict]) -> list[dict[str, Any]]:
    return [{
        "node": str(item.get("node") or ""),
        "label": str(item.get("label") or ""),
        "duration_ms": item.get("duration_ms"),
        "status": str(item.get("status") or "success"),
    } for item in items[:_NODE_LIMIT] if item.get("node")]


def build_query_trace(state: dict) -> dict[str, Any] | None:
    """Expose evidence coordinates and execution facts, never prompts or credentials."""
    if not any((state.get("rag"), state.get("sql"), state.get("verified_schemas"), state.get("query_error"), state.get("visualizations"), state.get('metric_resolution'))):
        return None
    plan = state.get("plan") or {}
    rows = state.get("result") or []
    tool = state.get("tool_result") or {}
    error = tool.get("error") if isinstance(tool, dict) else None
    return {
        "version": 1,
        "query_mode": state.get('query_mode','text_to_sql'),
        "metric_status": state.get('metric_resolution',{}).get('status'),
        "metric_parameter_model_used": bool(state.get('metric_parameter_model_used')),
        "route": state.get("route"),
        "route_source": state.get("route_source"),
        "contextual_follow_up": bool(state.get("contextual_follow_up")),
        "sources": _sources(state.get("rag") or []),
        "verified_tables": _tables(state.get("verified_schemas") or {}),
        "metric_refs": plan.get("metric_refs") or [],
        "time_column": plan.get("time_column"),
        "filters": (plan.get("filters") or [])[:20],
        "join_relationships": (plan.get("join_relationships") or [])[:10],
        "execution": {
            "result_reused": bool(state.get("visualization_reused")),
            "status": tool.get("status") if isinstance(tool, dict) else None,
            "row_count": len(rows),
            "tool_cache_hit": bool(tool.get("cache_hit")) if isinstance(tool, dict) else False,
            "error_code": error.get("code") if isinstance(error, dict) else None,
            "error_message": (error.get("message") if isinstance(error, dict) else None)
                or state.get("query_error"),
        },
        "cache": {
            "turn_hit": False,
            "route_hit": bool(state.get("route_cache_hit")),
            "plan_hit": bool(state.get("plan_cache_hit")),
            "answer_hit": bool(state.get("answer_cache_hit")),
        },
        "nodes": _nodes(state.get("node_trace") or []),
    }
