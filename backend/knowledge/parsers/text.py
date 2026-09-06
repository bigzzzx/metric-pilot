"""Plain-text and SQL parsers."""

from __future__ import annotations

import re

from backend.knowledge.models import DocumentBlock, ParsedDocument, dependency_missing
from backend.knowledge.parsers.base import decode_text, document_title, require_readable


def _paragraphs(text: str) -> list[tuple[str, int, int]]:
    result: list[tuple[str, int, int]] = []
    for match in re.finditer(r"\S(?:.*?\S)?(?=\r?\n\s*\r?\n|\Z)", text, flags=re.DOTALL):
        value = match.group(0).strip()
        if value:
            result.append((value, match.start(), match.end()))
    if not result and text.strip():
        start = len(text) - len(text.lstrip())
        result.append((text.strip(), start, len(text.rstrip())))
    return result


class TextParser:
    name = "text"
    extensions = frozenset({"txt"})
    media_types = frozenset({"text/plain"})

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        text = decode_text(data, parser_name=self.name)
        blocks = tuple(
            DocumentBlock(
                "paragraph",
                value,
                locator={"paragraph": index, "char_start": start, "char_end": end},
            )
            for index, (value, start, end) in enumerate(_paragraphs(text), start=1)
        )
        require_readable(len(blocks), self.name)
        return ParsedDocument(document_title(filename), "txt", blocks, {"media_type": media_type or "text/plain"})


class SqlParser:
    name = "sql"
    extensions = frozenset({"sql"})
    media_types = frozenset({"application/sql", "text/sql", "application/x-sql"})

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        text = decode_text(data, parser_name=self.name).strip()
        try:
            import sqlparse
        except ImportError as exc:  # pragma: no cover - exercised in minimal deployments
            raise dependency_missing("sqlparse", self.name) from exc

        statements = [statement.strip() for statement in sqlparse.split(text) if statement.strip()]
        blocks = tuple(
            DocumentBlock(
                "code",
                statement,
                locator={"statement": index, "language": "sql"},
            )
            for index, statement in enumerate(statements, start=1)
        )
        require_readable(len(blocks), self.name)
        return ParsedDocument(document_title(filename), "sql", blocks, {"media_type": media_type or "application/sql"})
