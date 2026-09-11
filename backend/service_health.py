"""Sanitized readiness checks for the external services used by the agent."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic
from typing import Callable

import httpx

from backend.core import db
from backend.data_sources.service import connector_from_row
from backend.retrieval_models import setting
from backend.vector_store import backend_name, milvus_client


def _timed(check: Callable[[], dict]) -> dict:
    started = monotonic()
    try:
        result = check()
    except Exception:
        # Health responses must never reveal credentials, hosts or driver errors.
        result = {"status": "unavailable"}
    result["latency_ms"] = round((monotonic() - started) * 1000)
    return result


def _postgresql() -> dict:
    connection = db()
    try:
        sources = connection.execute(
            "SELECT * FROM data_sources WHERE kind='postgresql' AND enabled=1"
        ).fetchall()
    finally:
        connection.close()
    if not sources:
        return {"status": "disabled", "configured": 0, "available": 0}
    available = 0
    for source in sources:
        try:
            connector_from_row(source).test()
            available += 1
        except Exception:
            continue
    return {
        "status": "ok" if available == len(sources) else "unavailable",
        "configured": len(sources),
        "available": available,
    }


def _vector_store() -> dict:
    backend = backend_name()
    if backend != "milvus":
        return {"status": "disabled", "backend": backend}
    milvus_client().list_collections()
    return {"status": "ok", "backend": backend}


def _http_model(backend_setting: str, url_setting: str, default_url: str) -> dict:
    backend = setting(backend_setting, "none").lower()
    if backend in {"none", "disabled", "off", "hash"}:
        return {"status": "disabled", "backend": backend}
    url = setting(url_setting, default_url).rstrip("/") + "/health"
    with httpx.Client(timeout=3, trust_env=False) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("status") not in {"ok", "healthy"}:
        raise RuntimeError("Unexpected health response")
    return {"status": "ok", "backend": backend}


def _embedding() -> dict:
    return _http_model("EMBEDDING_BACKEND", "EMBEDDING_BASE_URL", "http://127.0.0.1:8081")


def _reranker() -> dict:
    return _http_model("RERANK_BACKEND", "RERANK_BASE_URL", "http://127.0.0.1:8082")


def _tool_cache() -> dict:
    from backend.tools.executor import executor

    if executor.cache is None:
        return {"status": "disabled", "backend": "none"}
    executor.cache.ping()
    return {"status": "ok", "backend": executor.cache.backend_name}


def dependency_health() -> dict:
    """Probe independent dependencies concurrently and return a safe readiness view."""
    checks = {
        "postgresql": _postgresql,
        "vector_store": _vector_store,
        "embedding": _embedding,
        "reranker": _reranker,
        "tool_cache": _tool_cache,
    }
    dependencies: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="health") as executor:
        futures = {executor.submit(_timed, check): name for name, check in checks.items()}
        for future in as_completed(futures):
            dependencies[futures[future]] = future.result()
    ordered = {name: dependencies[name] for name in checks}
    healthy = all(item["status"] in {"ok", "disabled"} for item in ordered.values())
    return {
        "status": "ok" if healthy else "degraded",
        "runtime": "fastapi-langgraph",
        "dependencies": ordered,
    }
