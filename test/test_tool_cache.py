"""Exact tool-cache behavior, permission isolation and failure fallback."""
import json
import sqlite3

from backend.main import init_storage
from backend.mcp.repository import create_server, delete_server
from backend.tools.cache import RedisToolCache
from backend.tools.executor import ToolExecutor
from backend.tools.models import (
    HighRiskOperationInput,
    HighRiskOperationOutput,
    RiskLevel,
    ToolExecutionContext,
    ToolSpec,
    ToolStatus,
)
from backend.tools.registry import ToolRegistry


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.expirations[key] = ex

    def delete(self, key):
        self.values.pop(key, None)

    def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = str(value)
        return value

    def ping(self):
        return True


def _factory(path):
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    return connect


def _runtime(tmp_path, calls, client=None, risk_level=RiskLevel.LOW):
    factory = _factory(tmp_path / "tool-cache.db")
    init_storage(factory)
    connection = factory()
    connection.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('cache-user','cache-user','hash',1)")
    connection.execute(
        "INSERT INTO agents(id,name,description,model_id,tools,system_prompt,enabled,mcp_servers,skills) "
        "VALUES ('cache-agent','Cache Agent','test','model','[\"cache_test\"]','test',1,'[]','[]')"
    )
    connection.commit(); connection.close()
    registry = ToolRegistry()

    def handler(payload, _context):
        calls.append(payload.summary)
        return {"accepted": True, "message": payload.summary}

    registry.register(ToolSpec(
        id="cache_test", name="缓存测试", description="test",
        input_schema=HighRiskOperationInput, output_schema=HighRiskOperationOutput,
        handler=handler, risk_level=risk_level,
        allowed_agents=frozenset({"cache-agent"}), cache_ttl_seconds=60,
    ))
    redis = client or FakeRedis()
    return ToolExecutor(registry, factory, RedisToolCache(redis, factory, "test:tool-cache")), factory, redis


def _context(call_id, cache_mode="default"):
    return ToolExecutionContext(
        user_id="cache-user", agent_id="cache-agent", call_id=call_id, cache_mode=cache_mode,
    )


def _arguments(summary="相同参数"):
    return {"operation": "read", "summary": summary, "target": "orders", "estimated_effects": []}


def test_identical_tool_call_hits_cache_and_writes_a_fresh_audit(tmp_path):
    calls = []
    runtime, factory, redis = _runtime(tmp_path, calls)

    first = runtime.invoke("cache_test", _arguments(), _context("call-1"))
    second = runtime.invoke("cache_test", _arguments(), _context("call-2"))

    assert calls == ["相同参数"]
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert 60 in redis.expirations.values()
    connection = factory()
    audits = connection.execute(
        "SELECT tool_call_id,detail_json FROM tool_audit_logs ORDER BY created_at,id"
    ).fetchall()
    connection.close()
    assert [row["tool_call_id"] for row in audits] == ["call-1", "call-2"]
    assert json.loads(audits[1]["detail_json"])["cache_hit"] is True


def test_permission_change_and_refresh_mode_cannot_reuse_old_entry(tmp_path):
    calls = []
    runtime, factory, _redis = _runtime(tmp_path, calls)
    runtime.invoke("cache_test", _arguments(), _context("call-1"))
    connection = factory()
    connection.execute(
        "INSERT INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect) "
        "VALUES ('cache-policy','user','cache-user','platform','*','discover','allow')"
    )
    connection.commit(); connection.close()

    changed = runtime.invoke("cache_test", _arguments(), _context("call-2"))
    refreshed = runtime.invoke("cache_test", _arguments(), _context("call-3", "refresh"))
    reused = runtime.invoke("cache_test", _arguments(), _context("call-4"))

    assert calls == ["相同参数", "相同参数", "相同参数"]
    assert changed.cache_hit is False
    assert refreshed.cache_hit is False
    assert reused.cache_hit is True


def test_deleting_mcp_server_invalidates_existing_tool_cache_entry(tmp_path):
    calls = []
    runtime, factory, _redis = _runtime(tmp_path, calls)
    server = create_server(
        name="cache-mcp", description="", transport="streamable-http",
        endpoint="http://127.0.0.1:18765/mcp", auth_type="none", api_key="",
        timeout_seconds=10, enabled=True, connection_factory=factory,
    )

    first = runtime.invoke("cache_test", _arguments(), _context("call-1"))
    assert first.cache_hit is False
    assert runtime.invoke("cache_test", _arguments(), _context("call-2")).cache_hit is True

    delete_server(server["id"], factory)
    after_delete = runtime.invoke("cache_test", _arguments(), _context("call-3"))

    assert after_delete.cache_hit is False
    assert calls == ["相同参数", "相同参数"]


def test_redis_failure_falls_back_to_normal_tool_execution(tmp_path):
    class BrokenRedis(FakeRedis):
        def get(self, _key):
            raise ConnectionError("redis unavailable")

        def set(self, _key, _value, ex=None):
            raise ConnectionError("redis unavailable")

    calls = []
    runtime, _factory, _redis = _runtime(tmp_path, calls, BrokenRedis())

    first = runtime.invoke("cache_test", _arguments(), _context("call-1"))
    second = runtime.invoke("cache_test", _arguments(), _context("call-2"))

    assert first.cache_hit is False and second.cache_hit is False
    assert calls == ["相同参数", "相同参数"]


def test_high_risk_tool_never_uses_cache_to_bypass_approval(tmp_path):
    calls = []
    runtime, _factory, redis = _runtime(tmp_path, calls, risk_level=RiskLevel.HIGH)

    first = runtime.invoke("cache_test", _arguments(), _context("call-1"))
    second = runtime.invoke("cache_test", _arguments(), _context("call-2"))

    assert first.status is ToolStatus.APPROVAL_REQUIRED
    assert second.status is ToolStatus.APPROVAL_REQUIRED
    assert first.cache_hit is False and second.cache_hit is False
    assert calls == []
    assert redis.values == {}
