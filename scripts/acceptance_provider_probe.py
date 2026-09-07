"""Bounded read-only provider probe. Print response metadata, never prompts/keys/text."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    import backend.core as core
    core.PLATFORM_DB = core.DATA_DIR / "acceptance-browser" / "platform.db"
    core.db.__defaults__ = (core.PLATFORM_DB,)
    from backend.model_client import _configured_model, _with_contract_prompt
    from backend.provider_transport import client_options
    import httpx

    row = _configured_model("data-query-agent", None)
    assert row["kind"] == "openai-compatible"
    key = core.secrets().decrypt(row["api_key"].encode()).decode()
    messages = _with_contract_prompt([
        {"role": "system", "content": 'Classify intent. Return {"intent":"CHAT"} for a concept question.'},
        {"role": "user", "content": "刚才的PAID是什么意思？"},
    ], row["system_prompt"])
    results = []
    async with httpx.AsyncClient(timeout=45, **client_options(row)) as client:
        for _ in range(3):
            response = await client.post(row["base_url"].rstrip("/") + "/chat/completions",
                                         headers={"Authorization": "Bearer " + key},
                                         json={"model": row["model_id"], "messages": messages,
                                               "temperature": 0.2, "response_format": {"type": "json_object"}})
            response.raise_for_status()
            payload = response.json()
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            metadata = {"http_status": response.status_code, "finish_reason": choice.get("finish_reason"),
                        "content_chars": len(message.get("content") or ""),
                        "reasoning_chars": len(message.get("reasoning_content") or ""),
                        "refusal_present": bool(message.get("refusal")),
                        "tool_calls": len(message.get("tool_calls") or []),
                        "usage": payload.get("usage")}
            results.append(metadata)
            print(json.dumps(metadata, ensure_ascii=False))
    (core.PLATFORM_DB.parent / "provider-probe.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
