"""Markdown parser built on markdown-it-py block tokens."""

from __future__ import annotations

from backend.knowledge.models import DocumentBlock, ParsedDocument, dependency_missing
from backend.knowledge.parsers.base import decode_text, document_title, require_readable


class MarkdownParser:
    name = "markdown"
    extensions = frozenset({"md", "markdown"})
    media_types = frozenset({"text/markdown", "text/x-markdown"})

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        try:
            from markdown_it import MarkdownIt
        except ImportError as exc:  # pragma: no cover
            raise dependency_missing("markdown-it-py", self.name) from exc

        source = decode_text(data, parser_name=self.name)
        lines = source.splitlines()
        parser = MarkdownIt("commonmark", {"html": False}).enable("table")
        tokens = parser.parse(source)
        section: list[str] = []
        blocks: list[DocumentBlock] = []
        list_depth = 0
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type in {"bullet_list_open", "ordered_list_open"}:
                list_depth += 1
            elif token.type in {"bullet_list_close", "ordered_list_close"}:
                list_depth = max(0, list_depth - 1)
            elif token.type == "heading_open":
                level = int(token.tag[1])
                inline = tokens[index + 1] if index + 1 < len(tokens) else None
                value = (inline.content if inline and inline.type == "inline" else "").strip()
                if value:
                    section = section[: level - 1]
                    section.append(value)
                    start, end = _line_range(token.map)
                    blocks.append(DocumentBlock("heading", value, tuple(section), {"line_start": start, "line_end": end, "level": level}))
            elif token.type == "paragraph_open":
                inline = tokens[index + 1] if index + 1 < len(tokens) else None
                value = (inline.content if inline and inline.type == "inline" else "").strip()
                if value:
                    start, end = _line_range(token.map)
                    blocks.append(
                        DocumentBlock(
                            "list" if list_depth else "paragraph",
                            value,
                            tuple(section),
                            {"line_start": start, "line_end": end, "list_depth": list_depth},
                        )
                    )
            elif token.type in {"fence", "code_block"}:
                start, end = _line_range(token.map)
                blocks.append(
                    DocumentBlock(
                        "code",
                        token.content,
                        tuple(section),
                        {"line_start": start, "line_end": end, "language": (token.info or "").strip()},
                    )
                )
            elif token.type == "table_open":
                start, end = _line_range(token.map)
                value = "\n".join(lines[max(0, start - 1):end]).strip()
                if value:
                    blocks.append(DocumentBlock("table", value, tuple(section), {"line_start": start, "line_end": end}))
                while index + 1 < len(tokens) and tokens[index + 1].type != "table_close":
                    index += 1
            elif token.type == "html_block":
                start, end = _line_range(token.map)
                if token.content.strip():
                    blocks.append(DocumentBlock("paragraph", token.content, tuple(section), {"line_start": start, "line_end": end, "format": "html"}))
            index += 1

        readable = [block for block in blocks if block.block_type != "heading" and block.text]
        require_readable(len(readable), self.name)
        title = next((block.text for block in blocks if block.block_type == "heading"), document_title(filename))
        return ParsedDocument(title, "md", tuple(blocks), {"media_type": media_type or "text/markdown"})


def _line_range(mapping: list[int] | None) -> tuple[int, int]:
    if not mapping:
        return (1, 1)
    return (mapping[0] + 1, mapping[1])
