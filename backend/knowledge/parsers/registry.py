"""MIME, extension, and signature based parser dispatch."""

from __future__ import annotations

from pathlib import Path

from backend.knowledge.limits import DEFAULT_LIMITS, IngestionLimits
from backend.knowledge.models import DocumentIngestionError
from backend.knowledge.parsers.base import DocumentParser


LEGACY_OFFICE = {"doc", "xls", "ppt"}
PDF_SIGNATURE = b"%PDF-"
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class ParserRegistry:
    def __init__(self) -> None:
        self._by_extension: dict[str, DocumentParser] = {}
        self._by_media_type: dict[str, DocumentParser] = {}

    def register(self, parser: DocumentParser) -> None:
        for extension in parser.extensions:
            self._by_extension[extension.lower().lstrip(".")] = parser
        for media_type in parser.media_types:
            self._by_media_type[media_type.lower().partition(";")[0].strip()] = parser

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_extension))

    def resolve(self, filename: str, media_type: str | None, data: bytes) -> DocumentParser:
        extension = Path(filename).suffix.lower().lstrip(".")
        normalized_media = (media_type or "").lower().partition(";")[0].strip()
        if extension in LEGACY_OFFICE:
            raise DocumentIngestionError(
                "unsupported_format",
                f"暂不支持 .{extension} 旧版 Office 文件，请先转换为现代格式。",
                details={"extension": extension},
            )

        parser = self._by_extension.get(extension) or self._by_media_type.get(normalized_media)
        if parser is None:
            raise DocumentIngestionError(
                "unsupported_format",
                f"不支持该文件格式。支持的扩展名：{', '.join(self.supported_extensions)}。",
                details={"extension": extension, "media_type": normalized_media},
            )

        if parser.name == "pdf" and not data.startswith(PDF_SIGNATURE):
            raise self._signature_mismatch(extension)
        if parser.name in {"docx", "xlsx", "pptx"} and not data.startswith(ZIP_SIGNATURES):
            raise self._signature_mismatch(extension)
        if data.startswith(PDF_SIGNATURE) and extension and extension != "pdf":
            raise self._signature_mismatch(extension)
        return parser

    @staticmethod
    def _signature_mismatch(extension: str) -> DocumentIngestionError:
        return DocumentIngestionError(
            "file_type_mismatch",
            "文件内容与扩展名不一致，请检查文件是否损坏或被错误改名。",
            details={"extension": extension},
        )


_DEFAULT_REGISTRY: ParserRegistry | None = None


def default_parser_registry(*, limits: IngestionLimits = DEFAULT_LIMITS) -> ParserRegistry:
    global _DEFAULT_REGISTRY
    use_shared = limits == DEFAULT_LIMITS
    if use_shared and _DEFAULT_REGISTRY is not None:
        return _DEFAULT_REGISTRY

    from backend.knowledge.parsers.html import HtmlParser
    from backend.knowledge.parsers.markdown import MarkdownParser
    from backend.knowledge.parsers.pdf import PdfParser
    from backend.knowledge.parsers.presentation import PresentationParser
    from backend.knowledge.parsers.structured import JsonParser, YamlParser
    from backend.knowledge.parsers.tabular import CsvParser, XlsxParser
    from backend.knowledge.parsers.text import SqlParser, TextParser
    from backend.knowledge.parsers.word import WordParser

    registry = ParserRegistry()
    for parser in (
        TextParser(),
        SqlParser(),
        MarkdownParser(),
        HtmlParser(),
        JsonParser(limits=limits),
        YamlParser(limits=limits),
        CsvParser(limits=limits),
        XlsxParser(limits=limits),
        PdfParser(limits=limits),
        WordParser(limits=limits),
        PresentationParser(limits=limits),
    ):
        registry.register(parser)
    if use_shared:
        _DEFAULT_REGISTRY = registry
    return registry
