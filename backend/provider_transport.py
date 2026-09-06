"""Network transport policy for model providers.

Providers default to direct outbound access.  A deployment can optionally use
the process/system proxy environment or an explicit HTTP proxy without
affecting local Ollama and vLLM runtimes.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse


CONNECTION_MODES = {"direct", "system_proxy", "custom_proxy"}
LOCAL_PROVIDER_KINDS = {"ollama", "vllm"}


def normalize_connection(mode: str | None, proxy_url: str | None) -> tuple[str, str]:
    """Validate and normalize persisted provider network settings."""

    normalized_mode = (mode or "direct").strip() or "direct"
    normalized_proxy = (proxy_url or "").strip()
    if normalized_mode not in CONNECTION_MODES:
        raise ValueError("connection_mode must be direct, system_proxy, or custom_proxy")
    if normalized_mode != "custom_proxy":
        return normalized_mode, ""

    parsed = urlparse(normalized_proxy)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("custom proxy must be a valid http:// or https:// URL")
    return normalized_mode, normalized_proxy.rstrip("/")


def client_options(provider: Mapping[str, object]) -> dict[str, object]:
    """Return safe httpx options for a provider's selected connection mode."""

    if str(provider.get("kind") or "") in LOCAL_PROVIDER_KINDS:
        # Never send localhost traffic through a corporate or desktop proxy.
        return {"trust_env": False}

    mode, proxy_url = normalize_connection(
        str(provider.get("connection_mode") or "direct"),
        str(provider.get("proxy_url") or ""),
    )
    if mode == "system_proxy":
        return {"trust_env": True}
    if mode == "custom_proxy":
        return {"trust_env": False, "proxy": proxy_url}
    return {"trust_env": False}


def connection_label(provider: Mapping[str, object]) -> str:
    """A non-sensitive value suitable for application logs."""

    if str(provider.get("kind") or "") in LOCAL_PROVIDER_KINDS:
        return "local_direct"
    return str(provider.get("connection_mode") or "direct")
