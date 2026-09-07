import asyncio
from types import SimpleNamespace

import httpx
import pytest

from backend import model_client
from backend import model_reliability as reliability
from backend.model_reliability import ModelProviderError, run_request, run_stream


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(reliability, "RETRY_DELAY_SECONDS", 0)


@pytest.mark.parametrize("status,expected", [(400, 1), (401, 1), (403, 1), (429, 2), (503, 2)])
def test_retry_only_transient_status(status, expected):
    calls = []
    async def operation():
        calls.append(1)
        raise ModelProviderError("upstream", status=status)
    with pytest.raises(ModelProviderError):
        asyncio.run(run_request(operation))
    assert len(calls) == expected


def test_connection_failure_retries_once_then_succeeds():
    calls = []
    async def operation():
        calls.append(1)
        if len(calls) == 1:
            raise ModelProviderError("connect") from httpx.ConnectError("offline")
        return "ok"
    assert asyncio.run(run_request(operation)) == "ok"
    assert len(calls) == 2


def test_non_json_response_is_not_network_retried():
    calls = []
    async def operation():
        calls.append(1)
        return model_client._response_content(httpx.Response(200, text="not JSON"), "openai-compatible")
    with pytest.raises(ModelProviderError):
        asyncio.run(run_request(operation))
    assert len(calls) == 1


def test_empty_structured_content_retries_once_then_succeeds():
    calls = []
    async def operation():
        calls.append(1)
        if len(calls) == 1:
            raise ModelProviderError("empty", status=200, code="model_empty_content")
        return "ok"
    assert asyncio.run(run_request(operation)) == "ok"
    assert len(calls) == 2


def test_empty_structured_content_fails_after_one_retry():
    calls = []
    async def operation():
        calls.append(1)
        raise ModelProviderError("empty", status=200, code="model_empty_content")
    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run_request(operation))
    assert caught.value.code == "model_empty_content"
    assert len(calls) == 2


def test_total_deadline_cancels_slow_model_without_retry(monkeypatch):
    monkeypatch.setattr(reliability, "MODEL_CALL_SECONDS", 0.02)
    calls, closed = [], []
    async def operation():
        calls.append(1)
        try:
            await asyncio.sleep(1)
        finally:
            closed.append(1)
    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run_request(operation))
    assert caught.value.code == "model_deadline_exceeded"
    assert len(calls) == len(closed) == 1


def test_graph_budget_shared_by_multiple_requests(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reliability, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    async def slow():
        clock[0] += reliability.GRAPH_MODEL_SECONDS + 1
        return "ok"
    async def run():
        with reliability.model_budget():
            assert await run_request(slow) == "ok"
            with pytest.raises(ModelProviderError) as caught:
                await run_request(slow)
            assert caught.value.code == "model_deadline_exceeded"
        assert reliability.model_deadline.get() is None
    asyncio.run(run())


@pytest.mark.parametrize("partial", [False, True])
def test_stream_retry_never_duplicates_visible_text(partial):
    calls, chunks, closed = [], [], []
    async def factory():
        calls.append(1)
        try:
            if partial:
                yield "first"
            if len(calls) == 1:
                raise ModelProviderError("read") from httpx.ReadError("lost")
            yield "ok"
        finally:
            closed.append(1)
    async def run():
        async for chunk in run_stream(factory):
            chunks.append(chunk)
    if partial:
        with pytest.raises(ModelProviderError):
            asyncio.run(run())
        assert chunks == ["first"] and len(calls) == 1
    else:
        asyncio.run(run())
        assert chunks == ["ok"] and len(calls) == 2
    assert len(closed) == len(calls)


def test_stream_deadline_includes_all_chunks(monkeypatch):
    monkeypatch.setattr(reliability, "MODEL_CALL_SECONDS", 0.03)
    calls = []
    async def factory():
        calls.append(1)
        yield "first"
        await asyncio.sleep(1)
        yield "late"
    async def run():
        return [chunk async for chunk in run_stream(factory)]
    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(run())
    assert caught.value.code == "model_deadline_exceeded" and len(calls) == 1


def test_provider_client_actually_uses_retry(monkeypatch):
    calls = []
    row = {"system_prompt": "test", "api_key": "", "kind": "vllm",
           "provider_name": "fixture", "model_id": "test", "base_url": "http://fixture.invalid"}
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(model_client, "_configured_model", lambda *args: row)
    monkeypatch.setattr(model_client.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    content = asyncio.run(model_client._request_content("a", [{"role": "system", "content": "test"}]))
    assert content == '{"ok":true}' and len(calls) == 2


def test_provider_client_retries_empty_200_response(monkeypatch):
    calls = []
    row = {"system_prompt": "test", "api_key": "", "kind": "vllm",
           "provider_name": "fixture", "model_id": "test", "base_url": "http://fixture.invalid"}
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": "", "reasoning_content": "not a final answer",
            }}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(model_client, "_configured_model", lambda *args: row)
    monkeypatch.setattr(model_client.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    content = asyncio.run(model_client._request_content("a", [{"role": "system", "content": "test"}]))
    assert content == '{"ok":true}' and len(calls) == 2
