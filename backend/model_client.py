"""OpenAI-compatible model adapter. Provider details remain outside the agent."""
import json

import httpx
from fastapi import HTTPException

from backend.core import db, secrets


async def complete_json(agent_id: str, messages: list[dict], model_override: str | None = None) -> dict:
    connection = db()
    row = connection.execute("SELECT a.system_prompt,m.model_id,p.base_url,p.api_key,p.kind FROM agents a JOIN models m ON m.id=COALESCE(?,a.model_id) JOIN providers p ON m.provider_id=p.id WHERE a.id=? AND a.enabled=1 AND m.enabled=1 AND p.enabled=1", (model_override, agent_id)).fetchone()
    connection.close()
    if not row: raise HTTPException(404, "No enabled agent and model configuration found")
    if not row["api_key"] and row["kind"] not in {"ollama", "vllm"}: raise HTTPException(400, "The provider has no API key")
    messages[0]["content"] = row["system_prompt"] + "\n\n" + messages[0]["content"]
    try:
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
            headers = {"Authorization": f"Bearer {secrets().decrypt(row['api_key'].encode()).decode()}"} if row["api_key"] else {}
            response = await client.post(row["base_url"].rstrip("/") + "/chat/completions", headers=headers, json={"model":row["model_id"],"messages":messages,"temperature":0.2,"response_format":{"type":"json_object"}})
        response.raise_for_status()
        return json.loads(response.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Model provider request failed: {str(exc)[:160]}") from exc
