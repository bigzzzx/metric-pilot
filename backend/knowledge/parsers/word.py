"""DOCX parser preserving Word headings, paragraphs, lists, and tables."""

from __future__ import annotations

from io import BytesIO

from backend.knowledge.limits import (
    DEFAULT_LIMITS,
    IngestionLimits,
    ParsedContentBudget,
    TabularBudget,
    validate_ooxml_archive,
)
from backend.knowledge.models import DocumentBlock, DocumentIngestionError, ParsedDocument, dependency_missing
from backend.knowledge.parsers.base import document_title, require_readable
from backend.knowledge.parsers.table_utils import cell_text, markdown_row


class WordParser:
    name = "docx"
    extensions = frozenset({"docx"})
    media_types = frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"})

    def __init__(self, *, limits: IngestionLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        validate_ooxml_archive(data, parser_name=self.name, limits=self.limits)
        try:
            import docx
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError as exc:  # pragma: no cover
            raise dependency_missing("python-docx", self.name) from exc
        try:
            document = docx.Document(BytesIO(data))
        except Exception as exc:
            raise DocumentIngestionError("parse_failed", "DOCX 文件无法读取或已损坏。", details={"parser": self.name}) from exc

        section: list[str] = []
        blocks: list[DocumentBlock] = []
        budget = ParsedContentBudget(self.limits)
        table_budget = TabularBudget(self.limits)
        paragraph_index = 0
        table_index = 0
        # python-docx exposes paragraphs and tables as separate lists. Iterating
        # body XML keeps their original reading order without parsing XML details
        # in the rest of the application.
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                paragraph_index += 1
                paragraph = Paragraph(child, document)
                value = paragraph.text.strip()
                if not value:
                    continue
                style = (paragraph.style.name if paragraph.style else "").strip()
                if style.lower().startswith("heading"):
                    try:
                        level = max(1, min(6, int(style.rsplit(" ", 1)[-1])))
                    except ValueError:
                        level = 1
                    section = section[: level - 1]
                    section.append(value)
                    budget.append(blocks, DocumentBlock("heading", value, tuple(section), {"paragraph": paragraph_index, "level": level}))
                else:
                    is_list = style.lower().startswith("list")
                    budget.append(
                        blocks,
                        DocumentBlock(
                            "list" if is_list else "paragraph",
                            value,
                            tuple(section),
                            {"paragraph": paragraph_index, "style": style},
                        )
                    )
            elif child.tag.endswith("}tbl"):
                table_index += 1
                table = Table(child, document)
                rows = iter(table.rows)
                first = next(rows, None)
                if first is None:
                    continue
                header_values = table_budget.observe_row(
                    (cell.text for cell in first.cells),
                    row_number=1,
                    source=f"table:{table_index}",
                )
                header = tuple(cell_text(value) or f"column_{index}" for index, value in enumerate(header_values, start=1))
                data_rows = 0
                for row_index, row in enumerate(rows, start=2):
                    values = table_budget.observe_row(
                        (cell.text for cell in row.cells),
                        row_number=row_index,
                        source=f"table:{table_index}",
                    )
                    if not any(str(value).strip() for value in values):
                        continue
                    data_rows += 1
                    budget.append(
                        blocks,
                        DocumentBlock(
                            "table",
                            markdown_row(header, values),
                            tuple(section),
                            {"table": table_index, "row_start": row_index, "row_end": row_index, "columns": header},
                        ),
                    )
                if data_rows == 0:
                    budget.append(
                        blocks,
                        DocumentBlock("record", "列：" + ", ".join(header), tuple(section), {"table": table_index, "row_start": 1, "row_end": 1}),
                    )

        readable = [block for block in blocks if block.block_type != "heading"]
        require_readable(len(readable), self.name)
        core_title = (document.core_properties.title or "").strip()
        heading_title = next((block.text for block in blocks if block.block_type == "heading"), "")
        return ParsedDocument(
            core_title or heading_title or document_title(filename),
            "docx",
            tuple(blocks),
            {"media_type": media_type or next(iter(self.media_types))},
        )
