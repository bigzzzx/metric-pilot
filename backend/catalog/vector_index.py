"""Milvus projection for governed catalog metadata, isolated from user knowledge."""
from __future__ import annotations

import hashlib
import json
import os
from functools import wraps
from typing import Any

from backend.catalog.rag_documents import authorized_catalog_documents, published_catalog_documents
from backend.core import ROOT, db
from backend.observability import log_event
from backend.retrieval_models import EMBEDDING_DIMENSIONS, RetrievalModelError, embed_texts
from backend.vector_store import VectorStoreError, knowledge_collection_name, milvus_client, using_milvus

CATALOG_COLLECTION = "catalog_metadata_bge_small_zh_v1"
_indexed_version = ""


def _setting(name: str, default: str) -> str:
    if value := os.getenv(name):
        return value.strip()
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip()
    return default


def collection_name() -> str:
    return _setting("CATALOG_MILVUS_COLLECTION", CATALOG_COLLECTION)


def _embedding_batch_size() -> int:
    try:
        return min(64, max(1, int(_setting("CATALOG_EMBEDDING_BATCH_SIZE", "16"))))
    except ValueError:
        return 16


def _embed_resilient(texts: list[str]) -> list[list[float]]:
    """Keep a large metadata reindex from overloading a small TEI instance."""
    if not texts:
        return []
    try:
        return embed_texts(texts)
    except RetrievalModelError:
        if len(texts) == 1:
            raise
        midpoint = len(texts) // 2
        return [*_embed_resilient(texts[:midpoint]), *_embed_resilient(texts[midpoint:])]


def _embed_batched(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    batch_size = _embedding_batch_size()
    for start in range(0, len(texts), batch_size):
        vectors.extend(_embed_resilient(texts[start:start + batch_size]))
    return vectors


def _collection(client) -> str:
    collection = collection_name()
    try:
        if client.has_collection(collection_name=collection):
            return collection
        from pymilvus import DataType
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=160)
        schema.add_field("entity_type", DataType.VARCHAR, max_length=32)
        schema.add_field("asset_scope_json", DataType.VARCHAR, max_length=1024)
        schema.add_field("domain_id", DataType.VARCHAR, max_length=128)
        schema.add_field("version", DataType.VARCHAR, max_length=64)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("content", DataType.VARCHAR, max_length=65535)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSIONS)
        indexes = client.prepare_index_params()
        indexes.add_index(field_name="vector", index_name="vector_hnsw", index_type="HNSW", metric_type="COSINE", params={"M": 16, "efConstruction": 200})
        client.create_collection(collection_name=collection, schema=schema, index_params=indexes)
        return collection
    except Exception as exc:
        raise VectorStoreError(f"Catalog metadata collection could not be created: {exc}") from exc


def _version(documents: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(documents, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _marker_version(client, collection: str) -> str:
    try:
        rows = client.query(collection_name=collection, filter='id == "catalog-index-state"', output_fields=["version"], limit=1, consistency_level="Strong")
        return str(rows[0].get("version") or "") if rows else ""
    except Exception as exc:
        raise VectorStoreError(f"Catalog index state lookup failed: {exc}") from exc


def _delete_stale(client, collection: str, current_ids: set[str]) -> None:
    try:
        rows = client.query(collection_name=collection, filter='entity_type != "index_state"', output_fields=["id"], limit=16384, consistency_level="Strong")
        stale = [str(row["id"]) for row in rows if str(row["id"]) not in current_ids]
        if stale:
            client.delete(collection_name=collection, filter=f"id in [{','.join(json.dumps(item) for item in stale)}]")
    except Exception as exc:
        raise VectorStoreError(f"Catalog stale-vector cleanup failed: {exc}") from exc


def _remove_legacy_shared_metadata(client) -> int:
    """Remove only the superseded static-catalog vectors from the KB collection."""
    try:
        legacy_collection = knowledge_collection_name()
        rows = client.query(
            collection_name=legacy_collection,
            filter='knowledge_base_id == "__metadata__"',
            output_fields=["id"],
            limit=16384,
            consistency_level="Strong",
        )
        if rows:
            client.delete(collection_name=legacy_collection, filter='knowledge_base_id == "__metadata__"')
        return len(rows)
    except Exception as exc:
        raise VectorStoreError(f"Legacy catalog-vector cleanup failed: {exc}") from exc


def _serialized_writer(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        if not using_milvus():
            return False
        from backend.index_worker import writer_lock
        try:
            with writer_lock(db):
                return function(*args, **kwargs)
        except Exception as exc:
            log_event('catalog.rag_writer_unavailable',error_type=type(exc).__name__)
            return False
    return guarded


@_serialized_writer
def ensure_catalog_index() -> bool:
    """Idempotently mirror published metadata into the isolated collection."""
    global _indexed_version
    if not using_milvus():
        return False
    documents = published_catalog_documents()
    version = _version(documents)
    # Read the durable marker under the writer lock. An in-process cache can be
    # stale after another process wrote an index or the configured target changed.
    try:
        client = milvus_client()
        collection = _collection(client)
        if _marker_version(client, collection) == version:
            removed = _remove_legacy_shared_metadata(client)
            _indexed_version = version
            if removed:
                log_event("catalog.rag_legacy_cleanup", collection=collection, removed_legacy_chunks=removed)
            return True
        vectors = _embed_batched([f"{item['title']}\n{item['content']}" for item in documents])
        rows = [{
            "id": str(item["id"]), "entity_type": str(item["kind"]),
            "asset_scope_json": json.dumps(item.get("asset_ids", []), ensure_ascii=False),
            "domain_id": str(item.get("domain_id") or ""), "version": version,
            "title": str(item["title"])[:512], "content": str(item["content"])[:65535], "vector": vector,
        } for item, vector in zip(documents, vectors)]
        if rows:
            client.upsert(collection_name=collection, data=rows)
        _delete_stale(client, collection, {str(item["id"]) for item in documents})
        client.upsert(collection_name=collection, data=[{
            "id": "catalog-index-state", "entity_type": "index_state", "asset_scope_json": "[]", "domain_id": "",
            "version": version, "title": "Catalog metadata index state", "content": version,
            "vector": [0.0] * EMBEDDING_DIMENSIONS,
        }])
        removed = _remove_legacy_shared_metadata(client)
        _indexed_version = version
        log_event("catalog.rag_index_ready", chunks=len(rows), collection=collection, removed_legacy_chunks=removed)
        return True
    except Exception as exc:
        log_event("catalog.rag_index_degraded", error=str(exc), collection=collection_name())
        return False


@_serialized_writer
def refresh_catalog_assets(asset_ids: set[str]) -> bool:
    """Incrementally re-embed documents scoped to changed catalog assets."""

    global _indexed_version
    scopes = {str(value) for value in asset_ids if value}
    if not scopes or not using_milvus():
        return False
    documents = published_catalog_documents()
    version = _version(documents)
    affected = [
        item for item in documents
        if scopes.intersection(str(value) for value in item.get("asset_ids", []))
    ]
    current_ids = {str(item["id"]) for item in affected}
    try:
        client = milvus_client()
        collection = _collection(client)
        vectors = _embed_batched([f"{item['title']}\n{item['content']}" for item in affected])
        rows = [{
            "id": str(item["id"]), "entity_type": str(item["kind"]),
            "asset_scope_json": json.dumps(item.get("asset_ids", []), ensure_ascii=False),
            "domain_id": str(item.get("domain_id") or ""), "version": version,
            "title": str(item["title"])[:512], "content": str(item["content"])[:65535], "vector": vector,
        } for item, vector in zip(affected, vectors)]
        if rows:
            client.upsert(collection_name=collection, data=rows)
        indexed = client.query(
            collection_name=collection, filter='entity_type != "index_state"',
            output_fields=["id", "asset_scope_json"], limit=16384, consistency_level="Strong",
        )
        stale = []
        for row in indexed:
            try:
                row_scopes = set(json.loads(row.get("asset_scope_json") or "[]"))
            except (json.JSONDecodeError, TypeError):
                row_scopes = set()
            if scopes.intersection(row_scopes) and str(row["id"]) not in current_ids:
                stale.append(str(row["id"]))
        if stale:
            client.delete(
                collection_name=collection,
                filter=f"id in [{','.join(json.dumps(item) for item in stale)}]",
            )
        # A scoped refresh cannot prove that all other scopes already match the
        # current snapshot. Do not publish a full-index marker until the durable
        # outbox worker has compared/refreshed the complete current projection.
        partial_version = 'partial:' + version[:56]
        client.upsert(collection_name=collection, data=[{
            "id": "catalog-index-state", "entity_type": "index_state", "asset_scope_json": "[]",
            "domain_id": "", "version": partial_version, "title": "Catalog metadata index state",
            "content": partial_version, "vector": [0.0] * EMBEDDING_DIMENSIONS,
        }])
        _indexed_version = ''
        log_event(
            "catalog.rag_incremental_refresh", collection=collection, assets=len(scopes),
            upserted=len(rows), deleted=len(stale),
        )
        return True
    except Exception as exc:
        log_event(
            "catalog.rag_incremental_degraded", error=str(exc), collection=collection_name(),
            assets=len(scopes),
        )
        return False


def retrieve_catalog_vectors(query: str, user_id: str, limit: int) -> list[dict[str, Any]]:
    """Dense retrieval with mandatory permission filtering before RRF/reranking."""
    if not user_id or not ensure_catalog_index():
        return []
    documents = {str(item["id"]): item for item in authorized_catalog_documents(user_id)}
    if not documents:
        return []
    try:
        client = milvus_client()
        collection = _collection(client)
        candidate_limit = min(max(limit * 10, 40), 200)
        hits = client.search(
            collection_name=collection,
            data=[embed_texts([query], query=True)[0]],
            filter='entity_type != "index_state"', limit=candidate_limit,
            output_fields=["id", "version"],
            search_params={"metric_type": "COSINE", "params": {"ef": max(64, candidate_limit)}},
            consistency_level="Strong",
        )
        result: list[dict[str, Any]] = []
        for hit in (hits[0] if hits else []):
            document = documents.get(str(hit["id"]))
            if document:
                result.append({**document, "dense_score": round(float(hit.get("distance", 0)), 6)})
            if len(result) >= limit:
                break
        return result
    except Exception as exc:
        log_event("catalog.rag_dense_fallback", error=str(exc), collection=collection_name())
        return []
