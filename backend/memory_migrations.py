"""One-time durable-memory migrations kept outside the HTTP application."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.core import db
from backend.memory import backfill_explicit_memories
from backend.observability import log_event
from backend.conversations import upsert_user_memory


EXPLICIT_MEMORY_BACKFILL_VERSION = "002_explicit_memory_commands_v2"


ConnectionFactory = Callable[[], Any]


def _successful_user_messages(connection_factory: ConnectionFactory) -> list[dict[str, Any]]:
    """Return user messages whose turn has a successful assistant terminal row."""

    connection = connection_factory()
    try:
        rows = connection.execute(
            """
            SELECT message.id,
                   message.conversation_id,
                   conversation.user_id,
                   message.role,
                   message.content
            FROM conversation_messages AS message
            JOIN conversations AS conversation
              ON conversation.id = message.conversation_id
            WHERE message.role = 'user'
              AND EXISTS (
                  SELECT 1
                  FROM conversation_messages AS terminal
                  WHERE terminal.conversation_id = message.conversation_id
                    AND terminal.client_message_id = message.client_message_id
                    AND terminal.role = 'assistant'
                    AND terminal.kind IN ('message', 'legacy')
              )
            ORDER BY message.created_at, message.sequence_no
            """
        )
        return [dict(row) for row in rows]
    finally:
        connection.close()


def backfill_explicit_memories_once(
    connection_factory: ConnectionFactory | None = None,
) -> dict[str, int | bool]:
    """Backfill newly-supported explicit commands once, safely and idempotently."""

    connection_factory = connection_factory or db
    connection = connection_factory()
    try:
        applied = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (EXPLICIT_MEMORY_BACKFILL_VERSION,),
        ).fetchone()
    finally:
        connection.close()
    if applied:
        return {"already_applied": True, "scanned": 0, "saved": 0, "skipped": 0}

    def save_memory(
        user_id: str,
        memory_type: str,
        content: str,
        conversation_id: str,
        source_message_id: str | None,
    ):
        return upsert_user_memory(
            user_id,
            memory_type,
            content,
            conversation_id,
            source_message_id,
            connection_factory=connection_factory,
        )

    stats = backfill_explicit_memories(
        _successful_user_messages(connection_factory),
        save_memory=save_memory,
    )
    connection = connection_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES (?,CURRENT_TIMESTAMP)",
            (EXPLICIT_MEMORY_BACKFILL_VERSION,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    log_event("memory.backfill_migration_applied", version=EXPLICIT_MEMORY_BACKFILL_VERSION, **stats)
    return {"already_applied": False, **stats}
