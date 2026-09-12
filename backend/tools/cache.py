"""Redis-backed cache-aside storage for deterministic, read-only tools."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from backend.observability import log_event
from backend.tools.models import ToolExecutionContext, ToolSpec


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    data: Any


class ToolCache(Protocol):
    backend_name: str

    def get(self, spec: ToolSpec, arguments: dict[str, Any], context: ToolExecutionContext) -> CacheEntry | None: ...
    def set(self, spec: ToolSpec, arguments: dict[str, Any], context: ToolExecutionContext, data: Any) -> None: ...
    def delete(self, key: str) -> None: ...
    def invalidate_all(self) -> None: ...
    def ping(self) -> None: ...


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def permission_fingerprint(connection_factory: Callable[[], Any], user_id: str) -> str:
    connection = connection_factory()
    try:
        roles = [row[0] for row in connection.execute(
            "SELECT role_id FROM catalog_user_roles WHERE user_id=? ORDER BY role_id", (user_id,),
        )]
        groups = [row[0] for row in connection.execute(
            "SELECT group_id FROM catalog_group_memberships WHERE user_id=? ORDER BY group_id", (user_id,),
        )]
        subjects = {("user", user_id), *[("role", value) for value in roles], *[("group", value) for value in groups]}
        policies = [tuple(row) for row in connection.execute(
            "SELECT subject_type,subject_id,resource_type,resource_id,action,effect FROM catalog_policies "
            "ORDER BY subject_type,subject_id,resource_type,resource_id,action,effect"
        ) if (row[0], row[1]) in subjects]
        return _hash({"roles": roles, "groups": groups, "policies": policies})
    finally:
        connection.close()


def dependency_fingerprint(connection_factory: Callable[[], Any]) -> str:
    connection = connection_factory()
    try:
        values = {
            "assets": tuple(connection.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),''),COALESCE(SUM(catalog_version),0) FROM catalog_assets"
            ).fetchone()),
            "columns": tuple(connection.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM catalog_columns"
            ).fetchone()),
            "relationships": tuple(connection.execute(
                "SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM catalog_relationships"
            ).fetchone()),
            "metrics": tuple(connection.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),''),COALESCE(SUM(catalog_version),0) FROM catalog_metrics"
            ).fetchone()),
            "sources": [tuple(row) for row in connection.execute(
                "SELECT id,enabled,COALESCE(last_synced_at,'') FROM data_sources ORDER BY id"
            )],
        }
        return _hash(values)
    finally:
        connection.close()


class RedisToolCache:
    backend_name = "redis"

    def __init__(self, client: Any, connection_factory: Callable[[], Any], prefix: str = "data-agent:tool-cache:v1",
                 max_entries: int = 10_000) -> None:
        self.client = client
        self.connection_factory = connection_factory
        self.prefix = prefix.rstrip(":")
        self.max_entries = max_entries

    def _permission_fingerprint(self, user_id: str) -> str:
        return permission_fingerprint(self.connection_factory, user_id)

    def _dependency_fingerprint(self) -> str:
        return dependency_fingerprint(self.connection_factory)

    def _remember_key(self, key: str, ttl_seconds: int) -> None:
        required = ("zadd", "zcard", "zpopmin", "zremrangebyscore")
        if self.max_entries <= 0 or not all(hasattr(self.client, name) for name in required):
            return
        index = f"{self.prefix}:expiry-index"
        now = time.time()
        self.client.zremrangebyscore(index, "-inf", now)
        self.client.zadd(index, {key: now + ttl_seconds})
        overflow = int(self.client.zcard(index)) - self.max_entries
        if overflow > 0:
            victims = self.client.zpopmin(index, overflow)
            victim_keys = [item[0] if isinstance(item, (tuple, list)) else item for item in victims]
            if victim_keys:
                self.client.delete(*victim_keys)

    def _generation(self) -> str:
        value = self.client.get(f"{self.prefix}:generation")
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return str(value or "0")

    def _key(self, spec: ToolSpec, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        identity = {
            "generation": self._generation(),
            "tool_id": spec.id,
            "tool_version": spec.version,
            "user_id": context.user_id,
            "agent_id": context.agent_id,
            "arguments": arguments,
            "permissions": self._permission_fingerprint(context.user_id),
            "dependencies": self._dependency_fingerprint(),
        }
        return f"{self.prefix}:entry:{_hash(identity)}"

    def get(self, spec: ToolSpec, arguments: dict[str, Any], context: ToolExecutionContext) -> CacheEntry | None:
        try:
            key = self._key(spec, arguments, context)
            raw = self.client.get(key)
            if raw is None:
                log_event("tool.cache_miss", tool_id=spec.id, backend=self.backend_name)
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "data" not in payload:
                self.delete(key)
                return None
            log_event("tool.cache_hit", tool_id=spec.id, backend=self.backend_name)
            return CacheEntry(key=key, data=payload["data"])
        except Exception as exc:
            log_event("tool.cache_unavailable", level=30, tool_id=spec.id, error_type=type(exc).__name__)
            return None

    def set(self, spec: ToolSpec, arguments: dict[str, Any], context: ToolExecutionContext, data: Any) -> None:
        try:
            key = self._key(spec, arguments, context)
            payload = _canonical({"data": data, "cached_at": datetime.now(UTC).isoformat()})
            self.client.set(key, payload, ex=spec.cache_ttl_seconds)
            self._remember_key(key, spec.cache_ttl_seconds)
            log_event("tool.cache_write", tool_id=spec.id, backend=self.backend_name, ttl_seconds=spec.cache_ttl_seconds)
        except Exception as exc:
            log_event("tool.cache_unavailable", level=30, tool_id=spec.id, error_type=type(exc).__name__)

    def delete(self, key: str) -> None:
        try:
            self.client.delete(key)
        except Exception:
            pass

    def invalidate_all(self) -> None:
        try:
            self.client.incr(f"{self.prefix}:generation")
            log_event("tool.cache_invalidated", backend=self.backend_name)
        except Exception as exc:
            log_event("tool.cache_unavailable", level=30, error_type=type(exc).__name__)

    def ping(self) -> None:
        if not self.client.ping():
            raise RuntimeError("Redis cache ping failed")


def configured_tool_cache(connection_factory: Callable[[], Any]) -> ToolCache | None:
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        from redis import Redis
    except ImportError:
        log_event("tool.cache_unavailable", level=30, error_type="RedisClientMissing")
        return None
    client = Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.75,
        health_check_interval=30,
    )
    prefix = os.getenv("TOOL_CACHE_PREFIX", "data-agent:tool-cache:v1").strip()
    try:
        max_entries = max(1, int(os.getenv("TOOL_CACHE_MAX_ENTRIES", "10000")))
    except ValueError:
        max_entries = 10_000
    return RedisToolCache(client, connection_factory, prefix or "data-agent:tool-cache:v1", max_entries)
