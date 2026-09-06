"""Request-scoped, privacy-aware logs for troubleshooting local runs."""
import contextvars
import json
import logging
import uuid

from backend.core import DATA_DIR

_request_id = contextvars.ContextVar("request_id", default="-")
logger = logging.getLogger("data_agent")


def configure_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(DATA_DIR / "api.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


def start_request(request_id: str | None = None):
    request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
    return request_id, _request_id.set(request_id)


def finish_request(token) -> None:
    _request_id.reset(token)


def current_request_id() -> str:
    return _request_id.get()


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    """One JSON log line. Never pass prompts, API keys, or full query results here."""
    payload = {"event": event, "request_id": current_request_id(), **fields}
    logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))


def log_exception(event: str, **fields) -> None:
    """Write the full traceback to the local log while keeping client errors safe."""
    payload = {"event": event, "request_id": current_request_id(), **fields}
    logger.exception(json.dumps(payload, ensure_ascii=False, default=str))
