"""Exit non-zero when the API or one of its required dependencies is unavailable."""
from __future__ import annotations

import json
import os

import httpx


def main() -> None:
    port = int(os.getenv("DATA_AGENT_API_PORT", "18001"))
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=12, trust_env=False)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SystemExit(f"Runtime health check failed: {type(exc).__name__}") from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if response.status_code != 200 or payload.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
