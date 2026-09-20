"""Safe persistence and text extraction for uploaded metric-source files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.core import DATA_DIR
from backend.knowledge.service import parse_and_chunk_document
from backend.knowledge.source_storage import SourceStorage, source_fingerprint


METRIC_SOURCE_ROOT = DATA_DIR / "metric-sources"


@dataclass(frozen=True, slots=True)
class UploadedMetricSource:
    filename: str
    media_type: str
    content: str
    content_hash: str
    storage_path: str
    byte_size: int
    parser_name: str
    warnings: tuple[str, ...]


def ingest_uploaded_metric_source(*, metric_id: str, user_id: str, filename: str, media_type: str | None, data: bytes) -> UploadedMetricSource:
    """Parse a supported document and persist the untouched original bytes."""
    byte_size, content_hash = source_fingerprint(data)
    ingestion = parse_and_chunk_document(filename, media_type, data)
    content = "\n\n".join(block.text for block in ingestion.document.blocks if block.text).strip()
    if not content:
        raise ValueError("文档没有可作为指标口径证据的正文。")
    stored = SourceStorage(METRIC_SOURCE_ROOT).store(
        owner_id=user_id,
        knowledge_base_id=f"metric:{metric_id}",
        document_id=content_hash,
        filename=filename,
        data=data,
    )
    return UploadedMetricSource(
        filename=Path(filename).name,
        media_type=(media_type or "application/octet-stream").strip(),
        content=content,
        content_hash=content_hash,
        storage_path=stored.storage_path,
        byte_size=byte_size,
        parser_name=ingestion.parser_name,
        warnings=tuple(ingestion.warnings),
    )


def remove_uploaded_metric_source(storage_path: str) -> None:
    """Best-effort cleanup if a database transaction cannot retain an upload."""
    SourceStorage(METRIC_SOURCE_ROOT).remove(storage_path)
