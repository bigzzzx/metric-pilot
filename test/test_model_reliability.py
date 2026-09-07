import asyncio

import httpx
import pytest

from backend import model_client
from backend.model_client import ModelOutputError, ModelProviderError, _response_content, complete_structured
from backend.schemas import AnalysisOutput, RouteOutput


def test_route_contract_rejects_unknown_value_and_extra_field():
    with pytest.raises(Exception):
        RouteOutput.model_validate_json('{"route":"unknown","unexpected":true}')


def test_analysis_contract_rejects_missing_required_summary():
    with pytest.raises(Exception):
        AnalysisOutput.model_validate_json('{"answer":"only answer"}')


def test_provider_response_with_empty_choices_is_a_provider_error():
    response = httpx.Response(200, json={"choices": []})
    with pytest.raises(ModelProviderError, match="unexpected response shape"):
        _response_content(response, "openai-compatible")


def test_provider_response_with_reasoning_but_no_final_content_is_recoverable(caplog):
    response = httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {
            "content": "", "reasoning_content": "internal reasoning", "tool_calls": [],
        }}],
        "usage": {"completion_tokens": 12},
    })
    with caplog.at_level("INFO", logger="data_agent"):
        with pytest.raises(ModelProviderError) as caught:
            _response_content(response, "openai-compatible")
    assert caught.value.code == "model_empty_content"
    assert caught.value.status == 200
    assert "internal reasoning" not in caplog.text
    assert '"content_chars": 0' in caplog.text
    assert '"reasoning_chars": 18' in caplog.text


def test_invalid_model_output_is_repaired_once(monkeypatch):
    replies = iter(['{"route":"not-a-route"}', '{"route":"general"}'])

    async def fake_request(*_args, **_kwargs):
        return next(replies)

    monkeypatch.setattr(model_client, "_request_content", fake_request)
    result = asyncio.run(complete_structured("general-agent", [{"role":"system", "content":"test"}], RouteOutput))
    assert result.route == "general"


def test_invalid_model_output_fails_after_one_repair(monkeypatch):
    async def fake_request(*_args, **_kwargs):
        return '{"route":"not-a-route"}'

    monkeypatch.setattr(model_client, "_request_content", fake_request)
    with pytest.raises(ModelOutputError, match="after one repair attempt"):
        asyncio.run(complete_structured("general-agent", [{"role":"system", "content":"test"}], RouteOutput))
