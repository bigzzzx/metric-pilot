"""Milvus index adapter for durable user memories.

SQLite remains the source of truth.  This module only owns the optional
semantic index and always requires a server-owned user id for reads.
"""
from __future__ import annotations

import json
from typing import Any

from backend.retrieval_models import EMBEDDING_DIMENSIONS, setting
from backend.vector_store import VectorStoreError, using_milvus


MEMORY_COLLECTION = "user_memories_bge_small_zh_v1"


def memory_vectors_enabled() -> bool:
    """Require an explicit opt-in before user memories leave local SQLite."""

    return (
        setting("MEMORY_VECTOR_BACKEND", "disabled").lower() == "milvus"
        and using_milvus()
    )


def _client():
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise VectorStoreError("PyMilvus is not installed") from exc
    uri = setting("MILVUS_URI")
    if not uri:
        raise VectorStoreError("MILVUS_URI is not configured")
    try:
        return MilvusClient(
            uri=uri,
            db_name=setting("MILVUS_DATABASE", "data_agent"),
        )
    except Exception as exc:
        raise VectorStoreError(f"Memory Milvus connection failed: {exc}") from exc


def _collection(client) -> str:
    collection = setting("MEMORY_MILVUS_COLLECTION", MEMORY_COLLECTION)
    try:
        if client.has_collection(collection_name=collection):
            return collection
        from pymilvus import DataType

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("user_id", DataType.VARCHAR, max_length=128)
        schema.add_field("memory_type", DataType.VARCHAR, max_length=32)
        schema.add_field("content", DataType.VARCHAR, max_length=1024)
        schema.add_field("updated_at", DataType.VARCHAR, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSIONS)
        indexes = client.prepare_index_params()
        indexes.add_index(
            field_name="vector",
            index_name="memory_vector_hnsw",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 120},
        )
        client.create_collection(
            collection_name=collection,
            schema=schema,
            index_params=indexes,
        )
        return collection
    except VectorStoreError:
        raise
    except Exception as exc:
        raise VectorStoreError(f"Memory Milvus collection failed: {exc}") from exc


def indexed_versions(user_id: str) -> dict[str, str]:
    if not memory_vectors_enabled():
        return {}
    safe_user_id = user_id.replace("\\", "\\\\").replace('"', r'\"')
    client = _client()
    try:
        rows = client.query(
            collection_name=_collection(client),
            filter=f'user_id == "{safe_user_id}"',
            output_fields=["id", "updated_at"],
            limit=10_000,
            consistency_level="Strong",
        )
        return {str(row["id"]): str(row.get("updated_at", "")) for row in rows}
    except Exception as exc:
        raise VectorStoreError(f"Memory Milvus id query failed: {exc}") from exc


def upsert_memory_vectors(memories: list[dict[str, Any]]) -> None:
    if not memories or not memory_vectors_enabled():
        return
    client = _client()
    rows = [
        {
            "id": str(memory["id"]),
            "user_id": str(memory["user_id"]),
            "memory_type": str(memory.get("memory_type", ""))[:32],
            "content": str(memory.get("content", ""))[:1024],
            "updated_at": str(memory.get("updated_at", ""))[:64],
            "vector": memory["vector"],
        }
        for memory in memories
    ]
    try:
        client.upsert(collection_name=_collection(client), data=rows)
    except Exception as exc:
        raise VectorStoreError(f"Memory Milvus upsert failed: {exc}") from exc


def delete_memory_vectors(memory_ids: list[str]) -> None:
    """Remove vectors after a durable memory is explicitly forgotten.

    SQLite remains authoritative, so callers may safely retry this cleanup when
    Milvus is temporarily unavailable.
    """

    if not memory_ids or not memory_vectors_enabled():
        return
    identifiers = [str(item).strip() for item in memory_ids if str(item).strip()]
    if not identifiers:
        return
    client = _client()
    expression = "id in [" + ",".join(json.dumps(item) for item in identifiers) + "]"
    try:
        client.delete(collection_name=_collection(client), filter=expression)
    except Exception as exc:
        raise VectorStoreError(f"Memory Milvus delete failed: {exc}") from exc


def search_memory_vectors(
    query_vector: list[float], user_id: str, limit: int
) -> list[dict[str, Any]]:
    if not memory_vectors_enabled():
        return []
    safe_user_id = user_id.replace("\\", "\\\\").replace('"', r'\"')
    client = _client()
    try:
        results = client.search(
            collection_name=_collection(client),
            data=[query_vector],
            filter=f'user_id == "{safe_user_id}"',
            limit=limit,
            output_fields=["user_id", "memory_type", "content", "updated_at"],
            search_params={
                "metric_type": "COSINE",
                "params": {"ef": max(32, limit * 2)},
            },
            consistency_level="Strong",
        )
        return results[0] if results else []
    except Exception as exc:
        raise VectorStoreError(f"Memory Milvus search failed: {exc}") from exc

