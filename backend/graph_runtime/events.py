"""Request-local streaming, independent of graph routing."""
from contextvars import ContextVar

from langgraph.config import get_stream_writer

streaming_answer = ContextVar("streaming_answer", default=False)


def emit(event: dict) -> None:
    # Unit tests can invoke a node outside a graph execution.
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(event)
