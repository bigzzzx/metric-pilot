"""Adapt LangGraph events to the existing frontend protocol; never dispatch nodes."""
import time

from backend.graph_runtime.events import streaming_answer
from backend.observability import log_event


async def stream_graph(graph, initial: dict, output_keys: set[str]):
    token = streaming_answer.set(True)
    started = time.perf_counter()
    state = {}
    emitted_answer = False
    first_token_ms = None
    try:
        async for mode, payload in graph.astream(initial, stream_mode=["custom", "updates"]):
            if mode == "custom":
                if payload.get("event") == "delta":
                    emitted_answer = True
                    if first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                        log_event("graph.first_token", duration_ms=first_token_ms)
                yield payload
            elif mode == "updates":
                for update in payload.values():
                    if isinstance(update, dict):
                        state.update({key: value for key, value in update.items() if key in output_keys})
        if state.get("approval"):
            yield {"event": "approval_required", **state["approval"]}
        if state.get("permission_error"):
            yield {"event": "permission_denied", **state["permission_error"]}
        if not emitted_answer:
            answer = str(state.get("final", state.get("plan", {})).get("answer") or "")
            if answer:
                # Deterministic tool/clarification messages are not model token streams.
                yield {"event": "delta", "content": answer}
        yield {"event": "done", "state": state}
    finally:
        streaming_answer.reset(token)
        log_event("graph.completed", duration_ms=round((time.perf_counter() - started) * 1000, 2),
                  first_token_ms=first_token_ms)
