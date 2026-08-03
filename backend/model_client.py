"""OpenAI-compatible model adapter. Provider details remain outside the agent."""
import json
import re

import httpx
from fastapi import HTTPException

from backend.core import db, secrets


def parse_model_object(content: str) -> dict:
    """Accept JSON mode, fenced JSON, and providers that return plain text."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"answer": str(value)}
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try: return json.loads(match.group(0))
            except json.JSONDecodeError: pass
        return {"answer": content}


async def complete_json(agent_id: str, messages: list[dict], model_override: str | None = None) -> dict:
    connection = db()
    row = connection.execute("SELECT a.system_prompt,m.model_id,p.base_url,p.api_key,p.kind FROM agents a JOIN models m ON m.id=COALESCE(?,a.model_id) JOIN providers p ON m.provider_id=p.id WHERE a.id=? AND a.enabled=1 AND m.enabled=1 AND p.enabled=1", (model_override, agent_id)).fetchone()
    connection.close()
    if not row: raise HTTPException(404, "No enabled agent and model configuration found")
    if not row["api_key"] and row["kind"] not in {"ollama", "vllm"}: raise HTTPException(400, "The provider has no API key")
    messages[0]["content"] = row["system_prompt"] + "\n\n" + messages[0]["content"]
    try:
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
            key = secrets().decrypt(row["api_key"].encode()).decode() if row["api_key"] else ""
            if row["kind"] == "anthropic-messages":
                system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
                chat = [{"role": item["role"], "content": item["content"]} for item in messages if item["role"] in {"user", "assistant"}]
                response = await client.post(row["base_url"].rstrip("/") + "/messages", headers={"x-api-key":key,"anthropic-version":"2023-06-01"}, json={"model":row["model_id"],"system":system,"messages":chat,"max_tokens":2048,"temperature":0.2})
                response.raise_for_status()
                return parse_model_object(response.json()["content"][0]["text"])
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            response = await client.post(row["base_url"].rstrip("/") + "/chat/completions", headers=headers, json={"model":row["model_id"],"messages":messages,"temperature":0.2,"response_format":{"type":"json_object"}})
            response.raise_for_status()
            return parse_model_object(response.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(502, f"Model provider request failed: {str(exc)[:160]}") from exc
