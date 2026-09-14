"""Durable, user-scoped conversation storage.

This module is deliberately independent from FastAPI.  HTTP routes translate
``LookupError`` into a non-disclosing 404 and use the returned dictionaries as
their API payloads.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from backend.core import db
from backend.memory_store import core_memories, init_memory_store, retrieve_memories, upsert_memory


_LEGACY_MIGRATION = "001_legacy_sessions_to_conversations"
_LEGACY_NAMESPACE = uuid.UUID("7892f9da-710c-4c84-8961-688711fd4409")
# Keep a small buffer before compacting so we do not make a separate LLM
# request for every new message.  Once a summary exists, only the smaller
# window after that summary is sent as raw history.
COMPACTED_RAW_HISTORY_LIMIT = 12
PRE_COMPACTION_HISTORY_LIMIT = 16


class ConversationConflictError(ValueError):
    """A client message id was reused for a different user message."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _connection() -> sqlite3.Connection:
    connection = db()
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # Some test/in-memory connections cannot change their journal mode.
        pass
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _json_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("Conversation payload must be a mapping or Pydantic model")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {"raw": value}


def _display_title(content: str, fallback: str = "新对话") -> str:
    normalized = " ".join(content.split())
    return normalized[:18] or fallback


def _summary(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "title": row["title"] or "新对话",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": int(row["message_count"]) if "message_count" in keys else 0,
    }


def _message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sequence_no": int(row["sequence_no"]),
        "role": row["role"],
        "kind": row["kind"],
        "content": row["content"],
        "payload": _load(row["payload_json"]),
        "client_message_id": row["client_message_id"],
        "created_at": row["created_at"],
    }


def _conversation_row(
    connection: sqlite3.Connection, conversation_id: str, user_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM conversation_messages m
                WHERE m.conversation_id=c.id) AS message_count
        FROM conversations c
        WHERE c.id=? AND c.user_id=?
        """,
        (conversation_id, user_id),
    ).fetchone()
    if row is None:
        # Deliberately identical for missing and cross-user IDs.
        raise LookupError("Conversation not found")
    return row


def _next_sequence(connection: sqlite3.Connection, conversation_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS value "
        "FROM conversation_messages WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    return int(row["value"])


def _terminal_response(
    connection: sqlite3.Connection, conversation_id: str, client_message_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM conversation_messages
        WHERE conversation_id=? AND client_message_id=? AND role='assistant'
          AND kind IN ('message', 'error', 'legacy')
        ORDER BY sequence_no DESC LIMIT 1
        """,
        (conversation_id, client_message_id),
    ).fetchone()


def _migrate_legacy_sessions(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?", (_LEGACY_MIGRATION,)
    ).fetchone():
        return
    if not _table_exists(connection, "sessions") or not _table_exists(connection, "users"):
        return
    columns = _column_names(connection, "sessions")
    if not {"session_id", "role", "content", "user_id"}.issubset(columns):
        return

    created_expression = "s.created_at" if "created_at" in columns else "CURRENT_TIMESTAMP"
    rows = list(
        connection.execute(
            f"""
            SELECT s.rowid AS legacy_rowid, s.session_id, s.role, s.content,
                   {created_expression} AS created_at, s.user_id
            FROM sessions s
            JOIN users u ON u.id=s.user_id
            WHERE TRIM(COALESCE(s.user_id,'')) <> ''
            ORDER BY s.user_id, s.session_id, s.rowid
            """
        )
    )

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault((str(row["user_id"]), str(row["session_id"])), []).append(row)

    for (user_id, legacy_session_id), legacy_rows in grouped.items():
        conversation_id = str(
            uuid.uuid5(_LEGACY_NAMESPACE, f"{user_id}:{legacy_session_id}")
        )
        first_question = next(
            (
                str(item["content"] or "")
                for item in legacy_rows
                if item["role"] == "user"
                and not str(item["content"] or "").startswith("Tool result: ")
            ),
            "",
        )
        created_at = str(legacy_rows[0]["created_at"] or _now())
        updated_at = str(legacy_rows[-1]["created_at"] or created_at)
        connection.execute(
            """
            INSERT OR IGNORE INTO conversations(id,user_id,title,created_at,updated_at)
            VALUES (?,?,?,?,?)
            """,
            (
                conversation_id,
                user_id,
                _display_title(first_question, "历史对话"),
                created_at,
                updated_at,
            ),
        )

        current_client_id: str | None = None
        sequence_no = 0
        for item in legacy_rows:
            raw_role = str(item["role"] or "")
            content = str(item["content"] or "")
            row_id = str(item["legacy_rowid"])
            if raw_role == "user" and content.startswith("Tool result: "):
                role, kind = "tool", "query_result"
                tool_text = content.removeprefix("Tool result: ")
                parsed = _load(tool_text)
                payload = {"result": parsed if not isinstance(parsed, dict) or "raw" not in parsed else tool_text}
                stored_content = tool_text
                current_client_id = current_client_id or str(
                    uuid.uuid5(_LEGACY_NAMESPACE, f"tool:{user_id}:{legacy_session_id}:{row_id}")
                )
            elif raw_role == "user":
                role, kind, stored_content = "user", "message", content
                current_client_id = str(
                    uuid.uuid5(_LEGACY_NAMESPACE, f"turn:{user_id}:{legacy_session_id}:{row_id}")
                )
                payload = {"text": content, "legacy": True}
            elif raw_role == "assistant":
                role, kind, stored_content = "assistant", "legacy", content
                current_client_id = current_client_id or str(
                    uuid.uuid5(_LEGACY_NAMESPACE, f"assistant:{user_id}:{legacy_session_id}:{row_id}")
                )
                payload = {"answer": content, "legacy": True}
            elif raw_role == "tool":
                role, kind, stored_content = "tool", "query_result", content
                current_client_id = current_client_id or str(
                    uuid.uuid5(_LEGACY_NAMESPACE, f"tool:{user_id}:{legacy_session_id}:{row_id}")
                )
                payload = {"result": _load(content)}
            else:
                continue

            sequence_no += 1
            message_id = str(
                uuid.uuid5(_LEGACY_NAMESPACE, f"message:{conversation_id}:{row_id}")
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_messages(
                    id,conversation_id,sequence_no,role,kind,content,payload_json,
                    client_message_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    conversation_id,
                    sequence_no,
                    role,
                    kind,
                    stored_content,
                    _dump(payload),
                    current_client_id,
                    str(item["created_at"] or created_at),
                ),
            )

    connection.execute(
        "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
        (_LEGACY_MIGRATION, _now()),
    )


def init_conversation_storage() -> None:
    """Create the versioned schema and migrate owned legacy sessions once."""

    connection = _connection()
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations(
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '新对话',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS conversation_messages(
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                kind TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                client_message_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                UNIQUE(conversation_id, sequence_no)
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
                ON conversations(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_order
                ON conversation_messages(conversation_id, sequence_no);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_user_message_id
                ON conversation_messages(conversation_id, client_message_id)
                WHERE role='user' AND client_message_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_conversation_terminal_message_id
                ON conversation_messages(conversation_id, client_message_id, role);
            CREATE TABLE IF NOT EXISTS conversation_summaries(
                conversation_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                covered_through_sequence_no INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_memories(
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                normalized_content TEXT NOT NULL,
                source_conversation_id TEXT,
                source_message_id TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(source_conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_user_memories_normalized
                ON user_memories(user_id, normalized_content);
            CREATE INDEX IF NOT EXISTS idx_user_memories_active
                ON user_memories(user_id, active, updated_at DESC);
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        _migrate_legacy_sessions(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    init_memory_store(db)


def create_conversation(user_id: str, conversation_id: str | None = None) -> dict[str, Any]:
    identifier = conversation_id or str(uuid.uuid4())
    timestamp = _now()
    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT user_id FROM conversations WHERE id=?", (identifier,)
        ).fetchone()
        if existing is not None:
            if existing["user_id"] != user_id:
                raise LookupError("Conversation not found")
            row = _conversation_row(connection, identifier, user_id)
            connection.commit()
            return _summary(row)
        connection.execute(
            """
            INSERT INTO conversations(id,user_id,title,created_at,updated_at)
            VALUES (?,?,'新对话',?,?)
            """,
            (identifier, user_id, timestamp, timestamp),
        )
        row = _conversation_row(connection, identifier, user_id)
        connection.commit()
        return _summary(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_conversation(conversation_id: str, user_id: str) -> dict[str, Any]:
    connection = _connection()
    try:
        return _summary(_conversation_row(connection, conversation_id, user_id))
    finally:
        connection.close()


def list_conversations(user_id: str) -> list[dict[str, Any]]:
    connection = _connection()
    try:
        rows = connection.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM conversation_messages m
                    WHERE m.conversation_id=c.id) AS message_count
            FROM conversations c
            WHERE c.user_id=?
              AND EXISTS (SELECT 1 FROM conversation_messages m
                          WHERE m.conversation_id=c.id)
            ORDER BY c.updated_at DESC, c.created_at DESC, c.id DESC
            """,
            (user_id,),
        )
        return [_summary(row) for row in rows]
    finally:
        connection.close()


def get_messages(conversation_id: str, user_id: str) -> dict[str, Any]:
    connection = _connection()
    try:
        conversation = _conversation_row(connection, conversation_id, user_id)
        rows = connection.execute(
            "SELECT * FROM conversation_messages WHERE conversation_id=? ORDER BY sequence_no",
            (conversation_id,),
        )
        return {
            "conversation": _summary(conversation),
            "messages": [_message(row) for row in rows],
        }
    finally:
        connection.close()


def history_for_agent(
    conversation_id: str,
    user_id: str,
    exclude_client_message_id: str | None = None,
    after_sequence_no: int = 0,
) -> list[dict[str, str]]:
    """Return only successful, completed turns in the existing Agent format.

    Pending turns and failed turns are excluded.  Tool rows remain internal in
    storage, but are adapted to the existing ``last_result`` convention here.
    """

    messages = get_messages(conversation_id, user_id)["messages"]
    successful_ids = {
        item["client_message_id"]
        for item in messages
        if item["role"] == "assistant"
        and item["kind"] in {"message", "legacy"}
        and item["client_message_id"] != exclude_client_message_id
    }
    history: list[dict[str, str]] = []
    for item in messages:
        client_id = item["client_message_id"]
        if client_id not in successful_ids or item["sequence_no"] <= after_sequence_no:
            continue
        if item["role"] == "user":
            history.append({"role": "user", "content": item["content"]})
        elif item["role"] == "assistant" and item["kind"] != "error":
            content = item['content']
            payload = item.get('payload') or {}
            if isinstance(payload, dict) and payload.get('source_id') and payload.get('sql') and (payload.get('tool_result') or {}).get('status') == 'success':
                content += '\n已执行查询上下文：' + _dump({'source_id': payload['source_id'], 'sql': payload['sql'][:2000]})
            history.append({"role": "assistant", "content": content})
        elif item["role"] == "tool" and item["kind"] == "query_result":
            payload = item["payload"]
            result = payload.get("result", payload) if isinstance(payload, dict) else payload
            history.append(
                {"role": "user", "content": "Tool result: " + _dump(result), "trusted_query_result": True}
            )
    return history


def conversation_summary(conversation_id: str, user_id: str) -> dict[str, Any] | None:
    connection = _connection()
    try:
        _conversation_row(connection, conversation_id, user_id)
        row = connection.execute(
            "SELECT summary,covered_through_sequence_no,updated_at FROM conversation_summaries WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def active_user_memories(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Compatibility accessor for durable user memory records."""

    return retrieve_memories(user_id, core_limit=limit, relevant_limit=0, connection_factory=db)


def context_for_agent(
    conversation_id: str,
    user_id: str,
    exclude_client_message_id: str | None = None,
    recent_limit: int = PRE_COMPACTION_HISTORY_LIMIT,
    memory_query: str = "",
) -> dict[str, Any]:
    """Load bounded chat history plus scoped, durable memory context."""

    summary = conversation_summary(conversation_id, user_id)
    covered_through = int(summary["covered_through_sequence_no"]) if summary else 0
    # Do not send raw messages that have already been represented by the
    # durable summary.  Before the first summary we retain the 16-message
    # buffer; after compaction the remaining raw window is at most 12.
    history = history_for_agent(
        conversation_id,
        user_id,
        exclude_client_message_id,
        after_sequence_no=covered_through,
    )
    return {
        "history": history[-recent_limit:],
        "conversation_summary": summary["summary"] if summary else "",
        "user_memories": core_memories(user_id, connection_factory=db),
    }

def compaction_candidate(
    conversation_id: str,
    user_id: str,
    recent_limit: int = COMPACTED_RAW_HISTORY_LIMIT,
    threshold: int = PRE_COMPACTION_HISTORY_LIMIT,
) -> dict[str, Any] | None:
    """Return completed context older than the raw-message window, if enough accumulated."""

    existing = conversation_summary(conversation_id, user_id)
    covered = int(existing["covered_through_sequence_no"]) if existing else 0
    messages = get_messages(conversation_id, user_id)["messages"]
    completed_ids = {
        item["client_message_id"] for item in messages
        if item["role"] == "assistant" and item["kind"] in {"message", "legacy"}
    }
    eligible = [
        item for item in messages
        if item["client_message_id"] in completed_ids
        and item["sequence_no"] > covered
        and item["role"] in {"user", "assistant", "tool"}
    ]
    if len(eligible) <= threshold:
        return None
    compact = eligible[:-recent_limit]
    if not compact:
        return None
    return {
        "previous_summary": existing["summary"] if existing else "",
        "messages": compact,
        "covered_through_sequence_no": compact[-1]["sequence_no"],
    }


def save_conversation_summary(
    conversation_id: str, user_id: str, summary: str, covered_through_sequence_no: int
) -> None:
    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _conversation_row(connection, conversation_id, user_id)
        current = connection.execute(
            "SELECT covered_through_sequence_no FROM conversation_summaries WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if current and int(current["covered_through_sequence_no"]) >= covered_through_sequence_no:
            connection.commit()
            return
        connection.execute(
            "INSERT INTO conversation_summaries(conversation_id,summary,covered_through_sequence_no,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET summary=excluded.summary,covered_through_sequence_no=excluded.covered_through_sequence_no,updated_at=excluded.updated_at",
            (conversation_id, summary, covered_through_sequence_no, _now()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_user_memory(
    user_id: str,
    memory_type: str,
    content: str,
    conversation_id: str,
    source_message_id: str | None = None,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
) -> dict[str, Any]:
    """Legacy-compatible writer backed by the namespaced memory store."""

    if memory_type == "profile" and content.startswith("用户希望被称为 "):
        key = "preferred_name"
        value = content.removeprefix("用户希望被称为 ").strip().rstrip("。")
    else:
        key = "legacy_" + uuid.uuid5(_LEGACY_NAMESPACE, f"{memory_type}:{content}").hex
        value = content
    return upsert_memory(
        user_id,
        memory_type,
        key,
        value,
        conversation_id,
        source_message_id,
        content_override=content,
        connection_factory=connection_factory or db,
    )

def begin_turn(
    conversation_id: str,
    user_id: str,
    content: str,
    client_message_id: str,
    attachment_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not client_message_id:
        raise ValueError("client_message_id is required")
    if not content.strip():
        raise ValueError("Message content is required")

    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _conversation_row(connection, conversation_id, user_id)
        existing = connection.execute(
            """
            SELECT * FROM conversation_messages
            WHERE conversation_id=? AND client_message_id=? AND role='user'
            """,
            (conversation_id, client_message_id),
        ).fetchone()
        if existing is not None:
            if existing["content"] != content:
                raise ConversationConflictError(
                    "client_message_id is already associated with a different message"
                )
            old_ids = [item['id'] for item in _load(existing['payload_json']).get('attachments', [])]
            if sorted(old_ids) != sorted(set(attachment_ids or [])):
                raise ConversationConflictError('client_message_id 的附件与原消息不一致')
            connection.commit()
            return {"created": False, "message": _message(existing)}

        has_user_message = connection.execute(
            """
            SELECT 1 FROM conversation_messages
            WHERE conversation_id=? AND role='user' LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        sequence_no = _next_sequence(connection, conversation_id)
        message_id = str(uuid.uuid4())
        timestamp = _now()
        attachment_views = []
        if attachment_ids:
            from backend.attachments.repository import bind, view
            bind(connection, set(attachment_ids), conversation_id, user_id, message_id)
            attachment_views = [view(connection.execute('SELECT * FROM chat_attachments WHERE id=?', (ident,)).fetchone()) for ident in dict.fromkeys(attachment_ids)]
        connection.execute(
            """
            INSERT INTO conversation_messages(
                id,conversation_id,sequence_no,role,kind,content,payload_json,
                client_message_id,created_at
            ) VALUES (?,?,?,'user','message',?,?,?,?)
            """,
            (
                message_id,
                conversation_id,
                sequence_no,
                content,
                _dump({"text": content, **({'attachments': attachment_views} if attachment_views else {})}),
                client_message_id,
                timestamp,
            ),
        )
        if has_user_message is None:
            connection.execute(
                "UPDATE conversations SET title=? WHERE id=? AND user_id=?",
                (_display_title(content), conversation_id, user_id),
            )
        connection.execute(
            "UPDATE conversations SET updated_at=? WHERE id=? AND user_id=?",
            (timestamp, conversation_id, user_id),
        )
        row = connection.execute(
            "SELECT * FROM conversation_messages WHERE id=?", (message_id,)
        ).fetchone()
        connection.commit()
        return {"created": True, "message": _message(row)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def completed_response(
    conversation_id: str, user_id: str, client_message_id: str
) -> dict[str, Any] | None:
    connection = _connection()
    try:
        _conversation_row(connection, conversation_id, user_id)
        row = _terminal_response(connection, conversation_id, client_message_id)
        return _message(row) if row is not None else None
    finally:
        connection.close()


def _finish(
    conversation_id: str,
    user_id: str,
    client_message_id: str,
    payload: dict[str, Any],
    *,
    kind: str,
    tool_result: Any = None,
    write_tool: bool = False,
) -> dict[str, Any]:
    connection = _connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _conversation_row(connection, conversation_id, user_id)
        existing = _terminal_response(connection, conversation_id, client_message_id)
        if existing is not None:
            connection.commit()
            return _message(existing)
        user_message = connection.execute(
            """
            SELECT 1 FROM conversation_messages
            WHERE conversation_id=? AND client_message_id=? AND role='user'
            """,
            (conversation_id, client_message_id),
        ).fetchone()
        if user_message is None:
            raise ValueError("begin_turn must be called before completing a turn")

        timestamp = _now()
        if write_tool:
            sequence_no = _next_sequence(connection, conversation_id)
            connection.execute(
                """
                INSERT INTO conversation_messages(
                    id,conversation_id,sequence_no,role,kind,content,payload_json,
                    client_message_id,created_at
                ) VALUES (?,?,?,'tool','query_result',?,?,?,?)
                """,
                (
                    str(uuid.uuid4()),
                    conversation_id,
                    sequence_no,
                    _dump(tool_result),
                    _dump({"result": tool_result}),
                    client_message_id,
                    timestamp,
                ),
            )

        content = str(payload.get("answer") or payload.get("detail") or payload.get("message") or "")
        sequence_no = _next_sequence(connection, conversation_id)
        message_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO conversation_messages(
                id,conversation_id,sequence_no,role,kind,content,payload_json,
                client_message_id,created_at
            ) VALUES (?,?,?,'assistant',?,?,?,?,?)
            """,
            (
                message_id,
                conversation_id,
                sequence_no,
                kind,
                content,
                _dump(payload),
                client_message_id,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE conversations SET updated_at=? WHERE id=? AND user_id=?",
            (timestamp, conversation_id, user_id),
        )
        row = connection.execute(
            "SELECT * FROM conversation_messages WHERE id=?", (message_id,)
        ).fetchone()
        connection.commit()
        return _message(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_turn(
    conversation_id: str,
    user_id: str,
    client_message_id: str,
    response_payload: Mapping[str, Any] | Any,
    tool_result: Any = None,
) -> dict[str, Any]:
    return _finish(
        conversation_id,
        user_id,
        client_message_id,
        _json_payload(response_payload),
        kind="message",
        tool_result=tool_result,
        write_tool=tool_result is not None,
    )


def fail_turn(
    conversation_id: str,
    user_id: str,
    client_message_id: str,
    error_payload: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    return _finish(
        conversation_id,
        user_id,
        client_message_id,
        _json_payload(error_payload),
        kind="error",
    )
