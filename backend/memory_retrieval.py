"""Bounded hybrid retrieval for durable user memories."""
from __future__ import annotations

import math
from typing import Any

from backend.hybrid_retrieval import (
    bm25_rank,
    reciprocal_rank_fusion,
    rerank_or_fallback,
)
from backend.memory_store import list_active_memories
from backend.memory_vector_store import (
    indexed_versions,
    memory_vectors_enabled,
    search_memory_vectors,
    upsert_memory_vectors,
)
from backend.observability import log_event
from backend.retrieval_models import (
    RetrievalModelError,
    embed_text,
    embed_texts,
    hash_embedding,
    setting,
)
from backend.vector_store import VectorStoreError


def _vector_score(hit: dict[str, Any]) -> float:
    try:
        return float(hit.get("distance", hit.get("score", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _local_semantic_rank(
    query: str, memories: list[dict[str, Any]], limit: int, threshold: float
) -> list[dict[str, Any]]:
    """Degraded semantic search when Milvus is disabled or unavailable."""

    bounded = memories[:200]
    query_vector = hash_embedding(query)
    vectors = [hash_embedding(str(item.get("content", ""))) for item in bounded]
    ranked = [
        {**item, "vector_score": _cosine(query_vector, vector)}
        for item, vector in zip(bounded, vectors)
    ]
    return [
        item
        for item in sorted(
            ranked, key=lambda item: item["vector_score"], reverse=True
        )
        if item["vector_score"] >= threshold
    ][:limit]


def _milvus_semantic_rank(
    user_id: str,
    query: str,
    memories: list[dict[str, Any]],
    limit: int,
    threshold: float,
) -> list[dict[str, Any]]:
    versions = indexed_versions(user_id)
    pending = [
        item
        for item in memories
        if versions.get(str(item["id"])) != str(item.get("updated_at", ""))
    ]
    if pending:
        vectors = embed_texts([str(item.get("content", "")) for item in pending])
        upsert_memory_vectors(
            [{**item, "vector": vector} for item, vector in zip(pending, vectors)]
        )

    by_id = {str(item["id"]): item for item in memories}
    hits = search_memory_vectors(embed_text(query, query=True), user_id, limit)
    ranked: list[dict[str, Any]] = []
    for hit in hits:
        identifier = str(hit.get("id", ""))
        score = _vector_score(hit)
        if identifier in by_id and score >= threshold:
            ranked.append({**by_id[identifier], "vector_score": score})
    return ranked


def hybrid_search_memories(
    user_id: str,
    query: str,
    *,
    limit: int = 5,
    connection_factory=None,
) -> list[dict[str, Any]]:
    """Return only relevant memories, isolated to ``user_id`` and capped at five."""

    query = " ".join(query.strip().split())
    if not user_id or not query:
        return []
    limit = max(1, min(int(limit), 5))
    memories = list_active_memories(
        user_id, connection_factory=connection_factory
    )
    if not memories:
        return []

    candidate_limit = max(10, limit * 3)
    searchable = [
        {
            **item,
            "content": " ".join(
                str(item.get(field, ""))
                for field in ("memory_key", "content", "value")
            ),
        }
        for item in memories
    ]
    lexical = bm25_rank(query, searchable, candidate_limit)
    originals = {str(item["id"]): item for item in memories}
    lexical = [
        {**originals[str(item["id"])], "bm25_score": item["bm25_score"]}
        for item in lexical
    ]

    threshold = float(setting("MEMORY_VECTOR_SCORE_THRESHOLD", "0.42"))
    semantic: list[dict[str, Any]] = []
    try:
        if memory_vectors_enabled():
            semantic = _milvus_semantic_rank(
                user_id, query, memories, candidate_limit, threshold
            )
        else:
            semantic = _local_semantic_rank(
                query, memories, candidate_limit, threshold
            )
    except (RetrievalModelError, VectorStoreError, ValueError) as exc:
        log_event(
            "memory.semantic_retrieval_degraded",
            level=30,
            user_id=user_id,
            error_type=type(exc).__name__,
        )

    fused = reciprocal_rank_fusion(lexical, semantic)
    if not fused:
        return []
    result = rerank_or_fallback(query, fused[:candidate_limit], limit)
    log_event(
        "memory.retrieved",
        user_id=user_id,
        lexical_candidates=len(lexical),
        semantic_candidates=len(semantic),
        returned=len(result),
    )
    return result

