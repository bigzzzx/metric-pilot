"""Milvus adapter for user knowledge-base vectors.

SQLite remains the source of truth for knowledge-base, document and chunk
metadata. This module owns only vector upsert/search operations.
"""
from __future__ import annotations

import os
from typing import Any

from backend.core import ROOT
from backend.retrieval_models import EMBEDDING_DIMENSIONS

MILVUS_DATABASE = "data_agent"
MILVUS_COLLECTION = "knowledge_chunks_bge_small_zh_v1"
SHARED_OWNER = "__shared__"


class VectorStoreError(RuntimeError):
    """The configured vector store could not complete an operation."""


def _setting(name: str, default: str = "") -> str:
    if value := os.getenv(name):
        return value.strip()
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip()
    return default


def backend_name() -> str:
    return _setting("VECTOR_STORE_BACKEND", "sqlite").lower()


def using_milvus() -> bool:
    return backend_name() == "milvus"


def knowledge_collection_name() -> str:
    """Configured collection reserved for uploaded knowledge-base chunks."""
    return _setting("MILVUS_COLLECTION", MILVUS_COLLECTION)


def _client():
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise VectorStoreError("PyMilvus is not installed. Run: pip install pymilvus") from exc
    uri = _setting("MILVUS_URI")
    if not uri:
        raise VectorStoreError("MILVUS_URI is not configured")
    try:
        return MilvusClient(uri=uri, db_name=_setting("MILVUS_DATABASE", MILVUS_DATABASE))
    except Exception as exc:
        raise VectorStoreError(f"Milvus connection failed: {exc}") from exc


def milvus_client():
    """Return a Milvus client for another isolated collection adapter."""
    return _client()


def _collection(client) -> str:
    collection = knowledge_collection_name()
    try:
        if not client.has_collection(collection_name=collection):
            _create_collection(client, collection)
    except VectorStoreError:
        raise
    except Exception as exc:
        raise VectorStoreError(f"Milvus collection check failed: {exc}") from exc
    return collection


def _create_collection(client, collection: str) -> None:
    """Create the isolated 512-dimensional Data Agent collection."""
    try:
        from pymilvus import DataType

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("owner_id", DataType.VARCHAR, max_length=128)
        schema.add_field("knowledge_base_id", DataType.VARCHAR, max_length=64)
        schema.add_field("document_id", DataType.VARCHAR, max_length=64)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("kind", DataType.VARCHAR, max_length=32)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("content", DataType.VARCHAR, max_length=65535)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSIONS)
        indexes = client.prepare_index_params()
        indexes.add_index(
            field_name="vector",
            index_name="vector_hnsw",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        client.create_collection(collection_name=collection, schema=schema, index_params=indexes)
    except Exception as exc:
        raise VectorStoreError(f"Milvus collection {collection} could not be created: {exc}") from exc


def upsert_chunks(chunks: list[dict[str, Any]]) -> None:
    if not chunks:
        return
    client = _client()
    try:
        rows = [
            {
                "id": str(chunk["id"]),
                "owner_id": str(chunk.get("owner_id", SHARED_OWNER)),
                "knowledge_base_id": str(chunk.get("knowledge_base_id", "")),
                "document_id": str(chunk.get("document_id", "")),
                "chunk_index": int(chunk.get("chunk_index", 0)),
                "kind": str(chunk.get("kind", "knowledge")),
                "title": str(chunk.get("title", ""))[:512],
                "content": str(chunk.get("content", ""))[:65535],
                "vector": chunk["vector"],
            }
            for chunk in chunks
        ]
        client.upsert(collection_name=_collection(client), data=rows)
    except VectorStoreError:
        raise
    except Exception as exc:
        raise VectorStoreError(f"Milvus upsert failed: {exc}") from exc


def delete_document_chunks(document_id: str) -> None:
    """Delete all external vectors for one document (ingestion compensation)."""
    if not document_id:
        return
    client = _client()
    safe_document_id = document_id.replace("\\", "\\\\").replace('"', r'\"')
    try:
        client.delete(
            collection_name=_collection(client),
            filter=f'document_id == "{safe_document_id}"',
        )
    except VectorStoreError:
        raise
    except Exception as exc:
        raise VectorStoreError(f"Milvus document cleanup failed: {exc}") from exc


def search_chunks(query_vector: list[float], user_id: str | None, limit: int) -> list[dict[str, Any]]:
    """Search shared chunks and chunks owned by the requesting user only."""
    client = _client()
    owner = (user_id or "").replace('"', r'\"')
    allowed = f'owner_id == "{SHARED_OWNER}"' if not owner else f'owner_id in ["{SHARED_OWNER}", "{owner}"]'
    try:
        results = client.search(
            collection_name=_collection(client), data=[query_vector], filter=allowed,
            limit=limit,
            output_fields=["owner_id", "knowledge_base_id", "document_id", "chunk_index", "kind", "title", "content"],
            search_params={"metric_type": "COSINE", "params": {"ef": max(64, limit * 2)}},
            consistency_level="Strong",
        )
    except VectorStoreError:
        raise
    except Exception as exc:
        raise VectorStoreError(f"Milvus search failed: {exc}") from exc
    return results[0] if results else []


def has_metadata_version(version: str) -> bool:
    """Return whether the current catalog version is already indexed."""
    client = _client()
    safe_version = version.replace('"', r'\"')
    try:
        rows = client.query(
            collection_name=_collection(client),
            filter=f'knowledge_base_id == "__metadata__" and document_id == "{safe_version}"',
            output_fields=["id"],
            limit=1,
            consistency_level="Strong",
        )
    except Exception as exc:
        raise VectorStoreError(f"Milvus metadata version check failed: {exc}") from exc
    return bool(rows)
