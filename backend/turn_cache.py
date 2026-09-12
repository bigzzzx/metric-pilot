"""Short-lived, permission-scoped cache for complete read-only chat turns."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import unicodedata
from typing import Any

from backend.agent_cache import cache_ttl, configured_agent_cache
from backend.core import db
from backend.observability import log_event
from backend.schemas import ChatResponse
from backend.tools.cache import dependency_fingerprint, permission_fingerprint


TURN_RESPONSE_CACHE_TTL_SECONDS = cache_ttl("TURN_RESPONSE_CACHE_TTL_SECONDS", 60)
try:
    TURN_RESPONSE_CACHE_MAX_PAYLOAD_BYTES = max(
        1_024, min(int(os.getenv("TURN_RESPONSE_CACHE_MAX_PAYLOAD_BYTES", "262144")), 1_048_576)
    )
except ValueError:
    TURN_RESPONSE_CACHE_MAX_PAYLOAD_BYTES = 262_144
_cache = configured_agent_cache()
_CONTEXTUAL_PREFIX = re.compile(
    r"^(那|这个|那个|它|他们|这些|那些|上面|前面|刚才|继续|同上|换成|改成|再查|再看|然后呢|还有呢)"
)
_RELATIVE_TIME = re.compile(r"(现在|目前|当前|实时|最新|今天|昨天|刚刚|最近|本周|本月|上周|上月)")


def normalize_question(message: str) -> str:
    normalized = unicodedata.normalize("NFKC", message or "").strip().lower()
    return " ".join(normalized.split())


def is_standalone_cacheable_question(message: str) -> bool:
    question = normalize_question(message)
    return bool(question) and not _CONTEXTUAL_PREFIX.search(question) and not _RELATIVE_TIME.search(question)


def _digest(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _agent_configuration(agent_id: str) -> tuple:
    connection = db()
    try:
        row = connection.execute(
            "SELECT id,model_id,tools,system_prompt,enabled,mcp_servers,mcp_tools,skills "
            "FROM agents WHERE id=?",
            (agent_id,),
        ).fetchone()
        return tuple(row) if row else ()
    finally:
        connection.close()


def response_cache_identity(message: str, user_id: str, agent_id: str, model_id: str | None) -> dict | None:
    """Build a history-independent key; contextual questions deliberately bypass this cache."""
    if _cache is None or not is_standalone_cacheable_question(message):
        return None
    try:
        configuration = _agent_configuration(agent_id)
        return {
            "contract": "turn-response-v1",
            "question": normalize_question(message),
            "user_id": user_id,
            "agent_id": agent_id,
            "model_id": model_id or (configuration[1] if configuration else None),
            "agent_configuration": _digest(configuration),
            "permissions": permission_fingerprint(db, user_id),
            "dependencies": dependency_fingerprint(db),
        }
    except Exception as exc:
        log_event("turn_cache.identity_unavailable", level=30, error_type=type(exc).__name__)
        return None


def load_cached_response(identity: dict | None) -> dict | None:
    if _cache is None or identity is None:
        return None
    cached = _cache.get("turn-response", identity)
    if not cached:
        return None
    try:
        payload = {key: value for key, value in cached.items() if key != "_cached_at"}
        response = ChatResponse.model_validate(payload).model_dump(mode="json")
    except Exception as exc:
        log_event("turn_cache.invalid_entry", level=30, error_type=type(exc).__name__)
        return None
    trace = copy.deepcopy(response.get("trace") or {})
    cache_trace = trace.setdefault("cache", {})
    cache_trace["turn_hit"] = True
    response["trace"] = trace
    # An old tool call/audit id must never be presented as a new execution.
    response["tool_result"] = None
    log_event("turn_cache.hit", user_id=identity.get("user_id"), agent_id=identity.get("agent_id"))
    return response


def _cacheable_success(response: ChatResponse, state: dict) -> bool:
    tool_result = state.get("tool_result") or {}
    return bool(
        response.intent == "DATA_QUERY"
        and response.sql
        and response.answer.strip()
        and state.get("route") in {"data_query", "data_analysis"}
        and not state.get("query_error")
        and not state.get("permission_error")
        and not state.get("approval")
        and not state.get("memory_actions")
        and isinstance(tool_result, dict)
        and tool_result.get("status") == "success"
    )


def store_cached_response(identity: dict | None, response: ChatResponse, state: dict) -> bool:
    if _cache is None or identity is None or not _cacheable_success(response, state):
        return False
    payload = response.model_dump(mode="json")
    # SQL/result are already first-class response fields. Avoid duplicating the
    # original tool payload and never cache an execution-specific audit id.
    payload["tool_result"] = None
    trace = copy.deepcopy(payload.get("trace") or {})
    trace.setdefault("cache", {})["turn_hit"] = False
    payload["trace"] = trace
    payload_bytes = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    if payload_bytes > TURN_RESPONSE_CACHE_MAX_PAYLOAD_BYTES:
        log_event("turn_cache.payload_too_large", payload_bytes=payload_bytes)
        return False
    _cache.set("turn-response", identity, payload, TURN_RESPONSE_CACHE_TTL_SECONDS)
    log_event(
        "turn_cache.write",
        user_id=identity.get("user_id"),
        agent_id=identity.get("agent_id"),
        ttl_seconds=TURN_RESPONSE_CACHE_TTL_SECONDS,
    )
    return True
