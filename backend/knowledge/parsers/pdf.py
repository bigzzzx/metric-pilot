"""Searchable PDF parser with explicit OCR-required detection."""

from __future__ import annotations

from io import BytesIO
import re

from backend.knowledge.limits import DEFAULT_LIMITS, IngestionLimits, ParsedContentBudget, TabularBudget
from backend.knowledge.models import DocumentBlock, DocumentIngestionError, ParsedDocument, dependency_missing
from backend.knowledge.parsers.base import document_title, require_readable
from backend.knowledge.parsers.table_utils import cell_text, markdown_row


def _paragraphs(text: str):
    start = 0
    for boundary in re.finditer(r"\n\s*\n|(?<=\.)\s*\n", text):
        part = text[start:boundary.start()].strip()
        if part:
            yield part
        start = boundary.end()
    tail = text[start:].strip()
    if tail:
        yield tail


def _non_whitespace_count(text: str) -> int:
    return sum(1 for character in text if not character.isspace())


class PdfParser:
    name = "pdf"
    extensions = frozenset({"pdf"})
    media_types = frozenset({"application/pdf"})

    def __init__(
        self,
        *,
        max_pages: int = 300,
        ocr_text_threshold: int = 8,
        limits: IngestionLimits = DEFAULT_LIMITS,
    ):
        self.max_pages = max_pages
        self.ocr_text_threshold = ocr_text_threshold
        self.limits = limits

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        try:
            import pdfplumber
        except ImportError as exc:  # pragma: no cover
            raise dependency_missing("pdfplumber", self.name) from exc

        try:
            pdf = pdfplumber.open(BytesIO(data))
        except Exception as exc:
            lowered = str(exc).lower()
            if "password" in lowered or "encrypt" in lowered:
                raise DocumentIngestionError("encrypted_pdf", "PDF 已加密，请移除密码后重新上传。", details={"parser": self.name}) from exc
            raise DocumentIngestionError("parse_failed", "PDF 文件无法读取或已损坏。", details={"parser": self.name}) from exc

        blocks: list[DocumentBlock] = []
        warnings: list[str] = []
        budget = ParsedContentBudget(self.limits)
        table_budget = TabularBudget(self.limits)
        extracted_characters = 0
        image_count = 0
        page_count = 0
        try:
            page_count = len(pdf.pages)
            if page_count > self.max_pages:
                raise DocumentIngestionError(
                    "page_limit_exceeded",
                    f"PDF 页数超过 {self.max_pages} 页限制。",
                    details={"page_count": page_count, "max_pages": self.max_pages},
                )
            for page_number, page in enumerate(pdf.pages, start=1):
                image_count += len(getattr(page, "images", ()) or ())
                try:
                    text = (page.extract_text(x_tolerance=2, y_tolerance=3) or "").strip()
                except Exception as exc:
                    warnings.append(f"第 {page_number} 页正文提取失败：{type(exc).__name__}")
                    text = ""
                extracted_characters += _non_whitespace_count(text)
                for paragraph_index, paragraph in enumerate(_paragraphs(text), start=1):
                    budget.append(
                        blocks,
                        DocumentBlock(
                            "paragraph",
                            paragraph,
                            locator={"page": page_number, "paragraph": paragraph_index},
                        )
                    )

                try:
                    tables = page.extract_tables() or []
                except Exception as exc:
                    warnings.append(f"第 {page_number} 页表格提取失败：{type(exc).__name__}")
                    tables = []
                for table_index, table in enumerate(tables, start=1):
                    rows = iter(table)
                    first = next(rows, None)
                    if first is None:
                        continue
                    source = f"page:{page_number}:table:{table_index}"
                    header_values = table_budget.observe_row(first, row_number=1, source=source)
                    header = tuple(cell_text(value) or f"column_{index}" for index, value in enumerate(header_values, start=1))
                    data_rows = 0
                    for row_index, row in enumerate(rows, start=2):
                        values = table_budget.observe_row(row, row_number=row_index, source=source)
                        if not any(value is not None and str(value).strip() for value in values):
                            continue
                        data_rows += 1
                        value = markdown_row(header, values)
                        extracted_characters += _non_whitespace_count(value)
                        budget.append(
                            blocks,
                            DocumentBlock(
                                "table",
                                value,
                                locator={
                                    "page": page_number,
                                    "table": table_index,
                                    "row_start": row_index,
                                    "row_end": row_index,
                                    "columns": header,
                                },
                            ),
                        )
                    if data_rows == 0:
                        value = "列：" + ", ".join(header)
                        extracted_characters += _non_whitespace_count(value)
                        budget.append(
                            blocks,
                            DocumentBlock("record", value, locator={"page": page_number, "table": table_index, "row_start": 1, "row_end": 1}),
                        )
        finally:
            pdf.close()

        if extracted_characters < self.ocr_text_threshold:
            raise DocumentIngestionError(
                "ocr_required",
                "PDF 中没有足够的可搜索文字，可能是扫描件，需要先执行 OCR。",
                details={"page_count": page_count, "image_count": image_count, "parser": self.name},
            )
        require_readable(len(blocks), self.name)
        return ParsedDocument(
            document_title(filename),
            "pdf",
            tuple(blocks),
            {"media_type": media_type or "application/pdf", "page_count": page_count, "image_count": image_count},
            tuple(warnings),
        )
