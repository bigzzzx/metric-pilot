"""Best-effort synchronization from authoritative SQLite memories to Milvus."""

from __future__ import annotations

from typing import Any

from backend.memory_vector_store import delete_memory_vectors
from backend.observability import log_event
def synchronize_forgotten_memory_vectors(outcomes: list[dict[str, Any]]) -> None:
    """Delete forgotten vectors without turning a successful chat into an error.

    Retrieval still filters against active SQLite records, so a temporary
    Milvus cleanup failure cannot expose forgotten content.
    """

    identifiers = [
        str(item.get("memory_id") or "")
        for item in outcomes
        if item.get("operation") == "forget" and item.get("deleted")
    ]
    identifiers = [item for item in identifiers if item]
    if not identifiers:
        return
    try:
        delete_memory_vectors(identifiers)
        log_event("memory.vector_delete_synced", count=len(identifiers))
    except Exception as exc:
        log_event(
            "memory.vector_delete_deferred",
            level=30,
            count=len(identifiers),
            error_type=type(exc).__name__,
        )
