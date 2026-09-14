"""Short-lived, context-scoped cache for read-only queries and knowledge explanations."""
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
KNOWLEDGE_RESPONSE_CACHE_TTL_SECONDS = cache_ttl("KNOWLEDGE_RESPONSE_CACHE_TTL_SECONDS", 300)
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
_REFERENCES = re.compile(r"(刚才|之前|上次|前面|上述|这个|那个|它|这些|那些|继续|同上|我|我们|记住|记得|记下)")
_EN_REFERENCES = re.compile(r"\b(this|that|it|previous|above|my|our|remember|today|latest|now|current)\b", re.I)
_EXPLANATION = re.compile(r"^(解释|介绍|讲解|科普|什么是|explain\b|what is\b|define\b)", re.I)


def is_knowledge_question(message: str) -> bool:
    return bool(_EXPLANATION.search(normalize_question(message)))


def _baseline_context(message: str, context: dict) -> dict:
    """Remove only immediately repeated successful Q/A pairs, retaining all other context.

    An intervening instruction or changed summary creates a different key. This
    deliberately trades hit rate for preserving a user's real conversation.
    """
    history = list(context.get("history") or [])
    while len(history) >= 2:
        question, answer = history[-2:]
        if (question.get("role") != "user" or answer.get("role") != "assistant"
                or normalize_question(question.get("content", "")) != normalize_question(message)):
            break
        del history[-2:]
    return {"history": history, "summary": context.get("conversation_summary", ""),
            "memories": context.get("user_memories", [])}


def normalize_question(message: str) -> str:
    normalized = unicodedata.normalize("NFKC", message or "").strip().lower()
    return " ".join(normalized.split())


def is_standalone_cacheable_question(message: str) -> bool:
    question = normalize_question(message)
    return (bool(question) and not _CONTEXTUAL_PREFIX.search(question)
            and not _RELATIVE_TIME.search(question) and not _REFERENCES.search(question)
            and not _EN_REFERENCES.search(question))


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


def _runtime_fingerprint(user_id: str) -> str:
    connection = db()
    try:
        # Hash mutable configuration and durable memory, never cache credentials.
        records = {}
        for table in ("agents", "models", "skills"):
            records[table] = [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
        records["providers"] = [tuple(row) for row in connection.execute(
            "SELECT id,kind,base_url,enabled FROM providers ORDER BY id")]
        records["memories"] = [tuple(row) for row in connection.execute(
            "SELECT id,memory_type,content,active,updated_at FROM user_memories WHERE user_id=? ORDER BY id", (user_id,))]
        return _digest(records)
    finally:
        connection.close()


def response_cache_identity(message: str, user_id: str, agent_id: str, model_id: str | None,
                            *, context: dict | None = None) -> dict | None:
    """Scope knowledge answers to context; explicit follow-ups bypass the cache."""
    if (context or {}).get('attachments') or _cache is None or not is_standalone_cacheable_question(message):
        return None
    try:
        configuration = _agent_configuration(agent_id)
        return {
            "contract": "turn-response-v2",
            "question": normalize_question(message),
            "user_id": user_id,
            "agent_id": agent_id,
            "model_id": model_id or (configuration[1] if configuration else None),
            "agent_configuration": _digest(configuration),
            "permissions": permission_fingerprint(db, user_id),
            "dependencies": dependency_fingerprint(db),
            "runtime": _runtime_fingerprint(user_id),
            "context": _digest(_baseline_context(message, context or {})) if is_knowledge_question(message) else None,
            "generation": _cache.client.get(os.getenv("TOOL_CACHE_PREFIX", "data-agent:tool-cache:v1") + ":generation")
                if hasattr(_cache, "client") else None,
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
    trace["nodes"] = [{"node": "turn_cache", "label": "复用完整回答", "status": "success"}]
    response["steps"] = [{"name": "完整回答缓存", "detail": "复用有效期内的答案，本次未重新运行查询或生成答案。"}]
    response["trace"] = trace
    # An old tool call/audit id must never be presented as a new execution.
    response["tool_result"] = None
    log_event("turn_cache.hit", user_id=identity.get("user_id"), agent_id=identity.get("agent_id"))
    return response


def _cacheable_success(response: ChatResponse, state: dict) -> bool:
    tool_result = state.get("tool_result") or {}
    if (not response.answer.strip() or state.get("query_error") or state.get("permission_error")
            or state.get("approval") or state.get("memory_actions") or state.get("contextual_follow_up")
            or state.get("memory_retrieval") == "search" or state.get("mcp_tool_call")):
        return False
    if response.intent == "CHAT":
        return (state.get("route") == "general" and not response.sql and not tool_result
                and not state.get("requires_data_evidence") and not state.get("selected_skill_ids"))
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
    if response.intent == "CHAT" and not is_knowledge_question(identity.get("question", "")):
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
    ttl = KNOWLEDGE_RESPONSE_CACHE_TTL_SECONDS if response.intent == "CHAT" else TURN_RESPONSE_CACHE_TTL_SECONDS
    _cache.set("turn-response", identity, payload, ttl)
    log_event(
        "turn_cache.write",
        user_id=identity.get("user_id"),
        agent_id=identity.get("agent_id"),
        ttl_seconds=ttl,
    )
    return True
