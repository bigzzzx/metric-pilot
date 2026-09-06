from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import re
import struct
import sys
from types import SimpleNamespace
import zipfile

import pytest

from backend.knowledge.chunking.pipeline import ChunkingPipeline
from backend.knowledge.chunking.token_budget import RegexTokenCounter, split_to_token_limit
from backend.knowledge import limits as limits_module
from backend.knowledge.limits import DEFAULT_LIMITS, validate_ooxml_archive
from backend.knowledge.models import DocumentBlock, DocumentIngestionError, ParsedDocument
from backend.knowledge.parsers.registry import ParserRegistry, default_parser_registry
from backend.knowledge.parsers.structured import JsonParser, YamlParser
from backend.knowledge.parsers.tabular import CsvParser, XlsxParser
from backend.knowledge.service import DocumentIngestionService, parse_and_chunk_document


class WordCounter:
    """Predictable token counter used to test chunking invariants."""

    def count(self, text: str) -> int:
        return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))

    def truncate(self, text: str, max_tokens: int) -> str:
        matches = list(re.finditer(r"\w+|[^\w\s]", text, flags=re.UNICODE))
        if len(matches) <= max_tokens:
            return text
        return text[: matches[max_tokens - 1].end()].rstrip()


class TopicEmbedding:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append([1.0, 0.0] if "apple" in lowered else [0.0, 1.0])
        return vectors


class RecordingEmbedding(TopicEmbedding):
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return super().embed_texts(texts)


def _service(*, target_tokens: int = 24, max_tokens: int = 36) -> DocumentIngestionService:
    return DocumentIngestionService(
        chunking=ChunkingPipeline(
            token_counter=WordCounter(),
            embedding_client=TopicEmbedding(),
            target_tokens=target_tokens,
            max_tokens=max_tokens,
            min_tokens=4,
        )
    )


def _minimal_pdf(text: str = "Searchable PDF document") -> bytes:
    """Build a tiny searchable, single-page PDF without a test-only dependency."""
    safe_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({safe_text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(payload)


def _zip_bytes(entries: list[tuple[str, bytes]], *, compression=zipfile.ZIP_STORED) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return stream.getvalue()


def _eocd_with_declared_entries(data: bytes, count: int) -> bytes:
    mutated = bytearray(data)
    position = mutated.rfind(b"PK\x05\x06")
    assert position >= 0
    struct.pack_into("<H", mutated, position + 8, count)
    struct.pack_into("<H", mutated, position + 10, count)
    return bytes(mutated)


def test_text_and_sql_parsers_preserve_source_locators():
    text = _service().parse_and_chunk("notes.txt", "text/plain", "第一段。\n\n第二段。".encode())
    sql = _service().parse_and_chunk(
        "report.sql",
        "application/sql",
        b"-- paid orders\nSELECT * FROM orders;\nSELECT count(*) FROM users;",
    )

    assert text.document.source_type == "txt"
    assert {block.locator["paragraph"] for block in text.document.blocks} == {1, 2}
    assert sql.document.source_type == "sql"
    assert [block.locator["statement"] for block in sql.document.blocks] == [1, 2]
    assert all(block.block_type == "code" for block in sql.document.blocks)


def test_markdown_parser_preserves_heading_path_and_fenced_code():
    result = _service().parse_and_chunk(
        "metric.md",
        "text/markdown",
        "# 指标口径\n\n## GMV\n已支付金额之和。\n\n```sql\nSELECT sum(amount) FROM orders;\n```".encode(),
    )

    paragraph = next(block for block in result.document.blocks if "已支付" in block.text)
    code = next(block for block in result.document.blocks if block.block_type == "code")
    assert paragraph.section_path == ("指标口径", "GMV")
    assert code.section_path == ("指标口径", "GMV")
    assert code.locator["language"] == "sql"
    assert "SELECT sum(amount)" in code.text


@pytest.mark.parametrize(
    ("filename", "media_type", "payload", "expected_path"),
    [
        ("rules.json", "application/json", json.dumps({"metrics": [{"name": "GMV", "formula": "sum(amount)"}]}).encode(), "$.metrics[0]"),
        ("rules.yaml", "application/yaml", "metrics:\n  - name: GMV\n    formula: sum(amount)\n".encode(), "$.metrics[0]"),
    ],
)
def test_structured_parser_keeps_object_path(filename, media_type, payload, expected_path):
    result = _service().parse_and_chunk(filename, media_type, payload)

    assert any(block.locator["path"] == expected_path for block in result.document.blocks)
    assert any("GMV" in block.text for block in result.document.blocks)


def test_csv_parser_keeps_header_and_row_range_in_chunks():
    result = _service(target_tokens=34, max_tokens=50).parse_and_chunk(
        "orders.csv",
        "text/csv",
        "order_id,amount,status\n1,100,paid\n2,50,cancelled\n".encode(),
    )

    assert result.document.metadata["columns"] == ("order_id", "amount", "status")
    assert all("order_id" in chunk.content for chunk in result.chunks)
    assert all("row_start" in chunk.locator for chunk in result.chunks)


def test_html_parser_preserves_sections_table_and_code():
    result = _service().parse_and_chunk(
        "guide.html",
        "text/html",
        b"<h1>Orders</h1><p>Paid order rules.</p><table><tr><th>status</th></tr><tr><td>paid</td></tr></table><pre>SELECT 1;</pre>",
    )

    assert any(block.section_path == ("Orders",) and block.block_type == "paragraph" for block in result.document.blocks)
    assert any(block.block_type == "table" and "paid" in block.text for block in result.document.blocks)
    assert any(block.block_type == "code" and "SELECT 1" in block.text for block in result.document.blocks)


def test_pdf_parser_extracts_searchable_text_and_reports_page():
    result = _service().parse_and_chunk("searchable.pdf", "application/pdf", _minimal_pdf())

    assert result.parser_name == "pdf"
    assert any("Searchable PDF document" in block.text for block in result.document.blocks)
    assert result.chunks[0].locator["page"] == 1


def test_textless_pdf_reports_ocr_required():
    with pytest.raises(DocumentIngestionError) as exc_info:
        _service().parse_and_chunk("scan.pdf", "application/pdf", _minimal_pdf(""))

    assert exc_info.value.code == "ocr_required"


def test_docx_xlsx_and_pptx_parsers_use_native_structures():
    docx = pytest.importorskip("docx")
    openpyxl = pytest.importorskip("openpyxl")
    pptx = pytest.importorskip("pptx")

    word_stream = BytesIO()
    word = docx.Document()
    word.add_heading("业务定义", level=1)
    word.add_paragraph("GMV 是已支付金额之和。")
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "字段", "含义"
    table.cell(1, 0).text, table.cell(1, 1).text = "amount", "金额"
    word.save(word_stream)

    excel_stream = BytesIO()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["order_id", "amount"])
    sheet.append([1, 100])
    workbook.save(excel_stream)

    slides_stream = BytesIO()
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "GMV"
    slide.placeholders[1].text = "已支付订单金额之和"
    deck.save(slides_stream)

    word_result = _service().parse_and_chunk("rules.docx", None, word_stream.getvalue())
    excel_result = _service().parse_and_chunk("orders.xlsx", None, excel_stream.getvalue())
    slides_result = _service().parse_and_chunk("metrics.pptx", None, slides_stream.getvalue())

    assert any(block.section_path == ("业务定义",) for block in word_result.document.blocks)
    assert any(block.locator.get("sheet") == "订单" for block in excel_result.document.blocks)
    assert any(block.locator.get("slide") == 1 and "已支付" in block.text for block in slides_result.document.blocks)


def test_semantic_long_prose_splits_on_topic_change_and_respects_hard_limit():
    prose = (
        "Apple orchard fruit grows slowly. Apple harvest needs careful work. "
        "Database indexes speed queries. Database transactions protect records. "
        "Database backups preserve important records."
    )
    result = _service(target_tokens=10, max_tokens=16).parse_and_chunk("topics.txt", None, prose.encode())

    assert len(result.chunks) >= 2
    assert all(chunk.token_count <= 16 for chunk in result.chunks)
    assert any("Apple" in chunk.content for chunk in result.chunks)
    assert any("Database" in chunk.content for chunk in result.chunks)


def test_registry_rejects_unsupported_and_signature_mismatch():
    registry = default_parser_registry()

    with pytest.raises(DocumentIngestionError) as unsupported:
        registry.resolve("legacy.doc", "application/msword", b"old office")
    with pytest.raises(DocumentIngestionError) as mismatch:
        registry.resolve("fake.pdf", "application/pdf", b"not a pdf")

    assert unsupported.value.code == "unsupported_format"
    assert mismatch.value.code == "file_type_mismatch"


def test_convenience_entrypoint_returns_immutable_domain_result():
    result = parse_and_chunk_document("note.txt", "text/plain", b"short document")

    assert result.parser_name == "text"
    assert result.document.title == "note"
    assert isinstance(result.chunks, tuple)
    assert result.chunks[0].embedding_content.startswith("文档：note")


@pytest.mark.parametrize(
    ("entries", "limits", "compression", "expected_code"),
    [
        (
            [("[Content_Types].xml", b"x"), ("word/document.xml", b"x")],
            replace(DEFAULT_LIMITS, max_ooxml_members=1),
            zipfile.ZIP_STORED,
            "archive_member_limit_exceeded",
        ),
        (
            [("word/document.xml", b"x" * 11)],
            replace(DEFAULT_LIMITS, max_ooxml_member_bytes=10),
            zipfile.ZIP_STORED,
            "archive_member_too_large",
        ),
        (
            [("a.xml", b"x" * 6), ("b.xml", b"y" * 6)],
            replace(DEFAULT_LIMITS, max_ooxml_member_bytes=10, max_ooxml_uncompressed_bytes=10),
            zipfile.ZIP_STORED,
            "archive_uncompressed_too_large",
        ),
        (
            [("word/document.xml", b"x" * 10_000)],
            replace(DEFAULT_LIMITS, max_ooxml_compression_ratio=2.0),
            zipfile.ZIP_DEFLATED,
            "archive_suspicious_compression",
        ),
        (
            [("../outside.xml", b"x")],
            DEFAULT_LIMITS,
            zipfile.ZIP_STORED,
            "archive_dangerous_path",
        ),
    ],
)
def test_ooxml_archive_limits_are_checked_before_parser(entries, limits, compression, expected_code):
    service = DocumentIngestionService(limits=limits)

    with pytest.raises(DocumentIngestionError) as exc_info:
        service.parse_and_chunk("unsafe.docx", None, _zip_bytes(entries, compression=compression))

    assert exc_info.value.code == expected_code


def test_eocd_member_limit_rejects_before_zipfile_builds_member_objects(monkeypatch):
    payload = _eocd_with_declared_entries(_zip_bytes([("word/document.xml", b"x")]), 2)

    def must_not_construct_zipfile(*args, **kwargs):
        raise AssertionError("ZipFile must not be constructed after EOCD already exceeds the member limit")

    monkeypatch.setattr(limits_module.zipfile, "ZipFile", must_not_construct_zipfile)
    with pytest.raises(DocumentIngestionError) as exc_info:
        validate_ooxml_archive(
            payload,
            parser_name="docx",
            limits=replace(DEFAULT_LIMITS, max_ooxml_members=1),
        )

    assert exc_info.value.code == "archive_member_limit_exceeded"


def test_zip64_sentinel_has_a_stable_preflight_error():
    payload = _eocd_with_declared_entries(_zip_bytes([("word/document.xml", b"x")]), 0xFFFF)

    with pytest.raises(DocumentIngestionError) as exc_info:
        validate_ooxml_archive(payload, parser_name="docx")

    assert exc_info.value.code == "archive_zip64_unsupported"


class StaticParser:
    name = "static"
    extensions = frozenset({"fake"})
    media_types = frozenset({"application/x-static"})

    def __init__(self, blocks: tuple[DocumentBlock, ...]):
        self.blocks = blocks

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        return ParsedDocument("static", "fake", self.blocks)


@pytest.mark.parametrize(
    ("blocks", "limits", "expected_code"),
    [
        (
            (DocumentBlock("paragraph", "one"), DocumentBlock("paragraph", "two")),
            replace(DEFAULT_LIMITS, max_document_blocks=1),
            "document_block_limit_exceeded",
        ),
        (
            (DocumentBlock("paragraph", "12345678"),),
            replace(DEFAULT_LIMITS, max_block_characters=7),
            "document_block_too_large",
        ),
        (
            (DocumentBlock("paragraph", "1234"), DocumentBlock("paragraph", "5678")),
            replace(DEFAULT_LIMITS, max_block_characters=10, max_document_characters=7),
            "document_character_limit_exceeded",
        ),
    ],
)
def test_all_parser_results_are_bounded_before_chunking(blocks, limits, expected_code):
    registry = ParserRegistry()
    registry.register(StaticParser(blocks))
    service = DocumentIngestionService(registry=registry, limits=limits)

    with pytest.raises(DocumentIngestionError) as exc_info:
        service.parse_and_chunk("document.fake", "application/x-static", b"payload")

    assert exc_info.value.code == expected_code


def test_yaml_alias_node_depth_expansion_and_cycle_limits():
    alias_parser = YamlParser(limits=replace(DEFAULT_LIMITS, max_yaml_aliases=1))
    aliases = b"base: &base {value: 1}\nitems: [*base, *base]\n"
    with pytest.raises(DocumentIngestionError) as alias_error:
        alias_parser.parse("aliases.yaml", None, aliases)
    assert alias_error.value.code == "yaml_alias_limit_exceeded"

    node_parser = YamlParser(limits=replace(DEFAULT_LIMITS, max_yaml_nodes=3))
    with pytest.raises(DocumentIngestionError) as node_error:
        node_parser.parse("nodes.yaml", None, b"a: 1\nb: 2\nc: 3\n")
    assert node_error.value.code == "yaml_node_limit_exceeded"

    depth_parser = YamlParser(limits=replace(DEFAULT_LIMITS, max_yaml_depth=2))
    with pytest.raises(DocumentIngestionError) as depth_error:
        depth_parser.parse("depth.yaml", None, b"a:\n  b:\n    c: 1\n")
    assert depth_error.value.code == "yaml_depth_limit_exceeded"

    expansion_parser = YamlParser(limits=replace(DEFAULT_LIMITS, max_yaml_expanded_nodes=12))
    expansion = b"a: &a [x, x]\nb: &b [*a, *a]\nc: &c [*b, *b]\nroot: *c\n"
    with pytest.raises(DocumentIngestionError) as expansion_error:
        expansion_parser.parse("expansion.yaml", None, expansion)
    assert expansion_error.value.code == "yaml_expansion_limit_exceeded"

    cycle_parser = YamlParser()
    with pytest.raises(DocumentIngestionError) as cycle_error:
        cycle_parser.parse("cycle.yaml", None, b"root: &root [*root]\n")
    assert cycle_error.value.code == "yaml_recursive_alias"


def test_bounded_json_serializer_stops_large_container_and_yaml_scalar_alias():
    json_parser = JsonParser(limits=replace(DEFAULT_LIMITS, max_block_characters=64))
    json_payload = json.dumps([{"payload": "x" * 2_000}]).encode()
    with pytest.raises(DocumentIngestionError) as json_error:
        json_parser.parse("large.json", None, json_payload)
    assert json_error.value.code == "document_block_too_large"

    yaml_parser = YamlParser(
        limits=replace(
            DEFAULT_LIMITS,
            max_block_characters=256,
            max_document_characters=120,
        )
    )
    scalar = "x" * 50
    yaml_payload = f'base: &base\n  payload: "{scalar}"\nitems: [*base, *base]\n'.encode()
    with pytest.raises(DocumentIngestionError) as yaml_error:
        yaml_parser.parse("large-alias.yaml", None, yaml_payload)
    assert yaml_error.value.code == "document_character_limit_exceeded"


def test_pdf_block_budget_stops_before_later_page_work(monkeypatch):
    class FakePage:
        images = []

        def __init__(self) -> None:
            self.tables_called = False

        def extract_text(self, **kwargs):
            return "\n\n".join(f"paragraph {index}" for index in range(20))

        def extract_tables(self):
            self.tables_called = True
            return []

    class FakePdf:
        def __init__(self, page) -> None:
            self.pages = [page]

        def close(self) -> None:
            pass

    page = FakePage()
    monkeypatch.setitem(sys.modules, "pdfplumber", SimpleNamespace(open=lambda _: FakePdf(page)))
    parser = default_parser_registry(
        limits=replace(DEFAULT_LIMITS, max_document_blocks=3)
    ).resolve("amplified.pdf", "application/pdf", b"%PDF-fake")

    with pytest.raises(DocumentIngestionError) as exc_info:
        parser.parse("amplified.pdf", "application/pdf", b"%PDF-fake")

    assert exc_info.value.code == "document_block_limit_exceeded"
    assert page.tables_called is False


def test_json_record_and_depth_budgets_stop_during_traversal():
    record_parser = JsonParser(limits=replace(DEFAULT_LIMITS, max_document_blocks=2))
    with pytest.raises(DocumentIngestionError) as record_error:
        record_parser.parse("many.json", None, b'{"a": 1, "b": 2, "c": 3}')
    assert record_error.value.code == "document_block_limit_exceeded"

    depth_parser = JsonParser(limits=replace(DEFAULT_LIMITS, max_structured_depth=3))
    with pytest.raises(DocumentIngestionError) as depth_error:
        depth_parser.parse("deep.json", None, b'{"a": {"b": {"c": {"d": 1}}}}')
    assert depth_error.value.code == "structured_depth_limit_exceeded"


def test_xlsx_block_budget_stops_while_rows_are_streamed():
    openpyxl = pytest.importorskip("openpyxl")
    stream = BytesIO()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["id", "amount"])
    sheet.append([1, 10])
    sheet.append([2, 20])
    workbook.save(stream)

    parser = XlsxParser(limits=replace(DEFAULT_LIMITS, max_document_blocks=1))
    with pytest.raises(DocumentIngestionError) as exc_info:
        parser.parse("rows.xlsx", None, stream.getvalue())

    assert exc_info.value.code == "document_block_limit_exceeded"


@pytest.mark.parametrize(
    ("limits", "expected_code"),
    [
        (replace(DEFAULT_LIMITS, max_tabular_rows=2), "tabular_row_limit_exceeded"),
        (replace(DEFAULT_LIMITS, max_tabular_cells=3), "tabular_cell_limit_exceeded"),
        (replace(DEFAULT_LIMITS, max_tabular_cell_characters=3), "tabular_cell_too_large"),
    ],
)
def test_csv_rows_and_cells_stop_at_incremental_budgets(limits, expected_code):
    parser = CsvParser(limits=limits)

    with pytest.raises(DocumentIngestionError) as exc_info:
        parser.parse("rows.csv", None, b"id,value\n1,abcd\n2,ok\n")

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("limits", "expected_code"),
    [
        (replace(DEFAULT_LIMITS, max_tabular_rows=2), "tabular_row_limit_exceeded"),
        (replace(DEFAULT_LIMITS, max_tabular_cells=3), "tabular_cell_limit_exceeded"),
    ],
)
def test_xlsx_rows_and_cells_stop_at_incremental_budgets(limits, expected_code):
    openpyxl = pytest.importorskip("openpyxl")
    stream = BytesIO()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["id", "value"])
    sheet.append([1, "one"])
    sheet.append([2, "two"])
    workbook.save(stream)

    with pytest.raises(DocumentIngestionError) as exc_info:
        XlsxParser(limits=limits).parse("rows.xlsx", None, stream.getvalue())

    assert exc_info.value.code == expected_code


def test_regex_counter_and_hard_split_bound_continuous_ascii():
    continuous = "a" * 1_000
    counter = RegexTokenCounter()

    assert counter.count(continuous) >= 125
    chunks = split_to_token_limit(continuous, counter, 4)
    assert "".join(chunks) == continuous
    assert all(counter.count(chunk) <= 4 for chunk in chunks)
    assert all(len(chunk) <= 32 for chunk in chunks)


def test_character_guard_still_applies_to_a_counter_that_under_counts_ascii():
    continuous = "z" * 100
    chunks = split_to_token_limit(continuous, WordCounter(), 4)

    assert "".join(chunks) == continuous
    assert all(len(chunk) <= 32 for chunk in chunks)


def test_semantic_embeddings_are_batched_and_sentence_count_is_capped():
    text = " ".join(f"Apple topic {index}." for index in range(10))
    recording = RecordingEmbedding()
    batched_limits = replace(DEFAULT_LIMITS, max_semantic_sentences=20, semantic_embedding_batch_size=3)
    pipeline = ChunkingPipeline(
        token_counter=WordCounter(),
        embedding_client=recording,
        target_tokens=6,
        max_tokens=14,
        min_tokens=2,
        limits=batched_limits,
    )
    pipeline.chunk(ParsedDocument("topics", "txt", (DocumentBlock("paragraph", text),)))

    assert recording.batch_sizes
    assert max(recording.batch_sizes) <= 3

    capped = RecordingEmbedding()
    capped_pipeline = ChunkingPipeline(
        token_counter=WordCounter(),
        embedding_client=capped,
        target_tokens=6,
        max_tokens=14,
        min_tokens=2,
        limits=replace(DEFAULT_LIMITS, max_semantic_sentences=3, semantic_embedding_batch_size=2),
    )
    chunks = capped_pipeline.chunk(ParsedDocument("topics", "txt", (DocumentBlock("paragraph", text),)))

    assert capped.batch_sizes == []
    assert chunks
    assert all(chunk.token_count <= 14 for chunk in chunks)
