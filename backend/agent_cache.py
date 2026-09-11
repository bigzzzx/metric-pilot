"""Exact Redis caches for validated query plans and data-analysis answers."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from backend.observability import log_event


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class RedisAgentCache:
    backend_name = "redis"

    def __init__(self, client: Any, prefix: str = "data-agent:agent-cache:v1") -> None:
        self.client = client
        self.prefix = prefix.rstrip(":")

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
            value = _canonical({**payload, "_cached_at": datetime.now(UTC).isoformat()})
            self.client.set(self._key(namespace, identity), value, ex=ttl_seconds)
            log_event("agent.cache_write", namespace=namespace, backend=self.backend_name, ttl_seconds=ttl_seconds)
        except Exception as exc:
            log_event("agent.cache_unavailable", level=30, namespace=namespace, error_type=type(exc).__name__)


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
    return RedisAgentCache(client, prefix or "data-agent:agent-cache:v1")


def cache_ttl(name: str, default: int) -> int:
    try:
        return max(1, min(int(os.getenv(name, str(default))), 3600))
    except ValueError:
        return default
