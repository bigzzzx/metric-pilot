"""Bounded model-only retries and shared graph model deadline; no tool retries."""
import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

import httpx

from backend.observability import log_event

MODEL_CALL_SECONDS = 60.0
GRAPH_MODEL_SECONDS = 90.0
RETRY_DELAY_SECONDS = 0.3
model_deadline = ContextVar("model_deadline", default=None)
model_node = ContextVar("model_node", default="outside_graph")


class ModelProviderError(RuntimeError):
    def __init__(self, message, *, status=None, code="model_provider_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def transient(exc):
    cause = exc.__cause__ or exc
    return (isinstance(cause, (httpx.ConnectError, httpx.TimeoutException,
                               httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError))
            or getattr(exc, "status", None) in {408, 429, 502, 503, 504})


def deadline():
    return min(model_deadline.get() or float("inf"), time.monotonic() + MODEL_CALL_SECONDS)


def remaining(end):
    seconds = end - time.monotonic()
    if seconds <= 0:
        raise ModelProviderError("模型响应超过总时限，请稍后重试。", code="model_deadline_exceeded")
    return seconds


@contextmanager
def model_budget():
    token = model_deadline.set(min(model_deadline.get() or float("inf"), time.monotonic() + GRAPH_MODEL_SECONDS))
    try:
        yield
    finally:
        model_deadline.reset(token)


def graph_model_budget(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with model_budget():
            return await function(*args, **kwargs)
    return wrapped


async def run_request(operation, *, end=None):
    end = end if end is not None else deadline()
    for attempt in range(2):
        started = time.monotonic()
        try:
            async with asyncio.timeout(remaining(end)):
                result = await operation()
            log_event("model.attempt", node=model_node.get(), attempt=attempt + 1,
                      status="success", duration_ms=round((time.monotonic() - started) * 1000))
            return result
        except TimeoutError as exc:
            log_event("model.attempt", node=model_node.get(), attempt=attempt + 1,
                      status="deadline", error_type=type(exc).__name__)
            raise ModelProviderError("模型响应超过总时限，请稍后重试。", code="model_deadline_exceeded") from exc
        except ModelProviderError as exc:
            retry = attempt == 0 and transient(exc) and end - time.monotonic() > RETRY_DELAY_SECONDS
            log_event("model.attempt", node=model_node.get(), attempt=attempt + 1,
                      status="error", http_status=exc.status, retry=retry,
                      error_type=type(exc.__cause__ or exc).__name__,
                      duration_ms=round((time.monotonic() - started) * 1000))
            if not retry:
                raise
            await asyncio.sleep(RETRY_DELAY_SECONDS)


async def run_stream(factory):
    """Retry only before any visible delta. Bound each next() by one shared deadline."""
    end = deadline()
    emitted = False
    for attempt in range(2):
        iterator = factory()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(iterator), remaining(end))
                except StopAsyncIteration:
                    return
                emitted = True
                yield chunk
        except TimeoutError as exc:
            log_event("model.stream_attempt", node=model_node.get(), attempt=attempt + 1,
                      status="deadline", partial=emitted)
            raise ModelProviderError("模型响应超过总时限，请稍后重试。", code="model_deadline_exceeded") from exc
        except ModelProviderError as exc:
            retry = not emitted and attempt == 0 and transient(exc) and end - time.monotonic() > RETRY_DELAY_SECONDS
            log_event("model.stream_attempt", node=model_node.get(), attempt=attempt + 1,
                      status="error", partial=emitted, retry=retry, http_status=exc.status,
                      error_type=type(exc.__cause__ or exc).__name__)
            if not retry:
                raise
        finally:
            await iterator.aclose()
        await asyncio.sleep(RETRY_DELAY_SECONDS)
