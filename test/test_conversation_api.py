import sqlite3

from fastapi.testclient import TestClient

from backend import conversations
from backend import main


def test_conversation_api_restores_messages_and_uses_durable_terminal(tmp_path, monkeypatch):
    database_path = tmp_path / "conversation-api.db"

    def isolated_db():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(main, "db", isolated_db)
    monkeypatch.setattr(conversations, "db", isolated_db)

    active_user = {"id": "user-a", "username": "alice"}
    main.app.dependency_overrides[main.require_user] = lambda: active_user
    model_calls = 0

    async def successful_stream(message, _agent_id, _history, _model_id, *_memory_context):
        nonlocal model_calls
        model_calls += 1
        yield {
            "event": "done",
            "state": {
                "plan": {"answer": f"已处理：{message}"},
                "final": {"answer": f"已处理：{message}"},
                "rag": [],
                "sql": "",
                "result": [],
                "route": "data_query",
                "route_source": "model",
                "verified_schemas": {"tables": []},
                "node_trace": [{"node": "router", "label": "正在理解问题", "duration_ms": 8.5, "status": "success"}],
            },
        }

    monkeypatch.setattr(main, "run_agent_stream", successful_stream)

    try:
        with TestClient(main.app) as client:
            connection = isolated_db()
            connection.executemany(
                "INSERT INTO users(id,username,password_hash) VALUES (?,?,?)",
                [
                    ("user-a", "alice", "unused"),
                    ("user-b", "bob", "unused"),
                ],
            )
            connection.commit()
            connection.close()

            created = client.post("/api/conversations").json()
            request_body = {
                "session_id": created["id"],
                "message": "保留这条对话",
                "client_message_id": "turn-1",
            }

            first = client.post("/api/chat/stream", json=request_body)
            second = client.post("/api/chat/stream", json=request_body)

            assert first.status_code == 200
            assert second.status_code == 200
            assert "event: done" in first.text
            assert "event: done" in second.text
            assert model_calls == 1

            restored = client.get(
                f"/api/conversations/{created['id']}/messages"
            ).json()
            assert [item["role"] for item in restored["messages"]] == [
                "user",
                "assistant",
            ]
            assert restored["messages"][-1]["payload"]["answer"] == "已处理：保留这条对话"
            assert restored["messages"][-1]["payload"]["trace"]["nodes"][0]["node"] == "router"

            conflict = client.post(
                "/api/chat/stream",
                json={**request_body, "message": "不同的问题"},
            )
            assert conflict.status_code == 409

            active_user["id"] = "user-b"
            active_user["username"] = "bob"
            hidden = client.get(
                f"/api/conversations/{created['id']}/messages"
            )
            assert hidden.status_code == 404
            assert client.get("/api/conversations").json() == []

        active_user["id"] = "user-a"
        active_user["username"] = "alice"
        with TestClient(main.app) as restarted_client:
            summaries = restarted_client.get("/api/conversations").json()
            assert [item["id"] for item in summaries] == [created["id"]]
            restored_after_restart = restarted_client.get(
                f"/api/conversations/{created['id']}/messages"
            ).json()
            assert restored_after_restart["messages"][-1]["payload"]["answer"] == "已处理：保留这条对话"
            assert restored_after_restart["messages"][-1]["payload"]["trace"]["route"] == "data_query"
    finally:
        main.app.dependency_overrides.clear()
