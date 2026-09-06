from __future__ import annotations

import asyncio
import json

from starlette.responses import JSONResponse

from backend.http_limits import UploadBodyLimitMiddleware


def _scope(*, content_length: int | None = None):
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/knowledge-bases/base/documents/upload",
        "raw_path": b"/api/knowledge-bases/base/documents/upload",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 3001),
        "state": {"request_id": "req_limit_test"},
    }


def _run(messages, *, limit=10, content_length=None):
    sent = []
    inner_called = False

    async def inner(scope, receive, send):
        nonlocal inner_called
        inner_called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break
        await JSONResponse({"ok": True})(scope, receive, send)

    pending = list(messages)

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    middleware = UploadBodyLimitMiddleware(inner, max_request_bytes=limit)
    asyncio.run(middleware(_scope(content_length=content_length), receive, send))
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, json.loads(body), inner_called


def test_declared_oversized_upload_is_rejected_before_inner_app_runs():
    status, payload, inner_called = _run([], content_length=11)
    assert status == 413
    assert payload["error_code"] == "file_too_large"
    assert inner_called is False


def test_chunked_upload_is_stopped_when_actual_bytes_cross_limit():
    status, payload, inner_called = _run(
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"789012", "more_body": False},
        ]
    )
    assert status == 413
    assert payload["request_id"] == "req_limit_test"
    assert inner_called is True


def test_upload_within_limit_reaches_inner_app():
    status, payload, inner_called = _run(
        [{"type": "http.request", "body": b"1234567890", "more_body": False}],
        content_length=10,
    )
    assert status == 200
    assert payload == {"ok": True}
    assert inner_called is True
