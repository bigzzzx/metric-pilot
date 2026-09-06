"""Provider transport and strict structured-output validation."""
import json
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from backend.core import db, secrets
from backend.observability import log_event
from backend.provider_transport import client_options, connection_label
from backend.model_reliability import ModelProviderError, run_request, run_stream
from backend.model_metrics import record_metric

T = TypeVar("T", bound=BaseModel)

class ModelOutputError(RuntimeError):
    """The provider answered, but its output does not match the node contract."""


def _configured_model(agent_id: str, model_override: str | None):
    connection = db()
    row = connection.execute("SELECT a.system_prompt,m.model_id,p.name provider_name,p.base_url,p.api_key,p.kind,p.connection_mode,p.proxy_url FROM agents a JOIN models m ON m.id=COALESCE(?,a.model_id) JOIN providers p ON m.provider_id=p.id WHERE a.id=? AND a.enabled=1 AND m.enabled=1 AND p.enabled=1", (model_override, agent_id)).fetchone()
    connection.close()
    if not row:
        raise ModelProviderError("No enabled agent and model configuration found")
    if not row["api_key"] and row["kind"] not in {"ollama", "vllm"}:
        raise ModelProviderError("The provider has no API key")
    item = dict(row)
    return item


def _with_contract_prompt(messages: list[dict], system_prompt: str) -> list[dict]:
    """Never mutate the graph's history; every provider sees the JSON contract."""
    prepared = [dict(message) for message in messages]
    contract = "Return exactly one valid JSON object. Do not use Markdown fences or add prose outside the JSON object."
    prepared[0]["content"] = system_prompt + "\n\n" + contract + "\n\n" + prepared[0]["content"]
    return prepared


def _with_text_prompt(messages: list[dict], system_prompt: str) -> list[dict]:
    """Prepare a natural-language final answer without mutating graph history."""
    prepared = [dict(message) for message in messages]
    stream_contract = "STREAMING OUTPUT MODE: Return plain natural-language text only. Do not return JSON, code fences, keys, or a wrapper object. This instruction overrides any earlier JSON-only instruction."
    prepared[0]["content"] = system_prompt + "\n\n" + stream_contract + "\n\n" + prepared[0]["content"]
    return prepared


def _response_content(response: httpx.Response, provider_kind: str) -> str:
    if response.is_error:
        raise ModelProviderError(f"Model provider returned HTTP {response.status_code}", status=response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelProviderError("Model provider returned a non-JSON success response") from exc
    try:
        if provider_kind == "anthropic-messages":
            blocks = payload["content"]
            content = next(block["text"] for block in blocks if block.get("type") == "text")
        else:
            choices = payload["choices"]
            content = choices[0]["message"]["content"]
    except (KeyError, IndexError, StopIteration, TypeError) as exc:
        raise ModelProviderError("Model provider returned an unexpected response shape") from exc
    if not isinstance(content, str) or not content.strip():
        raise ModelProviderError("Model provider returned empty content")
    return content


async def _request_content(agent_id: str, messages: list[dict], model_override: str | None = None) -> str:
    started = time.perf_counter()
    success = False
    try:
        content = await run_request(lambda: _request_content_once(agent_id, messages, model_override))
        success = True
        return content
    finally:
        record_metric(mode="structured", agent_id=agent_id, success=success,
                      input_message_chars=sum(len(str(m.get("content", ""))) for m in messages),
                      duration_ms=round((time.perf_counter() - started) * 1000, 2))


async def _request_content_once(agent_id: str, messages: list[dict], model_override: str | None = None) -> str:
    row = _configured_model(agent_id, model_override)
    prepared = _with_contract_prompt(messages, row["system_prompt"])
    try:
        log_event("model.request", agent_id=agent_id, provider=row["provider_name"], model=row["model_id"], mode="structured", connection=connection_label(row))
        async with httpx.AsyncClient(timeout=45, **client_options(row)) as client:
            key = secrets().decrypt(row["api_key"].encode()).decode() if row["api_key"] else ""
            if row["kind"] == "anthropic-messages":
                system = "\n\n".join(item["content"] for item in prepared if item["role"] == "system")
                chat = [{"role": item["role"], "content": item["content"]} for item in prepared if item["role"] in {"user", "assistant"}]
                response = await client.post(row["base_url"].rstrip("/") + "/messages", headers={"x-api-key":key,"anthropic-version":"2023-06-01"}, json={"model":row["model_id"],"system":system,"messages":chat,"max_tokens":2048,"temperature":0.2})
                content = _response_content(response, row["kind"])
                log_event("model.response", agent_id=agent_id, provider=row["provider_name"], status=response.status_code, mode="structured")
                return content
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            request = {"model":row["model_id"],"messages":prepared,"temperature":0.2}
            if row["kind"] in {"openai-compatible", "vllm"}:
                request["response_format"] = {"type":"json_object"}
            response = await client.post(row["base_url"].rstrip("/") + "/chat/completions", headers=headers, json=request)
            content = _response_content(response, row["kind"])
            log_event("model.response", agent_id=agent_id, provider=row["provider_name"], status=response.status_code, mode="structured")
            return content
    except ModelProviderError:
        log_event("model.provider_error", level=30, agent_id=agent_id, provider=row["provider_name"], model=row["model_id"], mode="structured")
        raise
    except (httpx.HTTPError, ValueError) as exc:
        log_event("model.transport_error", level=30, agent_id=agent_id, provider=row["provider_name"], error_type=type(exc).__name__)
        raise ModelProviderError(f"Model provider request failed: {type(exc).__name__}") from exc


async def complete_structured(agent_id: str, messages: list[dict], schema: type[T], model_override: str | None = None) -> T:
    """Get a schema-valid output, asking once for a correction if formatting is wrong."""
    started_at = time.perf_counter()
    retry_messages = list(messages)
    for attempt in range(2):
        content = await _request_content(agent_id, retry_messages, model_override)
        try:
            result = schema.model_validate_json(content)
            log_event(
                "model.structured_completed",
                agent_id=agent_id,
                schema=schema.__name__,
                attempts=attempt + 1,
                duration_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return result
        except ValidationError as exc:
            if attempt:
                log_event("model.schema_error", level=30, agent_id=agent_id, schema=schema.__name__, attempt=attempt + 1)
                raise ModelOutputError(f"Model output did not match {schema.__name__} after one repair attempt: {exc.errors()[0]['msg']}") from exc
            log_event("model.schema_repair", level=30, agent_id=agent_id, schema=schema.__name__, attempt=attempt + 1)
            repair = (
                "Your previous response violated the required JSON schema: "
                + exc.errors()[0]["msg"]
                + ". Return a corrected JSON object only. Required schema: "
                + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            )
            retry_messages = [*messages, {"role":"user", "content":repair}]
    raise ModelOutputError("Model output repair did not complete")


async def stream_text(agent_id: str, messages: list[dict], model_override: str | None = None) -> AsyncGenerator[str, None]:
    started = time.perf_counter()
    success, first = False, None
    try:
        async with aclosing(run_stream(lambda: _stream_text_once(agent_id, messages, model_override))) as stream:
            async for delta in stream:
                if first is None:
                    first = round((time.perf_counter() - started) * 1000, 2)
                yield delta
        success = True
    finally:
        record_metric(mode="stream", agent_id=agent_id, success=success, first_token_ms=first,
                      input_message_chars=sum(len(str(m.get("content", ""))) for m in messages),
                      duration_ms=round((time.perf_counter() - started) * 1000, 2))


async def _stream_text_once(agent_id: str, messages: list[dict], model_override: str | None = None) -> AsyncGenerator[str, None]:
    """Yield visible text deltas from OpenAI-compatible streaming providers."""
    started_at = time.perf_counter()
    row = _configured_model(agent_id, model_override)
    if row["kind"] == "anthropic-messages":
        raise ModelProviderError("Streaming is not configured for Anthropic Messages in this demo")
    prepared = _with_text_prompt(messages, row["system_prompt"])
    key = secrets().decrypt(row["api_key"].encode()).decode() if row["api_key"] else ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    request = {"model": row["model_id"], "messages": prepared, "temperature": 0.2, "stream": True}
    received = False
    log_event("model.request", agent_id=agent_id, provider=row["provider_name"], model=row["model_id"], mode="stream", connection=connection_label(row))
    try:
        async with httpx.AsyncClient(timeout=60, **client_options(row)) as client:
            async with client.stream("POST", row["base_url"].rstrip("/") + "/chat/completions", headers=headers, json=request) as response:
                if response.is_error:
                    raise ModelProviderError(f"Model provider returned HTTP {response.status_code}", status=response.status_code)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        content = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
                    except (json.JSONDecodeError, AttributeError, IndexError, TypeError) as exc:
                        raise ModelProviderError("Model provider returned an invalid stream chunk") from exc
                    if isinstance(content, str) and content:
                        if not received:
                            log_event(
                                "model.first_token",
                                agent_id=agent_id,
                                duration_ms=round((time.perf_counter() - started_at) * 1000),
                            )
                        received = True
                        yield content
        if not received:
            raise ModelProviderError("Model provider stream contained no text")
        log_event("model.response", agent_id=agent_id, provider=row["provider_name"], status=200, mode="stream", duration_ms=round((time.perf_counter() - started_at) * 1000))
    except ModelProviderError:
        log_event("model.provider_error", level=30, agent_id=agent_id, provider=row["provider_name"], model=row["model_id"], mode="stream")
        raise
    except httpx.HTTPError as exc:
        log_event("model.transport_error", level=30, agent_id=agent_id, provider=row["provider_name"], error_type=type(exc).__name__)
        raise ModelProviderError(f"Model provider stream failed: {type(exc).__name__}") from exc
