"""Small ASGI middleware for bounding untrusted request bodies."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse


ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


class _RequestBodyTooLarge(Exception):
    pass


class UploadBodyLimitMiddleware:
    """Reject oversized upload bodies while they are still being received.

    Route-level ``UploadFile`` validation runs only after Starlette has parsed
    multipart input. This middleware wraps the raw ASGI ``receive`` callable,
    so chunked requests without ``Content-Length`` cannot fill the temporary
    spool before the application notices the limit.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int,
        path_suffix: str = "/documents/upload",
    ) -> None:
        self.app = app
        self.max_request_bytes = int(max_request_bytes)
        self.path_suffix = path_suffix

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") != "http"
            or str(scope.get("method", "")).upper() != "POST"
            or not str(scope.get("path", "")).endswith(self.path_suffix)
        ):
            await self.app(scope, receive, send)
            return

        headers = {bytes(key).lower(): bytes(value) for key, value in scope.get("headers", [])}
        if self._declared_too_large(headers.get(b"content-length", b"").strip()):
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_request_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send)

    def _declared_too_large(self, raw_value: bytes) -> bool:
        if not raw_value:
            return False
        try:
            return int(raw_value) > self.max_request_bytes
        except (TypeError, ValueError):
            # Invalid Content-Length is left to the protocol layer; the receive
            # counter still enforces the number of bytes actually delivered.
            return False

    async def _reject(self, scope, receive, send) -> None:
        state = scope.get("state") or {}
        request_id = str(state.get("request_id") or f"req_{uuid.uuid4().hex[:12]}")
        response = JSONResponse(
            status_code=413,
            content={
                "detail": "The uploaded file is too large (maximum 20 MiB).",
                "code": "file_too_large",
                "error_code": "file_too_large",
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )
        await response(scope, receive, send)
