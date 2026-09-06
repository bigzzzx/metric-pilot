import json
import sqlite3

import pytest

from backend import agent, conversations


@pytest.fixture
def storage(tmp_path, monkeypatch):
    database_path = tmp_path / "conversations.db"

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
    return isolated_db


def _finish_simple(conversation_id, user_id, client_id, text="你好"):
    conversations.begin_turn(conversation_id, user_id, text, client_id)
    return conversations.finish_turn(
        conversation_id,
        user_id,
        client_id,
        {"answer": "收到", "intent": "CHAT", "result": []},
    )


def test_create_list_and_open_conversation(storage):
    empty = conversations.create_conversation("user-a", "empty")
    active = conversations.create_conversation("user-a", "active")

    assert empty["message_count"] == 0
    assert conversations.list_conversations("user-a") == []

    _finish_simple(active["id"], "user-a", "client-1", "请分析今日订单")

    recent = conversations.list_conversations("user-a")
    assert [item["id"] for item in recent] == ["active"]
    assert recent[0]["title"] == "请分析今日订单"
    assert recent[0]["message_count"] == 2

    detail = conversations.get_messages("active", "user-a")
    assert detail["conversation"]["message_count"] == 2
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert [item["sequence_no"] for item in detail["messages"]] == [1, 2]


def test_recent_conversations_are_ordered_by_terminal_update(storage):
    conversations.create_conversation("user-a", "older")
    conversations.create_conversation("user-a", "newer")
    _finish_simple("older", "user-a", "old-turn")
    _finish_simple("newer", "user-a", "new-turn")

    assert [item["id"] for item in conversations.list_conversations("user-a")] == [
        "newer",
        "older",
    ]


def test_conversations_are_strictly_scoped_to_the_owner(storage):
    conversations.create_conversation("user-a", "private")
    _finish_simple("private", "user-a", "owned-turn")

    for operation in (
        lambda: conversations.ensure_conversation("private", "user-b"),
        lambda: conversations.get_messages("private", "user-b"),
        lambda: conversations.begin_turn("private", "user-b", "steal", "other-turn"),
        lambda: conversations.completed_response("private", "user-b", "owned-turn"),
        lambda: conversations.create_conversation("user-b", "private"),
    ):
        with pytest.raises(LookupError, match="Conversation not found"):
            operation()

    assert conversations.list_conversations("user-b") == []


def test_turn_writes_are_idempotent_and_payload_round_trips(storage):
    conversations.create_conversation("user-a", "query")
    first = conversations.begin_turn("query", "user-a", "各地区订单数", "turn-1")
    duplicate = conversations.begin_turn("query", "user-a", "各地区订单数", "turn-1")
    response_payload = {
        "answer": "华东最多",
        "intent": "DATA_QUERY",
        "sql": "SELECT region, count(*) AS orders FROM orders GROUP BY region",
        "result": [{"region": "华东", "orders": 12}],
        "analysis": "华东订单数领先",
        "catalog": [{"name": "orders"}],
        "steps": [{"name": "readonly_sql"}],
    }
    terminal = conversations.finish_turn(
        "query",
        "user-a",
        "turn-1",
        response_payload,
        tool_result=response_payload["result"],
    )
    terminal_again = conversations.finish_turn(
        "query", "user-a", "turn-1", {"answer": "不能覆盖"}, tool_result=[]
    )

    assert first["created"] is True
    assert duplicate["created"] is False
    assert duplicate["message"]["content"] == "各地区订单数"
    assert terminal_again["id"] == terminal["id"]
    assert conversations.completed_response("query", "user-a", "turn-1") == terminal

    detail = conversations.get_messages("query", "user-a")
    assert [item["role"] for item in detail["messages"]] == [
        "user",
        "tool",
        "assistant",
    ]
    assert detail["messages"][1]["payload"] == {"result": response_payload["result"]}
    assert detail["messages"][2]["payload"] == response_payload

    history = conversations.history_for_agent("query", "user-a")
    assert history[0] == {"role": "user", "content": "各地区订单数"}
    assert history[1]["content"].startswith("Tool result: ")
    assert json.loads(history[1]["content"].removeprefix("Tool result: ")) == response_payload["result"]
    assert history[2] == {"role": "assistant", "content": "华东最多"}


def test_client_message_id_cannot_be_reused_for_different_content(storage):
    conversations.create_conversation("user-a", "conflict")
    conversations.begin_turn("conflict", "user-a", "问题 A", "same-id")

    with pytest.raises(conversations.ConversationConflictError):
        conversations.begin_turn("conflict", "user-a", "问题 B", "same-id")

    detail = conversations.get_messages("conflict", "user-a")
    assert [item["content"] for item in detail["messages"]] == ["问题 A"]


def test_failed_turn_is_reloadable_but_excluded_from_agent_history(storage):
    conversations.create_conversation("user-a", "failed")
    conversations.begin_turn("failed", "user-a", "你好", "failed-turn")
    payload = {
        "detail": "模型供应商暂时不可达",
        "code": "model_provider_error",
        "request_id": "req-test",
    }
    failed = conversations.fail_turn("failed", "user-a", "failed-turn", payload)
    failed_again = conversations.fail_turn(
        "failed", "user-a", "failed-turn", {"detail": "不能覆盖"}
    )

    assert failed["kind"] == "error"
    assert failed["payload"] == payload
    assert failed_again["id"] == failed["id"]
    assert conversations.get_messages("failed", "user-a")["messages"][-1]["payload"] == payload
    assert conversations.history_for_agent("failed", "user-a") == []


def test_context_window_summary_and_explicit_memory_are_durable(storage):
    conversations.create_conversation("user-a", "memory")
    for index in range(9):
        _finish_simple("memory", "user-a", f"turn-{index}", f"问题 {index}")

    candidate = conversations.compaction_candidate("memory", "user-a")
    assert candidate is not None
    assert candidate["covered_through_sequence_no"] == 6
    conversations.save_conversation_summary(
        "memory", "user-a", "用户正在讨论订单数据。", candidate["covered_through_sequence_no"]
    )
    saved = conversations.upsert_user_memory(
        "user-a", "profile", "用户希望被称为 xuan。", "memory"
    )
    again = conversations.upsert_user_memory(
        "user-a", "profile", "用户希望被称为 xuan。", "memory"
    )
    context = conversations.context_for_agent("memory", "user-a")

    assert saved["id"] == again["id"]
    assert context["conversation_summary"] == "用户正在讨论订单数据。"
    assert context["user_memories"][0]["content"] == "用户希望被称为 xuan。"
    assert len(context["history"]) == 12
    assert context["history"][0]["content"] == "问题 3"


def test_context_keeps_the_pre_compaction_buffer_without_losing_messages(storage):
    conversations.create_conversation("user-a", "context-buffer")
    for index in range(8):
        _finish_simple("context-buffer", "user-a", f"turn-{index}", f"问题 {index}")

    # Sixteen raw messages are intentionally retained until the next completed
    # turn triggers compaction.  This avoids the old 13-to-16 message gap.
    assert conversations.compaction_candidate("context-buffer", "user-a") is None
    context = conversations.context_for_agent("context-buffer", "user-a")

    assert len(context["history"]) == 16
    assert context["history"][0]["content"] == "问题 0"
    assert context["history"][-1]["content"] == "收到"


def test_agent_prompt_preserves_pre_compaction_history_buffer():
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"消息 {index}"}
        for index in range(16)
    ]
    prompt = agent.messages(
        {
            "message": "当前问题",
            "history": history,
            "conversation_summary": "",
            "user_memories": [],
        },
        "测试系统提示",
    )

    rendered_history = prompt[1:-1]
    assert len(rendered_history) == 16
    assert rendered_history[0]["content"] == "消息 0"
    assert rendered_history[-1]["content"] == "消息 15"


def test_failed_messages_are_not_compacted_or_used_as_memory_context(storage):
    conversations.create_conversation("user-a", "safe-memory")
    for index in range(8):
        _finish_simple("safe-memory", "user-a", f"ok-{index}", f"正常问题 {index}")
    conversations.begin_turn("safe-memory", "user-a", "失败问题", "broken")
    conversations.fail_turn("safe-memory", "user-a", "broken", {"detail": "network"})

    candidate = conversations.compaction_candidate("safe-memory", "user-a")
    assert candidate is None
    assert all(item["content"] != "失败问题" for item in conversations.context_for_agent("safe-memory", "user-a")["history"])


def test_owned_legacy_sessions_migrate_once_and_unowned_rows_stay_hidden(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "legacy.db"

    def isolated_db():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(conversations, "db", isolated_db)
    connection = isolated_db()
    connection.executescript(
        """
        CREATE TABLE users(id TEXT PRIMARY KEY, username TEXT, password_hash TEXT);
        INSERT INTO users VALUES ('user-a','alice','unused');
        CREATE TABLE sessions(
            session_id TEXT, role TEXT, content TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, user_id TEXT DEFAULT ''
        );
        INSERT INTO sessions(session_id,role,content,user_id) VALUES
            ('owned','user','展示订单','user-a'),
            ('owned','user','Tool result: [{"orders":3}]','user-a'),
            ('owned','assistant','共有 3 笔订单','user-a'),
            ('anonymous','user','不能认领',''),
            ('ghost','user','用户不存在','missing-user');
        """
    )
    connection.commit()
    connection.close()

    conversations.init_conversation_storage()
    conversations.init_conversation_storage()

    recent = conversations.list_conversations("user-a")
    assert len(recent) == 1
    detail = conversations.get_messages(recent[0]["id"], "user-a")
    assert [item["role"] for item in detail["messages"]] == [
        "user",
        "tool",
        "assistant",
    ]
    assert detail["messages"][1]["payload"] == {"result": [{"orders": 3}]}

    connection = isolated_db()
    assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 5
    connection.close()
