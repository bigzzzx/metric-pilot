"""PPTX parser preserving slide titles, body text, notes, and tables."""

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


class PresentationParser:
    name = "pptx"
    extensions = frozenset({"pptx"})
    media_types = frozenset({"application/vnd.openxmlformats-officedocument.presentationml.presentation"})

    def __init__(self, *, limits: IngestionLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        validate_ooxml_archive(data, parser_name=self.name, limits=self.limits)
        try:
            import pptx
        except ImportError as exc:  # pragma: no cover
            raise dependency_missing("python-pptx", self.name) from exc
        try:
            presentation = pptx.Presentation(BytesIO(data))
        except Exception as exc:
            raise DocumentIngestionError("parse_failed", "PPTX 文件无法读取或已损坏。", details={"parser": self.name}) from exc

        blocks: list[DocumentBlock] = []
        budget = ParsedContentBudget(self.limits)
        table_budget = TabularBudget(self.limits)
        deck_title = ""
        for slide_number, slide in enumerate(presentation.slides, start=1):
            slide_title = ""
            if slide.shapes.title is not None:
                slide_title = slide.shapes.title.text.strip()
            if not slide_title:
                slide_title = f"第 {slide_number} 页"
            if slide_number == 1 and slide.shapes.title is not None:
                deck_title = slide.shapes.title.text.strip()
            section = (slide_title,)
            budget.append(blocks, DocumentBlock("heading", slide_title, section, {"slide": slide_number, "element": "title", "level": 1}))

            table_index = 0
            for shape_index, shape in enumerate(slide.shapes, start=1):
                if shape is slide.shapes.title:
                    continue
                if getattr(shape, "has_table", False):
                    table_index += 1
                    rows = iter(shape.table.rows)
                    first = next(rows, None)
                    if first is None:
                        continue
                    source = f"slide:{slide_number}:table:{table_index}"
                    header_values = table_budget.observe_row(
                        (cell.text for cell in first.cells),
                        row_number=1,
                        source=source,
                    )
                    header = tuple(cell_text(value) or f"column_{index}" for index, value in enumerate(header_values, start=1))
                    data_rows = 0
                    for row_index, row in enumerate(rows, start=2):
                        values = table_budget.observe_row(
                            (cell.text for cell in row.cells),
                            row_number=row_index,
                            source=source,
                        )
                        if not any(str(value).strip() for value in values):
                            continue
                        data_rows += 1
                        budget.append(
                            blocks,
                            DocumentBlock(
                                "table",
                                markdown_row(header, values),
                                section,
                                {"slide": slide_number, "element": "table", "table": table_index, "row_start": row_index, "row_end": row_index},
                            ),
                        )
                    if data_rows == 0:
                        budget.append(
                            blocks,
                            DocumentBlock(
                                "record",
                                "列：" + ", ".join(header),
                                section,
                                {"slide": slide_number, "element": "table", "table": table_index, "row_start": 1, "row_end": 1},
                            ),
                        )
                elif getattr(shape, "has_text_frame", False):
                    value = shape.text.strip()
                    if value:
                        budget.append(blocks, DocumentBlock("paragraph", value, section, {"slide": slide_number, "element": "text", "shape": shape_index}))

            try:
                notes = slide.notes_slide.notes_text_frame.text.strip()
            except (AttributeError, KeyError):
                notes = ""
            if notes:
                budget.append(blocks, DocumentBlock("paragraph", notes, section, {"slide": slide_number, "element": "notes"}))

        readable = [block for block in blocks if block.block_type != "heading"]
        require_readable(len(readable), self.name)
        return ParsedDocument(
            deck_title or document_title(filename),
            "pptx",
            tuple(blocks),
            {"media_type": media_type or next(iter(self.media_types)), "slide_count": len(presentation.slides)},
        )
