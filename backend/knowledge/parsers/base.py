"""Parser protocol and shared byte decoding helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from backend.knowledge.models import DocumentIngestionError, ParsedDocument


class DocumentParser(Protocol):
    name: str
    extensions: frozenset[str]
    media_types: frozenset[str]

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument: ...


def document_title(filename: str) -> str:
    return Path(filename).stem.strip() or "未命名文档"


def decode_text(data: bytes, *, parser_name: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentIngestionError(
        "parse_failed",
        f"{parser_name} 文件编码无法识别，请使用 UTF-8 或 GB18030。",
        details={"parser": parser_name},
    )


def require_readable(block_count: int, parser_name: str) -> None:
    if block_count == 0:
        raise DocumentIngestionError(
            "no_readable_content",
            "文档中没有可读取的文本或表格内容。",
            details={"parser": parser_name},
        )
