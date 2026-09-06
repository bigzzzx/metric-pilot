"""Application service for parse-and-chunk; persistence remains an outer concern."""

from __future__ import annotations

from backend.knowledge.chunking.pipeline import ChunkingPipeline
from backend.knowledge.limits import (
    DEFAULT_LIMITS,
    OOXML_PARSERS,
    IngestionLimits,
    validate_ooxml_archive,
    validate_parsed_document,
)
from backend.knowledge.models import DocumentIngestionError, IngestionResult
from backend.knowledge.parsers.registry import ParserRegistry, default_parser_registry


DEFAULT_MAX_FILE_BYTES = DEFAULT_LIMITS.max_file_bytes


class DocumentIngestionService:
    """Stable entry point used by FastAPI, tests, and future background jobs."""

    def __init__(
        self,
        *,
        registry: ParserRegistry | None = None,
        chunking: ChunkingPipeline | None = None,
        max_file_bytes: int | None = None,
        limits: IngestionLimits | None = None,
    ) -> None:
        self.limits = limits or DEFAULT_LIMITS
        self.registry = registry or default_parser_registry(limits=self.limits)
        self.chunking = chunking or ChunkingPipeline(limits=self.limits)
        self.max_file_bytes = max_file_bytes if max_file_bytes is not None else self.limits.max_file_bytes

    def parse_and_chunk(self, filename: str, media_type: str | None, data: bytes) -> IngestionResult:
        if not filename or not filename.strip():
            raise DocumentIngestionError("invalid_filename", "文件名不能为空。")
        if not isinstance(data, bytes):
            raise DocumentIngestionError("invalid_payload", "文档内容必须是二进制数据。")
        if not data:
            raise DocumentIngestionError("no_readable_content", "文档内容为空。")
        if len(data) > self.max_file_bytes:
            raise DocumentIngestionError(
                "file_too_large",
                f"文件超过 {self.max_file_bytes // (1024 * 1024)} MB 限制。",
                details={"size": len(data), "max_size": self.max_file_bytes},
            )

        parser = self.registry.resolve(filename, media_type, data)
        try:
            if parser.name in OOXML_PARSERS and getattr(parser, "limits", None) != self.limits:
                validate_ooxml_archive(data, parser_name=parser.name, limits=self.limits)
            document = parser.parse(filename, media_type, data)
            validate_parsed_document(document, limits=self.limits)
            chunks = self.chunking.chunk(document)
        except DocumentIngestionError:
            raise
        except Exception as exc:
            raise DocumentIngestionError(
                "parse_failed",
                f"文档解析失败：{type(exc).__name__}。",
                details={"parser": parser.name},
            ) from exc
        if not chunks:
            raise DocumentIngestionError(
                "no_readable_content",
                "文档没有生成可检索内容。",
                details={"parser": parser.name},
            )
        return IngestionResult(document, chunks, parser.name, document.warnings)


_DEFAULT_SERVICE: DocumentIngestionService | None = None


def parse_and_chunk_document(filename: str, media_type: str | None, data: bytes) -> IngestionResult:
    """Stateless-looking convenience wrapper around the reusable service."""
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = DocumentIngestionService()
    return _DEFAULT_SERVICE.parse_and_chunk(filename, media_type, data)


def parse_text_document(filename: str, content: str, source_type: str = "txt") -> IngestionResult:
    """Compatibility entry point for pasted text while preserving one pipeline."""
    media_types = {
        "txt": "text/plain",
        "md": "text/markdown",
        "markdown": "text/markdown",
        "json": "application/json",
        "yaml": "application/yaml",
        "yml": "application/yaml",
        "csv": "text/csv",
        "sql": "application/sql",
        "html": "text/html",
    }
    extension = source_type.lower().removeprefix("file:").lstrip(".")
    normalized_filename = filename if "." in filename.rsplit("/", 1)[-1] else f"{filename}.{extension}"
    return parse_and_chunk_document(normalized_filename, media_types.get(extension), content.encode("utf-8"))
