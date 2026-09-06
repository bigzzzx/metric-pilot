import sqlite3

from backend import conversations, memory_migrations


def test_backfill_reads_only_successful_turns_and_runs_once(tmp_path, monkeypatch):
    database_path = tmp_path / "memory-migration.db"

    def isolated_db():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(conversations, "db", isolated_db)
    monkeypatch.setattr(memory_migrations, "db", isolated_db)
    connection = isolated_db()
    connection.executescript(
        """
        CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT,password_hash TEXT);
        CREATE TABLE sessions(session_id TEXT,role TEXT,content TEXT,user_id TEXT);
        INSERT INTO users VALUES ('user-a','alice','unused');
        """
    )
    connection.commit()
    connection.close()
    conversations.init_conversation_storage()
    conversations.create_conversation("user-a", "conversation-a")

    conversations.begin_turn(
        "conversation-a", "user-a", "记得我喜欢直接说重点", "successful-turn"
    )
    conversations.finish_turn(
        "conversation-a",
        "user-a",
        "successful-turn",
        {"answer": "好的"},
    )
    conversations.begin_turn(
        "conversation-a", "user-a", "记住这条失败内容", "failed-turn"
    )
    conversations.fail_turn(
        "conversation-a", "user-a", "failed-turn", {"detail": "failed"}
    )

    first = memory_migrations.backfill_explicit_memories_once()
    second = memory_migrations.backfill_explicit_memories_once()

    assert first == {
        "already_applied": False,
        "scanned": 1,
        "saved": 1,
        "skipped": 0,
    }
    assert second == {
        "already_applied": True,
        "scanned": 0,
        "saved": 0,
        "skipped": 0,
    }
    assert [item["content"] for item in conversations.active_user_memories("user-a")] == [
        "用户明确要求记住：我喜欢直接说重点"
    ]
    connection = isolated_db()
    source = connection.execute(
        "SELECT source_message_id FROM user_memories WHERE user_id='user-a'"
    ).fetchone()
    connection.close()
    assert source["source_message_id"]
