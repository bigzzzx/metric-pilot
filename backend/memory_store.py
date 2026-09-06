"""Namespaced, durable user-memory storage.

This module deliberately keeps the persistence contract independent from the
LangGraph graph.  The graph receives already-authorized memory context, while
all writes are scoped server-side by the authenticated user.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from backend.core import db
from backend.observability import log_event


DEFAULT_TENANT_ID = "local"
CORE_MEMORY_TYPES = {"profile", "preference"}
SAFE_MEMORY_TYPES = {"profile", "preference", "working_convention", "goal", "constraint", "episodic"}
MEMORY_SCOPES = {"user", "project"}
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def memory_namespace(user_id: str, memory_type: str, tenant_id: str = DEFAULT_TENANT_ID) -> tuple[str, ...]:
    """Build a server-owned namespace; callers never supply a foreign user id."""

    return (tenant_id, user_id, "long_term_memory", memory_type)


def _namespace_json(user_id: str, memory_type: str, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return json.dumps(memory_namespace(user_id, memory_type, tenant_id), ensure_ascii=False)


def _legacy_key(memory_type: str, content: str) -> str:
    if memory_type == "profile" and "被称为" in content:
        return "preferred_name"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    prefix = "working_convention" if memory_type == "working_convention" else memory_type
    return f"{prefix}_{digest}"


def _normalized_scope(scope: str, project_id: str | None) -> tuple[str, str]:
    """Validate server-owned memory scope details before a write or read."""

    normalized_scope = str(scope or "user").strip().lower()
    if normalized_scope not in MEMORY_SCOPES:
        raise ValueError("Unsupported long-term memory scope")
    normalized_project_id = str(project_id or "").strip()
    if normalized_scope == "project" and not normalized_project_id:
        raise ValueError("Project-scoped memory requires a project id")
    if len(normalized_project_id) > 128:
        raise ValueError("Project id must contain at most 128 characters")
    return normalized_scope, normalized_project_id if normalized_scope == "project" else ""


def _display_content(memory_type: str, key: str, value: str) -> str:
    value = value.strip()
    if memory_type == "profile" and key == "preferred_name":
        return f"用户希望被称为 {value}。"
    if memory_type == "profile" and key == "preferred_language":
        return f"用户偏好使用 {value}。"
    if memory_type == "preference" and key == "response_style":
        return f"用户偏好：{value}"
    if memory_type == "working_convention":
        return f"默认约定：{value}"
    if memory_type == "episodic":
        return f"历史事件：{value}"
    return f"用户{memory_type}：{value}"


def init_memory_store(connection_factory: Callable[[], Any] | None = None) -> None:
    """Upgrade the existing table in place without discarding historical memories."""

    connection_factory = connection_factory or db
    connection = connection_factory()
    try:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(user_memories)")}
        for column, definition in (
            ("namespace_json", "TEXT NOT NULL DEFAULT ''"),
            ("memory_key", "TEXT NOT NULL DEFAULT ''"),
            ("value_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("confidence", "REAL NOT NULL DEFAULT 1.0"),
            ("scope", "TEXT NOT NULL DEFAULT 'user'"),
            ("project_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                connection.execute(f"ALTER TABLE user_memories ADD COLUMN {column} {definition}")

        legacy_rows = connection.execute(
            "SELECT id,user_id,memory_type,content FROM user_memories WHERE namespace_json='' OR memory_key=''"
        ).fetchall()
        for row in legacy_rows:
            value = str(row["content"])
            key = _legacy_key(str(row["memory_type"]), value)
            connection.execute(
                "UPDATE user_memories SET namespace_json=?,memory_key=?,value_json=? WHERE id=?",
                (
                    _namespace_json(str(row["user_id"]), str(row["memory_type"])),
                    key,
                    json.dumps({"value": value}, ensure_ascii=False),
                    row["id"],
                ),
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_memories_namespace_key "
            "ON user_memories(namespace_json,memory_key)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_memories_namespace_active "
            "ON user_memories(namespace_json,active,updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_memories_scope_active "
            "ON user_memories(user_id,scope,project_id,active,updated_at DESC)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_memory(
    user_id: str,
    memory_type: str,
    key: str,
    value: str,
    conversation_id: str,
    source_message_id: str | None = None,
    *,
    confidence: float = 1.0,
    tenant_id: str = DEFAULT_TENANT_ID,
    scope: str = "user",
    project_id: str | None = None,
    content_override: str | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Create or replace one stable fact at ``namespace + key``."""

    if memory_type not in SAFE_MEMORY_TYPES:
        raise ValueError("Unsupported long-term memory type")
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("Memory key must be lower snake_case")
    value = value.strip()
    if not value or len(value) > 500:
        raise ValueError("Memory value must contain 1 to 500 characters")
    scope, project_id = _normalized_scope(scope, project_id)

    connection_factory = connection_factory or db
    namespace_json = _namespace_json(user_id, memory_type, tenant_id)
    content = content_override.strip() if content_override else _display_content(memory_type, key, value)
    timestamp = _now()
    connection = connection_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id FROM user_memories WHERE namespace_json=? AND memory_key=? "
            "AND scope=? AND project_id=? ORDER BY active DESC, updated_at DESC LIMIT 1",
            (namespace_json, key, scope, project_id),
        ).fetchone()
        value_json = json.dumps({"value": value}, ensure_ascii=False)
        if existing:
            identifier = existing["id"]
            connection.execute(
                "UPDATE user_memories SET content=?,normalized_content=?,value_json=?,source_conversation_id=?,"
                "source_message_id=?,active=1,confidence=?,updated_at=? WHERE id=?",
                (content, " ".join(content.lower().split()), value_json, conversation_id,
                 source_message_id, confidence, timestamp, identifier),
            )
        else:
            identifier = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO user_memories("
                "id,user_id,memory_type,content,normalized_content,source_conversation_id,source_message_id,"
                "active,created_at,updated_at,namespace_json,memory_key,value_json,confidence,scope,project_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identifier, user_id, memory_type, content, " ".join(content.lower().split()),
                 conversation_id, source_message_id, 1, timestamp, timestamp, namespace_json,
                 key, value_json, confidence, scope, project_id),
            )
        row = connection.execute(
            "SELECT id,user_id,memory_type,memory_key,content,value_json,namespace_json,confidence,scope,project_id,updated_at "
            "FROM user_memories WHERE id=?",
            (identifier,),
        ).fetchone()
        connection.commit()
        return _row(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def deactivate_memory(
    user_id: str,
    memory_type: str,
    key: str,
    *,
    scope: str = "user",
    project_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    return_identifier: bool = False,
    connection_factory: Callable[[], Any] | None = None,
) -> bool | str | None:
    """Deactivate one scoped memory and optionally return its stable identifier."""

    connection_factory = connection_factory or db
    scope, project_id = _normalized_scope(scope, project_id)
    connection = connection_factory()
    try:
        existing = connection.execute(
            "SELECT id FROM user_memories WHERE namespace_json=? AND memory_key=? "
            "AND scope=? AND project_id=? AND active=1",
            (_namespace_json(user_id, memory_type, tenant_id), key, scope, project_id),
        ).fetchone()
        cursor = connection.execute(
            "UPDATE user_memories SET active=0,updated_at=? WHERE namespace_json=? AND memory_key=? "
            "AND scope=? AND project_id=?",
            (_now(), _namespace_json(user_id, memory_type, tenant_id), key, scope, project_id),
        )
        connection.commit()
        if return_identifier:
            return str(existing["id"]) if existing and cursor.rowcount > 0 else None
        return cursor.rowcount > 0
    finally:
        connection.close()


def _query_terms(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    ascii_terms = set(re.findall(r"[a-z0-9_]{2,}", compact))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
    chinese_terms = {chinese[index:index + 2] for index in range(max(0, len(chinese) - 1))}
    return ascii_terms | chinese_terms


def _row(row: Any) -> dict[str, Any]:
    item = dict(row)
    try:
        item["namespace"] = tuple(json.loads(item.pop("namespace_json")))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        item["namespace"] = ()
    try:
        item["value"] = json.loads(item.pop("value_json")).get("value", "")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        item["value"] = item.get("content", "")
    return item


def list_active_memories(
    user_id: str,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    project_id: str | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Return all active memories for one authenticated user only."""

    connection_factory = connection_factory or db
    connection = connection_factory()
    try:
        columns = "id,user_id,memory_type,memory_key,content,value_json,namespace_json,confidence,scope,project_id,updated_at"
        if project_id:
            rows = connection.execute(
                f"SELECT {columns} FROM user_memories WHERE user_id=? AND active=1 "
                "AND (scope='user' OR (scope='project' AND project_id=?)) ORDER BY updated_at DESC",
                (user_id, project_id),
            ).fetchall()
        else:
            rows = connection.execute(
                f"SELECT {columns} FROM user_memories WHERE user_id=? AND active=1 AND scope='user' ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
    finally:
        connection.close()
    items = [_row(row) for row in rows]
    return [
        item
        for item in items
        if not item["namespace"] or item["namespace"][0] == tenant_id
    ]


def core_memories(
    user_id: str,
    *,
    limit: int = 6,
    project_id: str | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Load the small, stable profile that is safe to include every turn."""

    return [
        item
        for item in list_active_memories(
            user_id, project_id=project_id, connection_factory=connection_factory
        )
        if item.get("memory_type") in CORE_MEMORY_TYPES
    ][:limit]

def retrieve_memories(
    user_id: str,
    query: str = "",
    *,
    project_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    core_limit: int = 6,
    relevant_limit: int = 4,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Load core preferences plus query-relevant durable memories within a token budget."""

    items = list_active_memories(
        user_id, tenant_id=tenant_id, project_id=project_id,
        connection_factory=connection_factory,
    )
    if not query.strip():
        return items[:core_limit]
    core = [item for item in items if item.get("memory_type") in CORE_MEMORY_TYPES][:core_limit]
    selected_ids = {item["id"] for item in core}
    terms = _query_terms(query)
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        if item["id"] in selected_ids:
            continue
        haystack = " ".join(str(item.get(field, "")) for field in ("memory_key", "content", "value"))
        score = len(terms & _query_terms(haystack))
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].get("updated_at", "")), reverse=True)
    return [*core, *(item for _, item in scored[:relevant_limit])]


def apply_memory_actions(
    user_id: str,
    conversation_id: str,
    source_message_id: str | None,
    actions: Iterable[Any],
    *,
    project_id: str | None = None,
    connection_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute model-declared memory tools after server-side scope validation."""

    outcomes: list[dict[str, Any]] = []
    for action in actions:
        payload = action.model_dump() if hasattr(action, "model_dump") else dict(action)
        if payload.get("sensitive") or float(payload.get("confidence", 0)) < 0.75:
            log_event("memory.action_skipped", user_id=user_id, reason="unsafe_or_low_confidence")
            continue
        operation = payload.get("operation")
        memory_type = str(payload.get("memory_type") or "")
        key = str(payload.get("key") or "")
        try:
            scope, scoped_project_id = _normalized_scope(
                str(payload.get("scope") or "user"), project_id
            )
        except ValueError:
            log_event("memory.action_skipped", user_id=user_id, reason="missing_or_invalid_scope")
            continue
        if operation == "forget":
            identifier = deactivate_memory(
                user_id,
                memory_type,
                key,
                scope=scope,
                project_id=scoped_project_id,
                return_identifier=True,
                connection_factory=connection_factory,
            )
            outcomes.append(
                {
                    "operation": "forget",
                    "key": key,
                    "deleted": bool(identifier),
                    "memory_id": identifier,
                }
            )
            continue
        value = str(payload.get("value") or "")
        saved = upsert_memory(
            user_id,
            memory_type,
            key,
            value,
            conversation_id,
            source_message_id,
            confidence=float(payload.get("confidence", 1.0)),
            scope=scope,
            project_id=scoped_project_id,
            connection_factory=connection_factory,
        )
        outcomes.append({"operation": "save", "memory": saved})
    if outcomes:
        log_event("memory.actions_applied", user_id=user_id, count=len(outcomes))
    return outcomes
