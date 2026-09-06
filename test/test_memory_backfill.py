import sqlite3

import pytest

from backend import conversations
from backend.memory import backfill_explicit_memories


@pytest.fixture
def storage(tmp_path, monkeypatch):
    database_path = tmp_path / "memory-backfill.db"

    def isolated_db():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(conversations, "db", isolated_db)
    connection = isolated_db()
    connection.executescript(
        """
        CREATE TABLE users(
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sessions(
            session_id TEXT,
            role TEXT,
            content TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT DEFAULT ''
        );
        INSERT INTO users(id,username,password_hash) VALUES
            ('user-a','alice','unused'),
            ('user-b','bob','unused');
        """
    )
    connection.commit()
    connection.close()
    conversations.init_conversation_storage()
    conversations.create_conversation("user-a", "conversation-a")
    conversations.create_conversation("user-b", "conversation-b")
    return isolated_db


def test_backfill_is_idempotent_and_preserves_source_message_id(storage):
    records = [
        {
            "id": "message-a",
            "conversation_id": "conversation-a",
            "user_id": "user-a",
            "role": "user",
            "content": "记得我喜欢直接说重点",
        },
        {
            "id": "assistant-a",
            "conversation_id": "conversation-a",
            "user_id": "user-a",
            "role": "assistant",
            "content": "好的",
        },
        {
            "id": "question-a",
            "conversation_id": "conversation-a",
            "user_id": "user-a",
            "role": "user",
            "content": "记得我喜欢什么吗？",
        },
    ]

    first = backfill_explicit_memories(records)
    second = backfill_explicit_memories(records)

    assert first == {"scanned": 3, "saved": 1, "skipped": 2}
    assert second == {"scanned": 3, "saved": 1, "skipped": 2}
    memories = conversations.active_user_memories("user-a")
    assert len(memories) == 1
    assert memories[0]["content"] == "用户明确要求记住：我喜欢直接说重点"

    connection = storage()
    row = connection.execute(
        "SELECT source_message_id FROM user_memories WHERE user_id='user-a'"
    ).fetchone()
    connection.close()
    assert row["source_message_id"] == "message-a"


def test_backfill_keeps_user_memories_isolated(storage):
    records = [
        {
            "message_id": "message-a",
            "conversation_id": "conversation-a",
            "user_id": "user-a",
            "content": "记住我喜欢中文回答",
        },
        {
            "message_id": "message-b",
            "conversation_id": "conversation-b",
            "user_id": "user-b",
            "content": "记住我喜欢英文回答",
        },
    ]

    result = backfill_explicit_memories(records)

    assert result == {"scanned": 2, "saved": 2, "skipped": 0}
    assert conversations.active_user_memories("user-a")[0]["content"].endswith(
        "我喜欢中文回答"
    )
    assert conversations.active_user_memories("user-b")[0]["content"].endswith(
        "我喜欢英文回答"
    )


def test_backfill_skips_records_marked_unsuccessful(storage):
    result = backfill_explicit_memories(
        [
            {
                "id": "failed-message",
                "conversation_id": "conversation-a",
                "user_id": "user-a",
                "role": "user",
                "successful": False,
                "content": "记住这条失败消息",
            }
        ]
    )

    assert result == {"scanned": 1, "saved": 0, "skipped": 1}
    assert conversations.active_user_memories("user-a") == []
