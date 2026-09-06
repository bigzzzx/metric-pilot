"""CSV and XLSX parsers that keep table headers attached to every row."""

from __future__ import annotations

import csv
from io import BytesIO, StringIO

from backend.knowledge.limits import (
    DEFAULT_LIMITS,
    IngestionLimits,
    ParsedContentBudget,
    TabularBudget,
    validate_ooxml_archive,
)
from backend.knowledge.models import DocumentBlock, DocumentIngestionError, ParsedDocument, dependency_missing
from backend.knowledge.parsers.base import decode_text, document_title, require_readable
from backend.knowledge.parsers.table_utils import cell_text, markdown_row


class CsvParser:
    name = "csv"
    extensions = frozenset({"csv"})
    media_types = frozenset({"text/csv", "application/csv", "application/vnd.ms-excel"})

    def __init__(self, *, limits: IngestionLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        text = decode_text(data, parser_name=self.name)
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(StringIO(text), dialect)
        table_budget = TabularBudget(self.limits)
        content_budget = ParsedContentBudget(self.limits)
        blocks: list[DocumentBlock] = []
        header: tuple[str, ...] | None = None
        header_row = 0
        data_row_count = 0
        for row_number, raw_row in enumerate(reader, start=1):
            row = table_budget.observe_row(raw_row, row_number=row_number, source=filename)
            if not any(str(cell).strip() for cell in row):
                continue
            if header is None:
                header = tuple(cell_text(value) or f"column_{index}" for index, value in enumerate(row, start=1))
                header_row = row_number
                continue
            data_row_count += 1
            content_budget.append(
                blocks,
                DocumentBlock(
                    "table",
                    markdown_row(header, row),
                    section_path=(document_title(filename),),
                    locator={"row_start": row_number, "row_end": row_number, "columns": header, "table": 1},
                ),
            )
        if header is None:
            require_readable(0, self.name)
        # A header-only file still produces a useful schema block.
        if not blocks:
            content_budget.append(
                blocks,
                DocumentBlock("record", "列：" + ", ".join(header), locator={"row_start": header_row, "row_end": header_row, "columns": header}),
            )
        return ParsedDocument(
            document_title(filename),
            "csv",
            tuple(blocks),
            {"media_type": media_type or "text/csv", "columns": header, "row_count": data_row_count},
        )


class XlsxParser:
    name = "xlsx"
    extensions = frozenset({"xlsx"})
    media_types = frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})

    def __init__(self, *, limits: IngestionLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        validate_ooxml_archive(data, parser_name=self.name, limits=self.limits)
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise dependency_missing("openpyxl", self.name) from exc
        try:
            workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise DocumentIngestionError("parse_failed", "XLSX 文件无法读取或已损坏。", details={"parser": self.name}) from exc

        blocks: list[DocumentBlock] = []
        budget = ParsedContentBudget(self.limits)
        table_budget = TabularBudget(self.limits)
        sheets: list[str] = []
        try:
            for sheet in workbook.worksheets:
                sheets.append(sheet.title)
                if sheet.max_row and table_budget.row_count + sheet.max_row > self.limits.max_tabular_rows:
                    raise DocumentIngestionError(
                        "tabular_row_limit_exceeded",
                        "工作簿行数超过安全限制。",
                        details={"sheet": sheet.title, "actual": table_budget.row_count + sheet.max_row, "limit": self.limits.max_tabular_rows},
                    )
                if sheet.max_column and sheet.max_column > self.limits.max_tabular_columns:
                    raise DocumentIngestionError(
                        "tabular_column_limit_exceeded",
                        "工作表列数超过安全限制。",
                        details={"sheet": sheet.title, "actual": sheet.max_column, "limit": self.limits.max_tabular_columns},
                    )
                estimated_cells = (sheet.max_row or 0) * (sheet.max_column or 0)
                if table_budget.cell_count + estimated_cells > self.limits.max_tabular_cells:
                    raise DocumentIngestionError(
                        "tabular_cell_limit_exceeded",
                        "工作簿单元格总数超过安全限制。",
                        details={
                            "sheet": sheet.title,
                            "actual": table_budget.cell_count + estimated_cells,
                            "limit": self.limits.max_tabular_cells,
                        },
                    )
                header: tuple[str, ...] | None = None
                header_row = 0
                data_row_count = 0
                for row_number, raw_row in enumerate(sheet.iter_rows(values_only=True), start=1):
                    row = table_budget.observe_row(raw_row, row_number=row_number, source=sheet.title)
                    if not any(value is not None and str(value).strip() for value in row):
                        continue
                    if header is None:
                        header = tuple(cell_text(value) or f"column_{index}" for index, value in enumerate(row, start=1))
                        header_row = row_number
                        continue
                    data_row_count += 1
                    budget.append(
                        blocks,
                        DocumentBlock(
                            "table",
                            markdown_row(header, row),
                            section_path=(sheet.title,),
                            locator={
                                "sheet": sheet.title,
                                "row_start": row_number,
                                "row_end": row_number,
                                "column_start": 1,
                                "column_end": len(header),
                                "columns": header,
                            },
                        )
                    )
                if header is not None and data_row_count == 0:
                    budget.append(
                        blocks,
                        DocumentBlock(
                            "record",
                            "列：" + ", ".join(header),
                            section_path=(sheet.title,),
                            locator={"sheet": sheet.title, "row_start": header_row, "row_end": header_row, "columns": header},
                        )
                    )
        finally:
            workbook.close()
        require_readable(len(blocks), self.name)
        return ParsedDocument(
            document_title(filename),
            "xlsx",
            tuple(blocks),
            {"media_type": media_type or next(iter(self.media_types)), "sheets": tuple(sheets)},
        )
