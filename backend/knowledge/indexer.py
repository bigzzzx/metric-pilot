"""Orchestrate durable document ingestion across SQLite and Milvus."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any

from backend.core import ROOT, db
from backend.knowledge.models import DocumentIngestionError
from backend.knowledge.repository import (
    ConnectionFactory,
    assert_writable_knowledge_base,
    claim_processing_document,
    claim_stale_processing_documents,
    complete_stale_processing_cleanup,
    init_knowledge_schema,
    mark_document_status,
    persist_ready_document,
    set_document_storage_path,
)
from backend.knowledge.service import DocumentIngestionService
from backend.knowledge.source_storage import (
    DEFAULT_KNOWLEDGE_BASE_QUOTA_BYTES,
    DEFAULT_OWNER_QUOTA_BYTES,
    SourceStorage,
    source_fingerprint,
)
from backend.retrieval_models import RetrievalModelError, embed_texts
from backend.vector_store import (
    SHARED_OWNER,
    VectorStoreError,
    delete_document_chunks,
    upsert_chunks,
    using_milvus,
)


_MEDIA_TYPES = {
    "txt": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "json": "application/json",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "csv": "text/csv",
    "sql": "application/sql",
    "html": "text/html",
    "htm": "text/html",
}

MAX_EMBEDDING_BATCH_SIZE = 64
DEFAULT_EMBEDDING_BATCH_SIZE = 32
DEFAULT_STALE_PROCESSING_SECONDS = 15 * 60
DEFAULT_INDEX_GENERATION = "knowledge-bge-small-zh-v2"


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


def _integer_setting(name: str, default: int) -> int:
    try:
        return int(_setting(name, str(default)))
    except ValueError:
        return default


def _normalized_source_type(filename: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".")
    return extension or "binary"


def _chunk_identity(document_id: str, index: int, embedding_content: str) -> tuple[str, str]:
    content_hash = hashlib.sha256(embedding_content.encode("utf-8")).hexdigest()
    chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"knowledge:{document_id}:{index}:{content_hash}"))
    return chunk_id, content_hash


def _dedup_key(sha256: str, source_type: str, parser_name: str, index_generation: str) -> str:
    material = "\0".join((sha256, source_type.lower(), parser_name.lower(), index_generation))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _failure_code(error: Exception) -> str:
    if isinstance(error, VectorStoreError):
        return "index_unavailable"
    if isinstance(error, RetrievalModelError):
        return "embedding_failed"
    if isinstance(error, OSError):
        return "storage_failed"
    return "ingestion_failed"


class KnowledgeDocumentIndexer:
    """High-level application service used by HTTP routes and background jobs."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory = db,
        ingestion_service: DocumentIngestionService | None = None,
        source_storage: SourceStorage | None = None,
        owner_quota_bytes: int | None = None,
        knowledge_base_quota_bytes: int | None = None,
        embedding_batch_size: int | None = None,
        index_generation: str | None = None,
        stale_processing_seconds: int | None = None,
        reconcile_on_startup: bool = True,
    ) -> None:
        self.connection_factory = connection_factory
        self.ingestion_service = ingestion_service or DocumentIngestionService()
        self.source_storage = source_storage or SourceStorage()
        self.owner_quota_bytes = (
            _integer_setting("KNOWLEDGE_OWNER_QUOTA_BYTES", DEFAULT_OWNER_QUOTA_BYTES)
            if owner_quota_bytes is None
            else int(owner_quota_bytes)
        )
        self.knowledge_base_quota_bytes = (
            _integer_setting(
                "KNOWLEDGE_BASE_QUOTA_BYTES", DEFAULT_KNOWLEDGE_BASE_QUOTA_BYTES
            )
            if knowledge_base_quota_bytes is None
            else int(knowledge_base_quota_bytes)
        )
        configured_batch = (
            _integer_setting("KNOWLEDGE_EMBEDDING_BATCH_SIZE", DEFAULT_EMBEDDING_BATCH_SIZE)
            if embedding_batch_size is None
            else int(embedding_batch_size)
        )
        self.embedding_batch_size = min(MAX_EMBEDDING_BATCH_SIZE, max(1, configured_batch))
        self.index_generation = index_generation or _setting(
            "KNOWLEDGE_INDEX_GENERATION", DEFAULT_INDEX_GENERATION
        )
        self.stale_processing_seconds = (
            _integer_setting(
                "KNOWLEDGE_STALE_PROCESSING_SECONDS", DEFAULT_STALE_PROCESSING_SECONDS
            )
            if stale_processing_seconds is None
            else int(stale_processing_seconds)
        )
        init_knowledge_schema(self.connection_factory)
        if reconcile_on_startup:
            try:
                self.reconcile_stale_processing()
            except (OSError, VectorStoreError):
                # Startup remains available; the explicit reconciler can retry.
                pass

    def _embed_chunks(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.embedding_batch_size):
            batch = texts[start : start + self.embedding_batch_size]
            if len(batch) > MAX_EMBEDDING_BATCH_SIZE:  # defensive invariant
                raise RuntimeError("Embedding batch exceeded its hard limit")
            vectors.extend(embed_texts(batch))
        return vectors

    def _claim(
        self,
        *,
        document_id: str,
        knowledge_base_id: str,
        owner_id: str | None,
        filename: str,
        media_type: str,
        byte_size: int,
        digest: str,
        source_type: str,
        parser_name: str,
    ) -> tuple[dict[str, Any], bool]:
        return claim_processing_document(
            document_id=document_id,
            knowledge_base_id=knowledge_base_id,
            owner_id=owner_id,
            name=Path(filename).name.strip() or "未命名文档",
            source_type=source_type,
            media_type=media_type,
            byte_size=byte_size,
            sha256=digest,
            dedup_key=_dedup_key(digest, source_type, parser_name, self.index_generation),
            parser_name=parser_name,
            index_generation=self.index_generation,
            owner_quota_bytes=self.owner_quota_bytes,
            knowledge_base_quota_bytes=self.knowledge_base_quota_bytes,
            connection_factory=self.connection_factory,
        )

    def _store_claimed_source(
        self,
        *,
        document_id: str,
        knowledge_base_id: str,
        owner_id: str | None,
        filename: str,
        data: bytes,
    ) -> str:
        storage_path = self._planned_source_path(
            document_id=document_id,
            knowledge_base_id=knowledge_base_id,
            owner_id=owner_id,
            filename=filename,
        )
        set_document_storage_path(
            document_id,
            owner_id,
            storage_path,
            connection_factory=self.connection_factory,
        )
        self.source_storage.store(
            owner_id=owner_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            filename=filename,
            data=data,
        )
        return storage_path

    def _planned_source_path(
        self,
        *,
        document_id: str,
        knowledge_base_id: str,
        owner_id: str | None,
        filename: str,
    ) -> str:
        destination = self.source_storage.destination(
            owner_id=owner_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            filename=filename,
        )
        return str(destination.relative_to(self.source_storage.root.parent))

    def ingest_bytes(
        self,
        *,
        knowledge_base_id: str,
        owner_id: str | None,
        filename: str,
        media_type: str | None,
        data: bytes,
    ) -> dict[str, Any]:
        base = assert_writable_knowledge_base(
            knowledge_base_id,
            owner_id,
            connection_factory=self.connection_factory,
        )
        byte_size, digest = source_fingerprint(data)
        display_name = Path(filename).name.strip() or "未命名文档"
        normalized_media_type = (media_type or mimetypes.guess_type(filename)[0] or "").strip()
        fallback_source_type = _normalized_source_type(filename)
        try:
            ingestion = self.ingestion_service.parse_and_chunk(filename, media_type, data)
        except DocumentIngestionError as exc:
            parser_name = str(exc.details.get("parser") or fallback_source_type)
            document, claimed = self._claim(
                document_id=str(uuid.uuid4()),
                knowledge_base_id=knowledge_base_id,
                owner_id=owner_id,
                filename=filename,
                media_type=normalized_media_type,
                byte_size=byte_size,
                digest=digest,
                source_type=fallback_source_type,
                parser_name=parser_name,
            )
            claimed_id = str(document["id"])
            exc.details["document_id"] = claimed_id
            if not claimed:
                raise
            status = "ocr_required" if exc.code == "ocr_required" else "failed"
            if status == "ocr_required":
                ocr_storage_path = self._planned_source_path(
                    document_id=claimed_id,
                    knowledge_base_id=knowledge_base_id,
                    owner_id=owner_id,
                    filename=filename,
                )
                try:
                    self._store_claimed_source(
                        document_id=claimed_id,
                        knowledge_base_id=knowledge_base_id,
                        owner_id=owner_id,
                        filename=filename,
                        data=data,
                    )
                except Exception as storage_error:
                    try:
                        self.source_storage.remove(ocr_storage_path)
                    except (OSError, ValueError):
                        pass
                    mark_document_status(
                        claimed_id,
                        owner_id,
                        "failed",
                        error_code="storage_failed",
                        error_message=str(storage_error)[:1000],
                        clear_storage_path=True,
                        connection_factory=self.connection_factory,
                    )
                    raise storage_error from exc
            mark_document_status(
                claimed_id,
                owner_id,
                status,
                error_code=exc.code,
                error_message=exc.message,
                parser_name=parser_name,
                clear_storage_path=status != "ocr_required",
                connection_factory=self.connection_factory,
            )
            raise

        source_type = ingestion.document.source_type or fallback_source_type
        document, claimed = self._claim(
            document_id=str(uuid.uuid4()),
            knowledge_base_id=knowledge_base_id,
            owner_id=owner_id,
            filename=filename,
            media_type=normalized_media_type,
            byte_size=byte_size,
            digest=digest,
            source_type=source_type,
            parser_name=ingestion.parser_name,
        )
        if not claimed:
            return document
        document_id = str(document["id"])

        external_index_attempted = False
        storage_path = self._planned_source_path(
            document_id=document_id,
            knowledge_base_id=knowledge_base_id,
            owner_id=owner_id,
            filename=filename,
        )
        try:
            self._store_claimed_source(
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                owner_id=owner_id,
                filename=filename,
                data=data,
            )
            embeddings = self._embed_chunks(
                [chunk.embedding_content for chunk in ingestion.chunks]
            )
            identities = [
                _chunk_identity(document_id, index, chunk.embedding_content)
                for index, chunk in enumerate(ingestion.chunks)
            ]
            chunk_ids = [identity[0] for identity in identities]
            content_hashes = [identity[1] for identity in identities]
            if using_milvus():
                external_index_attempted = True
                vector_rows = [
                    {
                        "id": chunk_ids[index],
                        "owner_id": base.get("owner_id") or SHARED_OWNER,
                        "knowledge_base_id": knowledge_base_id,
                        "document_id": document_id,
                        "chunk_index": index,
                        "kind": "knowledge",
                        "title": display_name,
                        "content": chunk.content,
                        "vector": embeddings[index],
                    }
                    for index, chunk in enumerate(ingestion.chunks)
                ]
                upsert_chunks(vector_rows)
            return persist_ready_document(
                document_id=document_id,
                owner_id=owner_id,
                ingestion=ingestion,
                chunk_ids=chunk_ids,
                embeddings=embeddings,
                content_hashes=content_hashes,
                connection_factory=self.connection_factory,
            )
        except Exception as exc:
            if external_index_attempted:
                try:
                    delete_document_chunks(document_id)
                except VectorStoreError:
                    # Preserve the actionable primary failure. Reconciliation can
                    # retry cleanup because SQLite never marks this document ready.
                    pass
            if storage_path:
                try:
                    self.source_storage.remove(storage_path)
                except (OSError, ValueError):
                    pass
            mark_document_status(
                document_id,
                owner_id,
                "failed",
                error_code=_failure_code(exc),
                error_message=str(exc)[:1000],
                clear_storage_path=True,
                connection_factory=self.connection_factory,
            )
            raise

    def reconcile_stale_processing(self) -> list[str]:
        """Idempotently fail and clean abandoned processing documents."""
        records = claim_stale_processing_documents(
            stale_after_seconds=self.stale_processing_seconds,
            connection_factory=self.connection_factory,
        )
        reconciled: list[str] = []
        for record in records:
            document_id = str(record["id"])
            try:
                if using_milvus():
                    delete_document_chunks(document_id)
                self.source_storage.remove(str(record.get("storage_path") or ""))
                if complete_stale_processing_cleanup(
                    document_id, connection_factory=self.connection_factory
                ):
                    reconciled.append(document_id)
            except ValueError:
                # A corrupt/legacy path is never followed outside the root; the
                # unsafe reference itself can still be removed from SQLite.
                if complete_stale_processing_cleanup(
                    document_id, connection_factory=self.connection_factory
                ):
                    reconciled.append(document_id)
            except (OSError, VectorStoreError):
                # cleanup_pending remains queryable and will be retried.
                continue
        return reconciled

    def ingest_text(
        self,
        *,
        knowledge_base_id: str,
        owner_id: str | None,
        name: str,
        content: str,
        source_type: str = "txt",
    ) -> dict[str, Any]:
        extension = source_type.lower().removeprefix("file:").lstrip(".") or "txt"
        filename = name if Path(name).suffix else f"{name}.{extension}"
        return self.ingest_bytes(
            knowledge_base_id=knowledge_base_id,
            owner_id=owner_id,
            filename=filename,
            media_type=_MEDIA_TYPES.get(extension),
            data=content.encode("utf-8"),
        )
