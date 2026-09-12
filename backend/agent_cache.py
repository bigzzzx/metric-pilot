"""Exact Redis caches for validated query plans and data-analysis answers."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from backend.observability import log_event


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class RedisAgentCache:
    backend_name = "redis"

    def __init__(self, client: Any, prefix: str = "data-agent:agent-cache:v1", max_entries: int = 5_000) -> None:
        self.client = client
        self.prefix = prefix.rstrip(":")
        self.max_entries = max_entries

    def _key(self, namespace: str, identity: dict[str, Any]) -> str:
        digest = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
        return f"{self.prefix}:{namespace}:{digest}"

    def get(self, namespace: str, identity: dict[str, Any]) -> dict[str, Any] | None:
        try:
            raw = self.client.get(self._key(namespace, identity))
            if raw is None:
                log_event("agent.cache_miss", namespace=namespace, backend=self.backend_name)
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return None
            log_event("agent.cache_hit", namespace=namespace, backend=self.backend_name)
            return payload
        except Exception as exc:
            log_event("agent.cache_unavailable", level=30, namespace=namespace, error_type=type(exc).__name__)
            return None

    def set(self, namespace: str, identity: dict[str, Any], payload: dict[str, Any], ttl_seconds: int) -> None:
        try:
            key = self._key(namespace, identity)
            value = _canonical({**payload, "_cached_at": datetime.now(UTC).isoformat()})
            self.client.set(key, value, ex=ttl_seconds)
            self._remember_key(key, ttl_seconds)
            log_event("agent.cache_write", namespace=namespace, backend=self.backend_name, ttl_seconds=ttl_seconds)
        except Exception as exc:
            log_event("agent.cache_unavailable", level=30, namespace=namespace, error_type=type(exc).__name__)

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


def configured_agent_cache() -> RedisAgentCache | None:
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        from redis import Redis
    except ImportError:
        return None
    client = Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.75,
        health_check_interval=30,
    )
    prefix = os.getenv("AGENT_CACHE_PREFIX", "data-agent:agent-cache:v1").strip()
    try:
        max_entries = max(1, int(os.getenv("AGENT_CACHE_MAX_ENTRIES", "5000")))
    except ValueError:
        max_entries = 5_000
    return RedisAgentCache(client, prefix or "data-agent:agent-cache:v1", max_entries)


def cache_ttl(name: str, default: int) -> int:
    try:
        return max(1, min(int(os.getenv(name, str(default))), 3600))
    except ValueError:
        return default
