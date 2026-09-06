"""Framework-independent domain contracts for document ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


BlockType = Literal["heading", "paragraph", "list", "table", "code", "record"]


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    """One structural unit emitted by a file-format parser."""

    block_type: BlockType
    text: str
    section_path: tuple[str, ...] = ()
    locator: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", self.text.strip())
        object.__setattr__(self, "section_path", tuple(item.strip() for item in self.section_path if item.strip()))
        object.__setattr__(self, "locator", dict(self.locator))


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Normalized document independent of HTTP, persistence, or vector stores."""

    title: str
    source_type: str
    blocks: tuple[DocumentBlock, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", self.title.strip() or "未命名文档")
        object.__setattr__(self, "source_type", self.source_type.strip().lower())
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """A persistence-ready chunk whose embedding payload is separately visible."""

    content: str
    embedding_content: str
    token_count: int
    section_path: tuple[str, ...] = ()
    locator: Mapping[str, Any] = field(default_factory=dict)
    block_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", self.content.strip())
        object.__setattr__(self, "embedding_content", self.embedding_content.strip())
        object.__setattr__(self, "section_path", tuple(self.section_path))
        object.__setattr__(self, "locator", dict(self.locator))
        object.__setattr__(self, "block_types", tuple(dict.fromkeys(self.block_types)))


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Stable boundary returned to API/persistence orchestration."""

    document: ParsedDocument
    chunks: tuple[ChunkDraft, ...]
    parser_name: str
    warnings: tuple[str, ...] = ()


class DocumentIngestionError(RuntimeError):
    """A safe, stable ingestion failure suitable for an API error response."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def dependency_missing(package: str, parser_name: str) -> DocumentIngestionError:
    return DocumentIngestionError(
        "parser_dependency_missing",
        f"解析 {parser_name} 文件需要安装依赖 {package}。请执行 pip install -r requirements.txt。",
        details={"package": package, "parser": parser_name},
    )
