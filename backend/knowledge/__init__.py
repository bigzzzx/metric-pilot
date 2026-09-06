"""Modular document parsing and structure-aware chunking."""

from backend.knowledge.models import (
    ChunkDraft,
    DocumentBlock,
    DocumentIngestionError,
    IngestionResult,
    ParsedDocument,
)
from backend.knowledge.service import DocumentIngestionService, parse_and_chunk_document

__all__ = [
    "ChunkDraft",
    "DocumentBlock",
    "DocumentIngestionError",
    "DocumentIngestionService",
    "IngestionResult",
    "ParsedDocument",
    "parse_and_chunk_document",
]
