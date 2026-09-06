"""SQLite source-of-truth for knowledge documents and parsed chunks.

This module deliberately contains no HTTP, parser, embedding, or Milvus code.
It owns backward-compatible schema migration, authorization-aware reads, and
the atomic transition from a processing document to a ready document.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from backend.core import db
from backend.knowledge.models import DocumentIngestionError, IngestionResult


ConnectionFactory = Callable[[], Any]


_DOCUMENT_COLUMNS: dict[str, str] = {
    "media_type": "TEXT NOT NULL DEFAULT ''",
    "byte_size": "INTEGER NOT NULL DEFAULT 0",
    "sha256": "TEXT NOT NULL DEFAULT ''",
    "storage_path": "TEXT NOT NULL DEFAULT ''",
    # Rows created before the ingestion pipeline were already fully indexed.
    "status": "TEXT NOT NULL DEFAULT 'ready'",
    "parser_name": "TEXT NOT NULL DEFAULT ''",
    "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
    "error_code": "TEXT NOT NULL DEFAULT ''",
    "error_message": "TEXT NOT NULL DEFAULT ''",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
    "dedup_key": "TEXT NOT NULL DEFAULT ''",
    "index_generation": "TEXT NOT NULL DEFAULT ''",
}

_CHUNK_COLUMNS: dict[str, str] = {
    "token_count": "INTEGER NOT NULL DEFAULT 0",
    "section_path_json": "TEXT NOT NULL DEFAULT '[]'",
    "locator_json": "TEXT NOT NULL DEFAULT '{}'",
    "block_types_json": "TEXT NOT NULL DEFAULT '[]'",
    "embedding_content": "TEXT NOT NULL DEFAULT ''",
    "content_hash": "TEXT NOT NULL DEFAULT ''",
}

VALID_DOCUMENT_STATUSES = {"processing", "ready", "failed", "ocr_required"}


def _columns(connection: Any, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def init_knowledge_schema(connection_factory: ConnectionFactory = db) -> None:
    """Create/migrate knowledge tables without invalidating legacy rows."""
    connection = connection_factory()
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_bases(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                owner_id TEXT DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_documents(
                id TEXT PRIMARY KEY,
                knowledge_base_id TEXT NOT NULL,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS knowledge_chunks(
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding TEXT NOT NULL
            );
            """
        )
        base_columns = _columns(connection, "knowledge_bases")
        if "owner_id" not in base_columns:
            connection.execute("ALTER TABLE knowledge_bases ADD COLUMN owner_id TEXT DEFAULT NULL")
        document_columns = _columns(connection, "knowledge_documents")
        for name, definition in _DOCUMENT_COLUMNS.items():
            if name not in document_columns:
                connection.execute(f"ALTER TABLE knowledge_documents ADD COLUMN {name} {definition}")
        chunk_columns = _columns(connection, "knowledge_chunks")
        for name, definition in _CHUNK_COLUMNS.items():
            if name not in chunk_columns:
                connection.execute(f"ALTER TABLE knowledge_chunks ADD COLUMN {name} {definition}")
        connection.execute(
            "UPDATE knowledge_documents SET updated_at=created_at WHERE updated_at='' OR updated_at IS NULL"
        )
        connection.execute(
            "UPDATE knowledge_chunks SET embedding_content=content "
            "WHERE embedding_content='' OR embedding_content IS NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_base_status "
            "ON knowledge_documents(knowledge_base_id, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_sha256 "
            "ON knowledge_documents(knowledge_base_id, sha256)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_documents_dedup "
            "ON knowledge_documents(knowledge_base_id, dedup_key) "
            "WHERE dedup_key<>''"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document_index "
            "ON knowledge_chunks(document_id, chunk_index)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _decode_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _document_dict(row: Any) -> dict[str, Any]:
    payload = dict(row)
    payload["warnings"] = _decode_json(payload.pop("warnings_json", "[]"), [])
    payload["byte_size"] = int(payload.get("byte_size") or 0)
    payload["chunk_count"] = int(payload.get("chunk_count") or 0)
    return payload


def _readable_base(connection: Any, knowledge_base_id: str, owner_id: str | None) -> Any:
    return connection.execute(
        "SELECT id, owner_id FROM knowledge_bases "
        "WHERE id=? AND (owner_id IS NULL OR owner_id=?)",
        (knowledge_base_id, owner_id),
    ).fetchone()


def _writable_base(connection: Any, knowledge_base_id: str, owner_id: str | None) -> Any:
    # Shared bases are writable only by trusted, owner-less internal callers.
    return connection.execute(
        "SELECT id, owner_id FROM knowledge_bases "
        "WHERE id=? AND (owner_id=? OR (owner_id IS NULL AND ? IS NULL))",
        (knowledge_base_id, owner_id, owner_id),
    ).fetchone()


def assert_writable_knowledge_base(
    knowledge_base_id: str,
    owner_id: str | None,
    *,
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any]:
    connection = connection_factory()
    try:
        row = _writable_base(connection, knowledge_base_id, owner_id)
        if row is None:
            raise LookupError("Knowledge base not found")
        return dict(row)
    finally:
        connection.close()


def list_knowledge_documents(
    knowledge_base_id: str,
    owner_id: str | None,
    *,
    connection_factory: ConnectionFactory = db,
) -> list[dict[str, Any]]:
    init_knowledge_schema(connection_factory)
    connection = connection_factory()
    try:
        if _readable_base(connection, knowledge_base_id, owner_id) is None:
            raise LookupError("Knowledge base not found")
        rows = connection.execute(
            """
            SELECT id, knowledge_base_id, name, source_type, media_type,
                   byte_size, sha256, storage_path, status, parser_name,
                   warnings_json, error_code, error_message, chunk_count,
                   created_at, updated_at
            FROM knowledge_documents
            WHERE knowledge_base_id=?
            ORDER BY created_at DESC, id DESC
            """,
            (knowledge_base_id,),
        ).fetchall()
        return [_document_dict(row) for row in rows]
    finally:
        connection.close()


def get_knowledge_document(
    document_id: str,
    owner_id: str | None,
    *,
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any]:
    init_knowledge_schema(connection_factory)
    connection = connection_factory()
    try:
        row = connection.execute(
            """
            SELECT doc.id, doc.knowledge_base_id, doc.name, doc.source_type,
                   doc.media_type, doc.byte_size, doc.sha256, doc.storage_path,
                   doc.status, doc.parser_name, doc.warnings_json,
                   doc.error_code, doc.error_message, doc.chunk_count,
                   doc.created_at, doc.updated_at
            FROM knowledge_documents doc
            JOIN knowledge_bases kb ON kb.id=doc.knowledge_base_id
            WHERE doc.id=? AND (kb.owner_id IS NULL OR kb.owner_id=?)
            """,
            (document_id, owner_id),
        ).fetchone()
        if row is None:
            raise LookupError("Knowledge document not found")
        return _document_dict(row)
    finally:
        connection.close()


def find_ready_document_by_hash(
    knowledge_base_id: str,
    owner_id: str | None,
    sha256: str,
    *,
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any] | None:
    connection = connection_factory()
    try:
        if _writable_base(connection, knowledge_base_id, owner_id) is None:
            raise LookupError("Knowledge base not found")
        row = connection.execute(
            """
            SELECT id, knowledge_base_id, name, source_type, media_type,
                   byte_size, sha256, storage_path, status, parser_name,
                   warnings_json, error_code, error_message, chunk_count,
                   created_at, updated_at
            FROM knowledge_documents
            WHERE knowledge_base_id=? AND sha256=? AND status='ready'
            ORDER BY created_at DESC LIMIT 1
            """,
            (knowledge_base_id, sha256),
        ).fetchone()
        return _document_dict(row) if row else None
    finally:
        connection.close()


def create_processing_document(
    *,
    document_id: str,
    knowledge_base_id: str,
    owner_id: str | None,
    name: str,
    source_type: str,
    media_type: str,
    byte_size: int,
    sha256: str,
    storage_path: str,
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any]:
    init_knowledge_schema(connection_factory)
    connection = connection_factory()
    try:
        if _writable_base(connection, knowledge_base_id, owner_id) is None:
            raise LookupError("Knowledge base not found")
        connection.execute(
            """
            INSERT INTO knowledge_documents(
                id, knowledge_base_id, name, source_type, media_type,
                byte_size, sha256, storage_path, status, chunk_count,
                updated_at
            ) VALUES (?,?,?,?,?,?,?,?, 'processing', 0, CURRENT_TIMESTAMP)
            """,
            (
                document_id,
                knowledge_base_id,
                name,
                source_type,
                media_type,
                byte_size,
                sha256,
                storage_path,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_knowledge_document(document_id, owner_id, connection_factory=connection_factory)


def _quota_usage(connection: Any, knowledge_base_id: str, owner_id: str | None) -> tuple[int, int]:
    counted = (
        "(doc.status IN ('processing','ready','ocr_required') "
        "OR (doc.status='failed' AND doc.error_code='stale_processing_cleanup_pending'))"
    )
    base_usage = connection.execute(
        "SELECT COALESCE(SUM(doc.byte_size),0) AS used FROM knowledge_documents doc "
        f"WHERE doc.knowledge_base_id=? AND {counted}",
        (knowledge_base_id,),
    ).fetchone()["used"]
    if owner_id is None:
        owner_usage = connection.execute(
            f"""
            SELECT COALESCE(SUM(doc.byte_size),0) AS used
            FROM knowledge_documents doc
            JOIN knowledge_bases kb ON kb.id=doc.knowledge_base_id
            WHERE kb.owner_id IS NULL AND {counted}
            """
        ).fetchone()["used"]
    else:
        owner_usage = connection.execute(
            f"""
            SELECT COALESCE(SUM(doc.byte_size),0) AS used
            FROM knowledge_documents doc
            JOIN knowledge_bases kb ON kb.id=doc.knowledge_base_id
            WHERE kb.owner_id=? AND {counted}
            """,
            (owner_id,),
        ).fetchone()["used"]
    return int(owner_usage or 0), int(base_usage or 0)


def claim_processing_document(
    *,
    document_id: str,
    knowledge_base_id: str,
    owner_id: str | None,
    name: str,
    source_type: str,
    media_type: str,
    byte_size: int,
    sha256: str,
    dedup_key: str,
    parser_name: str,
    index_generation: str,
    owner_quota_bytes: int,
    knowledge_base_quota_bytes: int,
    connection_factory: ConnectionFactory = db,
) -> tuple[dict[str, Any], bool]:
    """Atomically reserve quota and claim a deduplicated processing row.

    Returns ``(document, claimed)``. A concurrent request observing an active,
    ready, or OCR document gets the same row with ``claimed=False``. Failed
    rows are reused so retries never multiply documents for the same key.
    """
    init_knowledge_schema(connection_factory)
    connection = connection_factory()
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        if _writable_base(connection, knowledge_base_id, owner_id) is None:
            raise LookupError("Knowledge base not found")
        existing = connection.execute(
            """
            SELECT id, knowledge_base_id, name, source_type, media_type,
                   byte_size, sha256, storage_path, status, parser_name,
                   warnings_json, error_code, error_message, chunk_count,
                   created_at, updated_at
            FROM knowledge_documents
            WHERE knowledge_base_id=? AND dedup_key=?
            LIMIT 1
            """,
            (knowledge_base_id, dedup_key),
        ).fetchone()
        if existing is not None and (
            existing["status"] != "failed"
            or existing["error_code"] == "stale_processing_cleanup_pending"
        ):
            connection.commit()
            return _document_dict(existing), False

        owner_used, base_used = _quota_usage(connection, knowledge_base_id, owner_id)
        # A failed row no longer reserves quota, so a retry adds the full size.
        for scope, used, limit in (
            ("owner", owner_used, owner_quota_bytes),
            ("knowledge_base", base_used, knowledge_base_quota_bytes),
        ):
            if limit >= 0 and used + byte_size > limit:
                raise DocumentIngestionError(
                    "storage_quota_exceeded",
                    "知识库原文件存储配额不足。",
                    details={
                        "scope": scope,
                        "used_bytes": used,
                        "requested_bytes": byte_size,
                        "limit_bytes": limit,
                    },
                )

        if existing is None:
            connection.execute(
                """
                INSERT INTO knowledge_documents(
                    id, knowledge_base_id, name, source_type, media_type,
                    byte_size, sha256, storage_path, status, parser_name,
                    chunk_count, updated_at, dedup_key, index_generation
                ) VALUES (?,?,?,?,?,?,?, '', 'processing', ?, 0,
                          CURRENT_TIMESTAMP, ?, ?)
                """,
                (
                    document_id,
                    knowledge_base_id,
                    name,
                    source_type,
                    media_type,
                    byte_size,
                    sha256,
                    parser_name,
                    dedup_key,
                    index_generation,
                ),
            )
            claimed_id = document_id
        else:
            claimed_id = str(existing["id"])
            connection.execute(
                """
                UPDATE knowledge_documents
                SET name=?, source_type=?, media_type=?, byte_size=?, sha256=?,
                    storage_path='', status='processing', parser_name=?,
                    warnings_json='[]', error_code='', error_message='',
                    chunk_count=0, updated_at=CURRENT_TIMESTAMP,
                    index_generation=?
                WHERE id=? AND status='failed'
                """,
                (
                    name,
                    source_type,
                    media_type,
                    byte_size,
                    sha256,
                    parser_name,
                    index_generation,
                    claimed_id,
                ),
            )
        row = connection.execute(
            """
            SELECT id, knowledge_base_id, name, source_type, media_type,
                   byte_size, sha256, storage_path, status, parser_name,
                   warnings_json, error_code, error_message, chunk_count,
                   created_at, updated_at
            FROM knowledge_documents WHERE id=?
            """,
            (claimed_id,),
        ).fetchone()
        connection.commit()
        return _document_dict(row), True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_document_storage_path(
    document_id: str,
    owner_id: str | None,
    storage_path: str,
    *,
    connection_factory: ConnectionFactory = db,
) -> None:
    connection = connection_factory()
    try:
        cursor = connection.execute(
            """
            UPDATE knowledge_documents SET storage_path=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='processing' AND knowledge_base_id IN (
                SELECT id FROM knowledge_bases
                WHERE owner_id=? OR (owner_id IS NULL AND ? IS NULL)
            )
            """,
            (storage_path, document_id, owner_id, owner_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("Knowledge document is not processing")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_document_status(
    document_id: str,
    owner_id: str | None,
    status: str,
    *,
    error_code: str = "",
    error_message: str = "",
    parser_name: str = "",
    warnings: Sequence[str] = (),
    clear_storage_path: bool = False,
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any]:
    if status not in VALID_DOCUMENT_STATUSES:
        raise ValueError(f"Unsupported knowledge document status: {status}")
    connection = connection_factory()
    try:
        writable = connection.execute(
            """
            SELECT doc.id
            FROM knowledge_documents doc
            JOIN knowledge_bases kb ON kb.id=doc.knowledge_base_id
            WHERE doc.id=? AND (kb.owner_id=? OR (kb.owner_id IS NULL AND ? IS NULL))
            """,
            (document_id, owner_id, owner_id),
        ).fetchone()
        if writable is None:
            raise LookupError("Knowledge document not found")
        connection.execute(
            """
            UPDATE knowledge_documents
            SET status=?, error_code=?, error_message=?,
                parser_name=CASE WHEN ?='' THEN parser_name ELSE ? END,
                warnings_json=?,
                storage_path=CASE WHEN ? THEN '' ELSE storage_path END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                status,
                error_code,
                error_message,
                parser_name,
                parser_name,
                json.dumps(list(warnings), ensure_ascii=False),
                1 if clear_storage_path else 0,
                document_id,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_knowledge_document(document_id, owner_id, connection_factory=connection_factory)


def persist_ready_document(
    *,
    document_id: str,
    owner_id: str | None,
    ingestion: IngestionResult,
    chunk_ids: Sequence[str],
    embeddings: Sequence[Sequence[float]],
    content_hashes: Sequence[str],
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any]:
    """Atomically replace local chunks and publish the document as ready."""
    chunks = ingestion.chunks
    if not (len(chunks) == len(chunk_ids) == len(embeddings) == len(content_hashes)):
        raise ValueError("Chunk ids, embeddings, and hashes must align with ingestion chunks")
    connection = connection_factory()
    try:
        writable = connection.execute(
            """
            SELECT doc.id, doc.status
            FROM knowledge_documents doc
            JOIN knowledge_bases kb ON kb.id=doc.knowledge_base_id
            WHERE doc.id=? AND (kb.owner_id=? OR (kb.owner_id IS NULL AND ? IS NULL))
            """,
            (document_id, owner_id, owner_id),
        ).fetchone()
        if writable is None:
            raise LookupError("Knowledge document not found")
        if writable["status"] != "processing":
            raise RuntimeError("Knowledge document is no longer processing")
        connection.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (document_id,))
        for index, (chunk, chunk_id, embedding, content_hash) in enumerate(
            zip(chunks, chunk_ids, embeddings, content_hashes)
        ):
            connection.execute(
                """
                INSERT INTO knowledge_chunks(
                    id, document_id, chunk_index, content, embedding,
                    token_count, section_path_json, locator_json,
                    block_types_json, embedding_content, content_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    chunk_id,
                    document_id,
                    index,
                    chunk.content,
                    json.dumps(list(embedding)),
                    int(chunk.token_count),
                    json.dumps(list(chunk.section_path), ensure_ascii=False),
                    json.dumps(dict(chunk.locator), ensure_ascii=False),
                    json.dumps(list(chunk.block_types), ensure_ascii=False),
                    chunk.embedding_content,
                    content_hash,
                ),
            )
        connection.execute(
            """
            UPDATE knowledge_documents
            SET source_type=?, status='ready', parser_name=?, warnings_json=?,
                error_code='', error_message='', chunk_count=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                ingestion.document.source_type,
                ingestion.parser_name,
                json.dumps(list(ingestion.warnings), ensure_ascii=False),
                len(chunks),
                document_id,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_knowledge_document(document_id, owner_id, connection_factory=connection_factory)


def claim_stale_processing_documents(
    *,
    stale_after_seconds: int,
    connection_factory: ConnectionFactory = db,
) -> list[dict[str, Any]]:
    """Claim stale work for idempotent external cleanup."""
    init_knowledge_schema(connection_factory)
    connection = connection_factory()
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        modifier = f"-{max(0, int(stale_after_seconds))} seconds"
        rows = connection.execute(
            """
            SELECT doc.id, doc.storage_path, doc.updated_at, kb.owner_id
            FROM knowledge_documents doc
            JOIN knowledge_bases kb ON kb.id=doc.knowledge_base_id
            WHERE (
                doc.status='processing'
                AND datetime(doc.updated_at) <= datetime('now', ?)
            ) OR (
                doc.status='failed'
                AND doc.error_code='stale_processing_cleanup_pending'
            )
            """,
            (modifier,),
        ).fetchall()
        processing_ids = [str(row["id"]) for row in rows if row["id"]]
        if processing_ids:
            placeholders = ",".join("?" for _ in processing_ids)
            connection.execute(
                f"""
                UPDATE knowledge_documents
                SET status='failed', error_code='stale_processing_cleanup_pending',
                    error_message='Stale processing document is awaiting cleanup',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id IN ({placeholders}) AND status='processing'
                """,
                processing_ids,
            )
        connection.commit()
        return [dict(row) for row in rows]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_stale_processing_cleanup(
    document_id: str,
    *,
    connection_factory: ConnectionFactory = db,
) -> bool:
    connection = connection_factory()
    try:
        cursor = connection.execute(
            """
            UPDATE knowledge_documents
            SET storage_path='', error_code='stale_processing',
                error_message='Processing timed out and was recovered',
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='failed'
              AND error_code='stale_processing_cleanup_pending'
            """,
            (document_id,),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_document_chunks(
    document_id: str,
    owner_id: str | None,
    *,
    connection_factory: ConnectionFactory = db,
) -> list[dict[str, Any]]:
    # Authorize through the document lookup before returning content.
    get_knowledge_document(document_id, owner_id, connection_factory=connection_factory)
    connection = connection_factory()
    try:
        rows = connection.execute(
            """
            SELECT id, document_id, chunk_index, content, embedding,
                   token_count, section_path_json, locator_json,
                   block_types_json, embedding_content, content_hash
            FROM knowledge_chunks WHERE document_id=? ORDER BY chunk_index
            """,
            (document_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["embedding"] = _decode_json(item["embedding"], [])
            item["section_path"] = _decode_json(item.pop("section_path_json"), [])
            item["locator"] = _decode_json(item.pop("locator_json"), {})
            item["block_types"] = _decode_json(item.pop("block_types_json"), [])
            result.append(item)
        return result
    finally:
        connection.close()
