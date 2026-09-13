"""Response-completion hook for best-effort, deduplicated conversation summaries."""
from backend.memory import compact_conversation_if_needed
from backend.observability import log_event

_running: set[tuple[str, str]] = set()


async def summarize_after_response(conversation_id: str, user_id: str) -> None:
    key = (user_id, conversation_id)
    if key in _running:
        return
    _running.add(key)
    try:
        await compact_conversation_if_needed(conversation_id, user_id)
    except Exception as exc:
        # The answer is already delivered. A later turn can retry compaction;
        # original messages remain durably stored even after a worker restart.
        log_event("memory.background_compaction_failed", level=30,
                  conversation_id=conversation_id, error_type=type(exc).__name__)
    finally:
        _running.discard(key)
