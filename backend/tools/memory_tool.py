"""Server-scoped long-term-memory search tool."""
from __future__ import annotations

from typing import Any

from backend.memory_retrieval import hybrid_search_memories


def search_long_term_memory(
    user_id: str,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search only the authenticated user's memories.

    ``user_id`` is supplied by the graph runtime, never by model output.
    """

    if not user_id.strip():
        return []
    normalized_query = " ".join(query.strip().split())
    if not normalized_query:
        return []
    if len(normalized_query) > 300:
        normalized_query = normalized_query[:300]
    return hybrid_search_memories(
        user_id,
        normalized_query,
        limit=max(1, min(int(limit), 5)),
    )

