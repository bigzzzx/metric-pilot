"""Central resource limits for untrusted document ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
import re
import struct
import zipfile

from backend.knowledge.models import DocumentBlock, DocumentIngestionError, ParsedDocument


@dataclass(frozen=True, slots=True)
class IngestionLimits:
    max_file_bytes: int = 20 * 1024 * 1024
    max_ooxml_members: int = 5_000
    max_ooxml_uncompressed_bytes: int = 100 * 1024 * 1024
    max_ooxml_member_bytes: int = 25 * 1024 * 1024
    max_ooxml_compression_ratio: float = 200.0
    max_ooxml_central_directory_bytes: int = 10 * 1024 * 1024
    max_document_blocks: int = 50_000
    max_document_characters: int = 20 * 1024 * 1024
    max_block_characters: int = 2 * 1024 * 1024
    max_yaml_aliases: int = 64
    max_yaml_nodes: int = 100_000
    max_yaml_depth: int = 64
    max_yaml_expanded_nodes: int = 100_000
    max_structured_depth: int = 128
    max_tabular_rows: int = 50_000
    max_tabular_columns: int = 2_048
    max_tabular_cells: int = 5_000_000
    max_tabular_cell_characters: int = 256 * 1024
    # PDF extraction is performed in an isolated worker because pdfplumber /
    # pdfminer build a page layout in memory before returning text or tables.
    # These limits bound the cheap page metadata checked before that work and
    # the worker's lifetime/resources when a malformed page expands heavily.
    max_pdf_page_width_points: float = 20_000.0
    max_pdf_page_height_points: float = 20_000.0
    max_pdf_page_area_points: float = 100_000_000.0
    max_pdf_content_streams_per_page: int = 256
    max_pdf_content_stream_bytes_per_page: int = 16 * 1024 * 1024
    max_pdf_filters_per_stream: int = 8
    max_pdf_xref_objects: int = 250_000
    max_pdf_resource_objects_per_page: int = 20_000
    max_pdf_annotations_per_page: int = 10_000
    max_pdf_layout_objects_per_page: int = 250_000
    max_pdf_page_text_characters: int = 2 * 1024 * 1024
    max_pdf_tables_per_page: int = 2_000
    max_pdf_extraction_seconds: float = 30.0
    max_pdf_worker_memory_bytes: int = 768 * 1024 * 1024
    max_semantic_sentences: int = 1_024
    semantic_embedding_batch_size: int = 64
    max_characters_per_token_budget: int = 8


DEFAULT_LIMITS = IngestionLimits()
OOXML_PARSERS = frozenset({"docx", "xlsx", "pptx"})
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_STRUCT = struct.Struct("<4s4H2LH")
_EOCD_MIN_SIZE = _EOCD_STRUCT.size
_MAX_ZIP_COMMENT = 65_535


@dataclass(slots=True)
class ParsedContentBudget:
    """Incremental output quota used while parsers are still constructing blocks."""

    limits: IngestionLimits = DEFAULT_LIMITS
    block_count: int = 0
    total_characters: int = 0

    def append(self, blocks: list[DocumentBlock], block: DocumentBlock) -> None:
        self.observe(block)
        blocks.append(block)

    def observe(self, block: DocumentBlock) -> None:
        next_count = self.block_count + 1
        if next_count > self.limits.max_document_blocks:
            raise DocumentIngestionError(
                "document_block_limit_exceeded",
                "解析结果包含过多内容块。",
                details={"actual": next_count, "limit": self.limits.max_document_blocks},
            )
        size = len(block.text)
        if size > self.limits.max_block_characters:
            raise DocumentIngestionError(
                "document_block_too_large",
                "解析结果中的单个内容块过大。",
                details={"block_index": self.block_count, "actual": size, "limit": self.limits.max_block_characters},
            )
        next_total = self.total_characters + size
        if next_total > self.limits.max_document_characters:
            raise DocumentIngestionError(
                "document_character_limit_exceeded",
                "解析结果的文本总量超过限制。",
                details={"actual": next_total, "limit": self.limits.max_document_characters},
            )
        self.block_count = next_count
        self.total_characters = next_total


@dataclass(slots=True)
class TabularBudget:
    """Incrementally bound rows and cells before table text is constructed."""

    limits: IngestionLimits = DEFAULT_LIMITS
    row_count: int = 0
    cell_count: int = 0

    def observe_row(self, row, *, row_number: int, source: str) -> tuple:
        values = tuple(row)
        next_rows = self.row_count + 1
        if next_rows > self.limits.max_tabular_rows:
            raise DocumentIngestionError(
                "tabular_row_limit_exceeded",
                "表格行数超过安全限制。",
                details={"source": source, "row": row_number, "actual": next_rows, "limit": self.limits.max_tabular_rows},
            )
        if len(values) > self.limits.max_tabular_columns:
            raise DocumentIngestionError(
                "tabular_column_limit_exceeded",
                "表格列数超过安全限制。",
                details={"source": source, "row": row_number, "actual": len(values), "limit": self.limits.max_tabular_columns},
            )
        next_cells = self.cell_count + len(values)
        if next_cells > self.limits.max_tabular_cells:
            raise DocumentIngestionError(
                "tabular_cell_limit_exceeded",
                "表格单元格总数超过安全限制。",
                details={"source": source, "row": row_number, "actual": next_cells, "limit": self.limits.max_tabular_cells},
            )
        for column, value in enumerate(values, start=1):
            size = len(str(value)) if value is not None else 0
            if size > self.limits.max_tabular_cell_characters:
                raise DocumentIngestionError(
                    "tabular_cell_too_large",
                    "表格中的单个单元格内容过大。",
                    details={
                        "source": source,
                        "row": row_number,
                        "column": column,
                        "actual": size,
                        "limit": self.limits.max_tabular_cell_characters,
                    },
                )
        self.row_count = next_rows
        self.cell_count = next_cells
        return values


def validate_ooxml_archive(
    data: bytes,
    *,
    parser_name: str,
    limits: IngestionLimits = DEFAULT_LIMITS,
) -> None:
    """Reject ZIP bombs and unsafe member names before an OOXML library opens it."""
    declared_entries = _preflight_ooxml_eocd(data, parser_name=parser_name, limits=limits)
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentIngestionError(
            "archive_invalid",
            "Office 文件不是有效的 OOXML ZIP 归档或已经损坏。",
            details={"parser": parser_name},
        ) from exc

    try:
        members = archive.infolist()
        if len(members) != declared_entries:
            raise DocumentIngestionError(
                "archive_invalid",
                "Office 文件中央目录的成员数量声明不一致。",
                details={"parser": parser_name, "declared": declared_entries, "actual": len(members)},
            )
        if len(members) > limits.max_ooxml_members:
            raise _archive_limit(
                "archive_member_limit_exceeded",
                "Office 文件包含过多归档成员。",
                parser_name,
                actual=len(members),
                limit=limits.max_ooxml_members,
            )

        total_uncompressed = 0
        for member in members:
            _validate_archive_path(member.filename, parser_name)
            if member.flag_bits & 0x1:
                raise DocumentIngestionError(
                    "archive_encrypted",
                    "Office 文件包含加密成员，无法安全解析。",
                    details={"parser": parser_name, "member": member.filename},
                )
            if member.file_size > limits.max_ooxml_member_bytes:
                raise _archive_limit(
                    "archive_member_too_large",
                    "Office 文件中的单个归档成员过大。",
                    parser_name,
                    member=member.filename,
                    actual=member.file_size,
                    limit=limits.max_ooxml_member_bytes,
                )
            total_uncompressed += member.file_size
            if total_uncompressed > limits.max_ooxml_uncompressed_bytes:
                raise _archive_limit(
                    "archive_uncompressed_too_large",
                    "Office 文件解压后的总体积超过限制。",
                    parser_name,
                    actual=total_uncompressed,
                    limit=limits.max_ooxml_uncompressed_bytes,
                )
            if member.file_size:
                ratio = member.file_size / max(1, member.compress_size)
                if ratio > limits.max_ooxml_compression_ratio:
                    raise _archive_limit(
                        "archive_suspicious_compression",
                        "Office 文件的压缩比异常，可能是 ZIP bomb。",
                        parser_name,
                        member=member.filename,
                        actual=round(ratio, 2),
                        limit=limits.max_ooxml_compression_ratio,
                    )
    finally:
        archive.close()


def validate_parsed_document(
    document: ParsedDocument,
    *,
    limits: IngestionLimits = DEFAULT_LIMITS,
) -> None:
    """Apply format-independent quotas before chunking or vectorization."""
    budget = ParsedContentBudget(limits)
    for block in document.blocks:
        budget.observe(block)


def _validate_archive_path(filename: str, parser_name: str) -> None:
    normalized = filename.replace("\\", "/")
    path = PurePosixPath(normalized)
    dangerous = (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or bool(_WINDOWS_DRIVE.match(normalized))
        or any(part == ".." for part in path.parts)
    )
    if dangerous:
        raise DocumentIngestionError(
            "archive_dangerous_path",
            "Office 文件包含不安全的归档路径。",
            details={"parser": parser_name, "member": filename},
        )


def _archive_limit(code: str, message: str, parser: str, **details) -> DocumentIngestionError:
    return DocumentIngestionError(code, message, details={"parser": parser, **details})


def _preflight_ooxml_eocd(data: bytes, *, parser_name: str, limits: IngestionLimits) -> int:
    """Read EOCD without constructing ZipInfo objects from an attacker-sized directory."""
    minimum = max(0, len(data) - (_EOCD_MIN_SIZE + _MAX_ZIP_COMMENT))
    position = data.rfind(_EOCD_SIGNATURE, minimum)
    values = None
    while position >= minimum:
        if position + _EOCD_MIN_SIZE <= len(data):
            candidate = _EOCD_STRUCT.unpack_from(data, position)
            comment_length = candidate[-1]
            if position + _EOCD_MIN_SIZE + comment_length == len(data):
                values = candidate
                break
        position = data.rfind(_EOCD_SIGNATURE, minimum, position)
    if values is None:
        raise DocumentIngestionError(
            "archive_invalid",
            "Office 文件缺少有效的 ZIP 中央目录结束记录。",
            details={"parser": parser_name},
        )

    _, disk_number, directory_disk, entries_on_disk, entries_total, directory_size, directory_offset, _ = values
    if 0xFFFF in {entries_on_disk, entries_total} or 0xFFFFFFFF in {directory_size, directory_offset}:
        raise DocumentIngestionError(
            "archive_zip64_unsupported",
            "Office 文件使用 ZIP64 或其目录规模超出当前安全限制。",
            details={"parser": parser_name},
        )
    if disk_number != 0 or directory_disk != 0 or entries_on_disk != entries_total:
        raise DocumentIngestionError(
            "archive_multidisk_unsupported",
            "不支持跨磁盘 ZIP 格式的 Office 文件。",
            details={"parser": parser_name},
        )
    if entries_total > limits.max_ooxml_members:
        raise _archive_limit(
            "archive_member_limit_exceeded",
            "Office 文件包含过多归档成员。",
            parser_name,
            actual=entries_total,
            limit=limits.max_ooxml_members,
        )
    if directory_size > limits.max_ooxml_central_directory_bytes:
        raise _archive_limit(
            "archive_central_directory_too_large",
            "Office 文件的 ZIP 中央目录过大。",
            parser_name,
            actual=directory_size,
            limit=limits.max_ooxml_central_directory_bytes,
        )
    if directory_offset + directory_size > position:
        raise DocumentIngestionError(
            "archive_invalid",
            "Office 文件的 ZIP 中央目录偏移无效。",
            details={"parser": parser_name},
        )
    return entries_total
