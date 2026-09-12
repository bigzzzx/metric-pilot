import json
import sqlite3

from fastapi.testclient import TestClient

from backend import conversations, main, turn_cache
from backend.schemas import ChatResponse


class DictCache:
    def __init__(self):
        self.values = {}

    @staticmethod
    def _key(namespace, identity):
        return namespace, json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)

    def get(self, namespace, identity):
        return self.values.get(self._key(namespace, identity))

    def set(self, namespace, identity, payload, _ttl_seconds):
        self.values[self._key(namespace, identity)] = payload


def test_question_normalization_and_contextual_bypass():
    assert turn_cache.normalize_question("  查询   ODS.Orders 数量  ") == "查询 ods.orders 数量"
    assert turn_cache.is_standalone_cacheable_question("查询 ods.orders 有多少行") is True
    assert turn_cache.is_standalone_cacheable_question("那华东地区呢") is False
    assert turn_cache.is_standalone_cacheable_question("查询今天的订单量") is False


def test_oversized_complete_response_is_not_cached(monkeypatch):
    cache = DictCache()
    monkeypatch.setattr(turn_cache, "_cache", cache)
    monkeypatch.setattr(turn_cache, "TURN_RESPONSE_CACHE_MAX_PAYLOAD_BYTES", 100)
    response = ChatResponse(
        answer="结果" * 100,
        intent="DATA_QUERY",
        sql="SELECT COUNT(*) FROM ods.orders",
        result=[{"row_count": 1200}],
    )
    state = {"route": "data_query", "tool_result": {"status": "success"}}

    stored = turn_cache.store_cached_response({"question": "orders"}, response, state)

    assert stored is False
    assert cache.values == {}


def test_same_question_with_new_message_id_skips_the_whole_graph(tmp_path, monkeypatch):
    database_path = tmp_path / "turn-cache-api.db"

    def isolated_db():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(main, "db", isolated_db)
    monkeypatch.setattr(conversations, "db", isolated_db)
    monkeypatch.setattr(turn_cache, "db", isolated_db)
    monkeypatch.setattr(turn_cache, "_cache", DictCache())

    active_user = {"id": "cache-user", "username": "cache-user"}
    main.app.dependency_overrides[main.require_user] = lambda: active_user
    graph_calls = 0

    async def successful_stream(message, _agent_id, _history, _model_id, *_memory_context):
        nonlocal graph_calls
        graph_calls += 1
        yield {
            "event": "done",
            "state": {
                "plan": {"answer": "共 1200 行", "source_id": "source-pg"},
                "final": {"answer": "共 1200 行"},
                "rag": [],
                "sql": "SELECT COUNT(*) AS row_count FROM ods.orders",
                "result": [{"row_count": 1200}],
                "route": "data_query",
                "route_source": "model",
                "verified_schemas": {"tables": []},
                "tool_result": {
                    "status": "success", "tool_id": "readonly_sql", "call_id": "original-call",
                    "data": {"rows": [{"row_count": 1200}]}, "audit_id": "original-audit",
                },
                "node_trace": [],
            },
        }

    monkeypatch.setattr(main, "run_agent_stream", successful_stream)

    try:
        with TestClient(main.app) as client:
            connection = isolated_db()
            connection.execute(
                "INSERT INTO users(id,username,password_hash) VALUES (?,?,?)",
                ("cache-user", "cache-user", "unused"),
            )
            connection.commit()
            connection.close()
            conversation_id = client.post("/api/conversations").json()["id"]
            body = {"session_id": conversation_id, "message": "查询 ods.orders 有多少行"}

            first = client.post("/api/chat/stream", json={**body, "client_message_id": "turn-1"})
            second = client.post("/api/chat/stream", json={**body, "client_message_id": "turn-2"})

            assert first.status_code == 200 and second.status_code == 200
            assert graph_calls == 1
            assert "已命中完整回答缓存" in second.text
            assert '"turn_hit": true' in second.text
            restored = client.get(f"/api/conversations/{conversation_id}/messages").json()
            assert [item["role"] for item in restored["messages"]] == [
                "user", "tool", "assistant", "user", "tool", "assistant",
            ]
            assert restored["messages"][-1]["payload"]["answer"] == "共 1200 行"
            assert restored["messages"][-1]["payload"]["tool_result"] is None
    finally:
        main.app.dependency_overrides.clear()
