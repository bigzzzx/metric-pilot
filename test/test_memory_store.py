import sqlite3

from backend import conversations
from backend.memory_store import (
    apply_memory_actions,
    core_memories,
    list_active_memories,
    retrieve_memories,
    upsert_memory,
)
from backend.memory_vector_sync import synchronize_forgotten_memory_vectors
from backend.schemas import RouteOutput


def _storage(tmp_path, monkeypatch):
    database_path = tmp_path / "namespaced-memory.db"

    def isolated_db():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(conversations, "db", isolated_db)
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
    return isolated_db


def test_memory_namespace_key_replaces_a_stable_fact(tmp_path, monkeypatch):
    storage = _storage(tmp_path, monkeypatch)
    first = upsert_memory(
        "user-a", "profile", "preferred_name", "xuan", "conversation-a", connection_factory=storage
    )
    second = upsert_memory(
        "user-a", "profile", "preferred_name", "张老师", "conversation-a", connection_factory=storage
    )

    memories = retrieve_memories("user-a", "怎么称呼我", connection_factory=storage)

    assert first["id"] == second["id"]
    assert second["namespace"] == ("local", "user-a", "long_term_memory", "profile")
    assert memories[0]["value"] == "张老师"
    assert memories[0]["content"] == "用户希望被称为 张老师。"


def test_model_declared_memory_actions_are_validated_then_applied(tmp_path, monkeypatch):
    storage = _storage(tmp_path, monkeypatch)
    action = RouteOutput.model_validate(
        {
            "route": "general",
            "memory_actions": [
                {
                    "operation": "save",
                    "memory_type": "preference",
                    "key": "response_style",
                    "value": "先给结论，再给依据",
                    "confidence": 0.96,
                    "sensitive": False,
                }
            ],
        }
    ).memory_actions

    applied = apply_memory_actions("user-a", "conversation-a", "message-a", action, connection_factory=storage)
    assert applied[0]["operation"] == "save"
    assert retrieve_memories("user-a", "怎么回答", connection_factory=storage)[0]["value"] == "先给结论，再给依据"

    removed = apply_memory_actions(
        "user-a",
        "conversation-a",
        "message-b",
        [{"operation": "forget", "memory_type": "preference", "key": "response_style", "confidence": 1, "sensitive": False}],
        connection_factory=storage,
    )
    assert removed[0]["operation"] == "forget"
    assert removed[0]["key"] == "response_style"
    assert removed[0]["deleted"] is True
    assert removed[0]["memory_id"]
    assert retrieve_memories("user-a", "怎么回答", connection_factory=storage) == []


def test_project_memories_are_isolated_from_other_project_contexts(tmp_path, monkeypatch):
    storage = _storage(tmp_path, monkeypatch)
    user_memory = upsert_memory(
        "user-a", "preference", "response_style", "先给结论", "conversation-a",
        connection_factory=storage,
    )
    project_memory = upsert_memory(
        "user-a", "working_convention", "gmv_scope", "GMV 排除退款订单", "conversation-a",
        scope="project", project_id="retail-bi", connection_factory=storage,
    )

    assert [item["id"] for item in list_active_memories("user-a", connection_factory=storage)] == [user_memory["id"]]
    in_project = list_active_memories("user-a", project_id="retail-bi", connection_factory=storage)
    assert {item["id"] for item in in_project} == {user_memory["id"], project_memory["id"]}
    other_project = list_active_memories("user-a", project_id="risk-bi", connection_factory=storage)
    assert [item["id"] for item in other_project] == [user_memory["id"]]


def test_project_scoped_action_requires_server_project_context(tmp_path, monkeypatch):
    storage = _storage(tmp_path, monkeypatch)
    action = {
        "operation": "save",
        "memory_type": "working_convention",
        "key": "gmv_scope",
        "value": "GMV 排除退款订单",
        "scope": "project",
        "confidence": 1,
        "sensitive": False,
    }

    assert apply_memory_actions("user-a", "conversation-a", "message-a", [action], connection_factory=storage) == []
    saved = apply_memory_actions(
        "user-a", "conversation-a", "message-a", [action], project_id="retail-bi", connection_factory=storage
    )
    assert saved[0]["memory"]["scope"] == "project"
    assert saved[0]["memory"]["project_id"] == "retail-bi"


def test_forgotten_memory_vector_cleanup_uses_internal_identifier(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "backend.memory_vector_sync.delete_memory_vectors",
        lambda identifiers: captured.update(identifiers=identifiers),
    )

    synchronize_forgotten_memory_vectors(
        [{"operation": "forget", "deleted": True, "memory_id": "memory-1"}]
    )

    assert captured == {"identifiers": ["memory-1"]}


def test_episodic_memory_is_retrievable_but_not_preloaded_every_turn(tmp_path, monkeypatch):
    storage = _storage(tmp_path, monkeypatch)
    event = upsert_memory(
        "user-a",
        "episodic",
        "proxy_connection_incident",
        "上次连接失败由代理配置导致",
        "conversation-a",
        connection_factory=storage,
    )

    assert core_memories("user-a", connection_factory=storage) == []
    found = retrieve_memories("user-a", "之前连接为什么失败", connection_factory=storage)
    assert [item["id"] for item in found] == [event["id"]]
    assert found[0]["content"] == "历史事件：上次连接失败由代理配置导致"
