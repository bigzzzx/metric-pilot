"""Resource-bounded PDF extraction executed outside the API process.

pdfminer/pdfplumber construct a complete page layout before returning text or
tables.  Output quotas alone therefore cannot protect the API process from a
malicious content stream.  This module performs cheap page-tree/resource
checks first and runs the unavoidable layout construction in a short-lived,
observable subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import multiprocessing
import os
import time
from typing import Any, Callable, Iterable

from backend.knowledge.limits import IngestionLimits, ParsedContentBudget, TabularBudget
from backend.knowledge.models import DocumentBlock, DocumentIngestionError
from backend.knowledge.parsers.table_utils import cell_text, markdown_row


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    blocks: tuple[DocumentBlock, ...]
    warnings: tuple[str, ...]
    page_count: int
    image_count: int
    extracted_characters: int


PdfOpener = Callable[[BytesIO], Any]
PageIteratorFactory = Callable[[Any], Iterable[Any]]
PageFactory = Callable[[Any, Any, int, float], Any]


def extract_pdf_isolated(
    data: bytes,
    *,
    max_pages: int,
    ocr_text_threshold: int,
    limits: IngestionLimits,
) -> PdfExtraction:
    """Extract one PDF in a killable worker with time and memory supervision."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_pdf_worker_main,
        args=(sender, data, max_pages, ocr_text_threshold, limits),
        name="data-agent-pdf-parser",
        daemon=True,
    )
    try:
        process.start()
    except (AssertionError, OSError, RuntimeError) as exc:
        receiver.close()
        sender.close()
        raise DocumentIngestionError(
            "pdf_isolation_unavailable",
            "当前运行环境无法启动安全的 PDF 解析工作进程。",
            details={"error_type": type(exc).__name__},
        ) from exc
    sender.close()

    deadline = time.monotonic() + max(0.1, float(limits.max_pdf_extraction_seconds))
    payload: tuple[str, Any] | None = None
    failure: DocumentIngestionError | None = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = DocumentIngestionError(
                    "pdf_extraction_timeout",
                    "PDF 解析超过安全时间限制，已终止解析。",
                    details={"limit_seconds": limits.max_pdf_extraction_seconds},
                )
                break
            if receiver.poll(min(0.05, remaining)):
                try:
                    payload = receiver.recv()
                except (EOFError, OSError):
                    payload = None
                break
            if not process.is_alive():
                break
            resident_bytes = _process_resident_bytes(process.pid)
            if resident_bytes is not None and resident_bytes > limits.max_pdf_worker_memory_bytes:
                failure = DocumentIngestionError(
                    "pdf_worker_memory_limit_exceeded",
                    "PDF 解析占用内存超过安全限制，已终止解析。",
                    details={"actual": resident_bytes, "limit": limits.max_pdf_worker_memory_bytes},
                )
                break
    finally:
        receiver.close()

    if failure is not None:
        _stop_process(process)
        raise failure

    process.join(timeout=1.0)
    if process.is_alive():
        _stop_process(process)
    if payload is None:
        raise DocumentIngestionError(
            "pdf_extraction_failed",
            "PDF 安全解析工作进程异常退出。",
            details={"exit_code": process.exitcode},
        )

    status, value = payload
    if status == "error" and isinstance(value, dict):
        raise DocumentIngestionError(
            str(value.get("code") or "pdf_extraction_failed"),
            str(value.get("message") or "PDF 解析失败。"),
            details=value.get("details") if isinstance(value.get("details"), dict) else {},
        )
    if status != "ok" or not isinstance(value, PdfExtraction):
        raise DocumentIngestionError("pdf_extraction_failed", "PDF 安全解析工作进程返回了无效结果。")
    return value


def extract_pdf_in_process(
    data: bytes,
    *,
    max_pages: int,
    ocr_text_threshold: int,
    limits: IngestionLimits,
    open_pdf: PdfOpener | None = None,
    page_iterator_factory: PageIteratorFactory | None = None,
    page_factory: PageFactory | None = None,
) -> PdfExtraction:
    """Worker implementation; injectable dependencies keep boundary tests cheap."""
    if open_pdf is None:
        import pdfplumber

        open_pdf = pdfplumber.open
    if page_iterator_factory is None:
        from pdfminer.pdfpage import PDFPage

        page_iterator_factory = PDFPage.create_pages
    if page_factory is None:
        from pdfplumber.page import Page

        page_factory = lambda pdf, raw, number, doctop: Page(  # noqa: E731
            pdf,
            raw,
            page_number=number,
            initial_doctop=doctop,
        )

    try:
        pdf = open_pdf(BytesIO(data))
    except Exception as exc:
        lowered = str(exc).lower()
        if "password" in lowered or "encrypt" in lowered:
            raise DocumentIngestionError(
                "encrypted_pdf",
                "PDF 已加密，请移除密码后重新上传。",
                details={"parser": "pdf"},
            ) from exc
        raise DocumentIngestionError(
            "parse_failed",
            "PDF 文件无法读取或已损坏。",
            details={"parser": "pdf", "error_type": type(exc).__name__},
        ) from exc

    blocks: list[DocumentBlock] = []
    warnings: list[str] = []
    content_budget = ParsedContentBudget(limits)
    table_budget = TabularBudget(limits)
    extracted_characters = 0
    image_count = 0
    page_count = 0
    initial_doctop = 0.0
    try:
        declared_pages = _declared_page_count(pdf)
        if declared_pages is not None and declared_pages > max_pages:
            raise _page_limit(declared_pages, max_pages)
        _validate_xref_object_count(pdf, limits=limits)

        try:
            raw_pages = iter(page_iterator_factory(pdf.doc))
            for page_number, raw_page in enumerate(raw_pages, start=1):
                if page_number > max_pages:
                    raise _page_limit(page_number, max_pages)
                page_count = page_number
                width, height, declared_images = _validate_raw_page(raw_page, page_number=page_number, limits=limits)
                image_count += declared_images
                page = page_factory(pdf, raw_page, page_number, initial_doctop)
                try:
                    text = _extract_page_text(page, page_number, warnings, limits)
                    extracted_characters += _non_whitespace_count(text)
                    for paragraph_index, paragraph in enumerate(_paragraphs(text), start=1):
                        content_budget.append(
                            blocks,
                            DocumentBlock(
                                "paragraph",
                                paragraph,
                                locator={"page": page_number, "paragraph": paragraph_index},
                            ),
                        )

                    _validate_layout_objects(page, page_number=page_number, limits=limits)
                    extracted_characters += _extract_page_tables(
                        page,
                        page_number=page_number,
                        blocks=blocks,
                        warnings=warnings,
                        content_budget=content_budget,
                        table_budget=table_budget,
                        limits=limits,
                    )
                finally:
                    close_page = getattr(page, "close", None)
                    if callable(close_page):
                        close_page()
                initial_doctop += height
        except DocumentIngestionError:
            raise
        except (MemoryError, RecursionError):
            raise
        except Exception as exc:
            raise DocumentIngestionError(
                "parse_failed",
                "PDF 页树或页面内容无法读取。",
                details={"parser": "pdf", "page": page_count + 1, "error_type": type(exc).__name__},
            ) from exc

        if declared_pages is not None and declared_pages != page_count:
            raise DocumentIngestionError(
                "pdf_page_count_mismatch",
                "PDF 页树声明的页数与实际页数不一致。",
                details={"declared": declared_pages, "actual": page_count},
            )
    finally:
        close_pdf = getattr(pdf, "close", None)
        if callable(close_pdf):
            close_pdf()

    if extracted_characters < ocr_text_threshold:
        raise DocumentIngestionError(
            "ocr_required",
            "PDF 中没有足够的可搜索文字，可能是扫描件，需要先执行 OCR。",
            details={"page_count": page_count, "image_count": image_count, "parser": "pdf"},
        )
    if not blocks:
        raise DocumentIngestionError(
            "no_readable_content",
            "文档中没有可读取的文本或表格内容。",
            details={"parser": "pdf"},
        )
    return PdfExtraction(tuple(blocks), tuple(warnings), page_count, image_count, extracted_characters)


def _pdf_worker_main(sender, data: bytes, max_pages: int, ocr_text_threshold: int, limits: IngestionLimits) -> None:
    _apply_child_memory_limit(limits.max_pdf_worker_memory_bytes)
    try:
        result = extract_pdf_in_process(
            data,
            max_pages=max_pages,
            ocr_text_threshold=ocr_text_threshold,
            limits=limits,
        )
        sender.send(("ok", result))
    except DocumentIngestionError as exc:
        sender.send(("error", exc.as_dict()))
    except MemoryError:
        sender.send(
            (
                "error",
                {
                    "code": "pdf_worker_memory_limit_exceeded",
                    "message": "PDF 解析占用内存超过安全限制，已终止解析。",
                    "details": {"limit": limits.max_pdf_worker_memory_bytes},
                },
            )
        )
    except BaseException as exc:  # worker must return only a safe error envelope
        sender.send(
            (
                "error",
                {
                    "code": "pdf_extraction_failed",
                    "message": "PDF 安全解析工作进程异常退出。",
                    "details": {"error_type": type(exc).__name__},
                },
            )
        )
    finally:
        sender.close()


def _declared_page_count(pdf: Any) -> int | None:
    try:
        from pdfminer.pdftypes import dict_value, resolve1

        catalog = dict_value(pdf.doc.catalog)
        pages = dict_value(resolve1(catalog.get("Pages")))
        count = resolve1(pages.get("Count"))
    except Exception as exc:
        raise DocumentIngestionError(
            "pdf_invalid_page_tree",
            "PDF 页树声明无效。",
            details={"error_type": type(exc).__name__},
        ) from exc
    if count is None:
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise DocumentIngestionError(
            "pdf_invalid_page_tree",
            "PDF 页树中的页数声明无效。",
            details={"declared_count": repr(count)[:100]},
        )
    return count


def _validate_xref_object_count(pdf: Any, *, limits: IngestionLimits) -> None:
    count = 0
    try:
        for xref in getattr(pdf.doc, "xrefs", ()) or ():
            for _object_id in xref.get_objids():
                count += 1
                if count > limits.max_pdf_xref_objects:
                    raise DocumentIngestionError(
                        "pdf_object_limit_exceeded",
                        "PDF 声明的对象数量超过安全限制。",
                        details={"actual": count, "limit": limits.max_pdf_xref_objects},
                    )
    except DocumentIngestionError:
        raise
    except Exception as exc:
        raise DocumentIngestionError(
            "pdf_invalid_xref",
            "PDF 交叉引用表无法安全读取。",
            details={"error_type": type(exc).__name__},
        ) from exc


def _validate_raw_page(raw_page: Any, *, page_number: int, limits: IngestionLimits) -> tuple[float, float, int]:
    try:
        x0, y0, x1, y1 = (float(value) for value in raw_page.mediabox)
    except Exception as exc:
        raise DocumentIngestionError(
            "pdf_invalid_page_geometry",
            "PDF 页面尺寸声明无效。",
            details={"page": page_number},
        ) from exc
    try:
        user_unit = float(getattr(raw_page, "attrs", {}).get("UserUnit", 1) or 1)
    except (TypeError, ValueError) as exc:
        raise DocumentIngestionError(
            "pdf_invalid_page_geometry",
            "PDF 页面缩放单位声明无效。",
            details={"page": page_number},
        ) from exc
    width = abs(x1 - x0) * user_unit
    height = abs(y1 - y0) * user_unit
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise DocumentIngestionError(
            "pdf_invalid_page_geometry",
            "PDF 页面尺寸声明无效。",
            details={"page": page_number, "width": width, "height": height},
        )
    if (
        width > limits.max_pdf_page_width_points
        or height > limits.max_pdf_page_height_points
        or width * height > limits.max_pdf_page_area_points
    ):
        raise DocumentIngestionError(
            "pdf_page_dimension_limit_exceeded",
            "PDF 页面尺寸超过安全限制。",
            details={
                "page": page_number,
                "width": width,
                "height": height,
                "max_width": limits.max_pdf_page_width_points,
                "max_height": limits.max_pdf_page_height_points,
                "max_area": limits.max_pdf_page_area_points,
            },
        )

    contents = tuple(getattr(raw_page, "contents", ()) or ())
    if len(contents) > limits.max_pdf_content_streams_per_page:
        raise DocumentIngestionError(
            "pdf_content_stream_limit_exceeded",
            "PDF 单页包含过多内容流。",
            details={"page": page_number, "actual": len(contents), "limit": limits.max_pdf_content_streams_per_page},
        )
    compressed_bytes = 0
    try:
        from pdfminer.pdftypes import resolve1

        for stream_ref in contents:
            stream = resolve1(stream_ref)
            raw_data = getattr(stream, "rawdata", None)
            actual_size = len(raw_data) if isinstance(raw_data, (bytes, bytearray, memoryview)) else 0
            attributes = getattr(stream, "attrs", {})
            declared_size = resolve1(attributes.get("Length", actual_size))
            if isinstance(declared_size, bool) or not isinstance(declared_size, (int, float)) or declared_size < 0:
                declared_size = actual_size
            filters = resolve1(attributes.get("Filter", ()))
            filter_count = len(filters) if isinstance(filters, (list, tuple)) else int(filters is not None and filters != ())
            if filter_count > limits.max_pdf_filters_per_stream:
                raise DocumentIngestionError(
                    "pdf_filter_limit_exceeded",
                    "PDF 内容流包含过长的解码过滤器链。",
                    details={"page": page_number, "actual": filter_count, "limit": limits.max_pdf_filters_per_stream},
                )
            compressed_bytes += max(actual_size, int(declared_size))
            if compressed_bytes > limits.max_pdf_content_stream_bytes_per_page:
                raise DocumentIngestionError(
                    "pdf_content_stream_too_large",
                    "PDF 单页内容流体积超过安全限制。",
                    details={
                        "page": page_number,
                        "actual": compressed_bytes,
                        "limit": limits.max_pdf_content_stream_bytes_per_page,
                    },
                )
    except DocumentIngestionError:
        raise
    except Exception as exc:
        raise DocumentIngestionError(
            "pdf_invalid_content_stream",
            "PDF 页面内容流声明无效。",
            details={"page": page_number, "error_type": type(exc).__name__},
        ) from exc

    resource_count = _bounded_container_size(
        getattr(raw_page, "resources", {}) or {},
        limit=limits.max_pdf_resource_objects_per_page,
    )
    if resource_count > limits.max_pdf_resource_objects_per_page:
        raise DocumentIngestionError(
            "pdf_resource_limit_exceeded",
            "PDF 单页资源对象数量超过安全限制。",
            details={"page": page_number, "actual": resource_count, "limit": limits.max_pdf_resource_objects_per_page},
        )
    image_count = _validate_declared_page_resources(raw_page, page_number=page_number, limits=limits)
    _validate_annotations(raw_page, page_number=page_number, limits=limits)
    return width, height, image_count


def _bounded_container_size(value: Any, *, limit: int) -> int:
    stack = [value]
    seen: set[int] = set()
    count = 0
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            count += len(current)
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            count += len(current)
            stack.extend(current)
        else:
            count += 1
        if count > limit:
            return count
    return count


def _validate_declared_page_resources(raw_page: Any, *, page_number: int, limits: IngestionLimits) -> int:
    try:
        from pdfminer.psparser import literal_name
        from pdfminer.pdftypes import dict_value, resolve1

        resources = dict_value(getattr(raw_page, "resources", {}) or {})
        raw_xobjects = resources.get("XObject")
        if raw_xobjects is None:
            return 0
        xobjects = dict_value(resolve1(raw_xobjects))
        if len(xobjects) > limits.max_pdf_resource_objects_per_page:
            raise DocumentIngestionError(
                "pdf_resource_limit_exceeded",
                "PDF 单页 XObject 数量超过安全限制。",
                details={"page": page_number, "actual": len(xobjects), "limit": limits.max_pdf_resource_objects_per_page},
            )
        images = 0
        for value in xobjects.values():
            resolved = resolve1(value)
            subtype = getattr(resolved, "attrs", {}).get("Subtype")
            if subtype is not None and literal_name(resolve1(subtype)) == "Image":
                images += 1
        return images
    except DocumentIngestionError:
        raise
    except Exception as exc:
        raise DocumentIngestionError(
            "pdf_invalid_resources",
            "PDF 页面资源声明无效。",
            details={"page": page_number, "error_type": type(exc).__name__},
        ) from exc


def _validate_annotations(raw_page: Any, *, page_number: int, limits: IngestionLimits) -> None:
    annotations = getattr(raw_page, "annots", None)
    if annotations is None:
        return
    try:
        from pdfminer.pdftypes import resolve1

        resolved = resolve1(annotations)
        count = len(resolved) if isinstance(resolved, (list, tuple)) else 1
    except Exception as exc:
        raise DocumentIngestionError(
            "pdf_invalid_annotations",
            "PDF 页面批注声明无效。",
            details={"page": page_number, "error_type": type(exc).__name__},
        ) from exc
    if count > limits.max_pdf_annotations_per_page:
        raise DocumentIngestionError(
            "pdf_annotation_limit_exceeded",
            "PDF 单页批注数量超过安全限制。",
            details={"page": page_number, "actual": count, "limit": limits.max_pdf_annotations_per_page},
        )


def _extract_page_text(page: Any, page_number: int, warnings: list[str], limits: IngestionLimits) -> str:
    try:
        text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
    except (MemoryError, RecursionError):
        raise
    except Exception as exc:
        warnings.append(f"第 {page_number} 页正文提取失败：{type(exc).__name__}")
        return ""
    text_limit = min(limits.max_pdf_page_text_characters, limits.max_document_characters)
    if len(text) > text_limit:
        raise DocumentIngestionError(
            "pdf_page_text_limit_exceeded",
            "PDF 单页提取文本超过安全限制。",
            details={"page": page_number, "actual": len(text), "limit": text_limit},
        )
    return text.strip()


def _validate_layout_objects(page: Any, *, page_number: int, limits: IngestionLimits) -> None:
    objects = getattr(page, "_objects", None)
    if not isinstance(objects, dict):
        return
    object_count = 0
    for values in objects.values():
        object_count += len(values) if isinstance(values, (list, tuple)) else 1
        if object_count > limits.max_pdf_layout_objects_per_page:
            raise DocumentIngestionError(
                "pdf_layout_object_limit_exceeded",
                "PDF 单页布局对象数量超过安全限制。",
                details={"page": page_number, "actual": object_count, "limit": limits.max_pdf_layout_objects_per_page},
            )


def _extract_page_tables(
    page: Any,
    *,
    page_number: int,
    blocks: list[DocumentBlock],
    warnings: list[str],
    content_budget: ParsedContentBudget,
    table_budget: TabularBudget,
    limits: IngestionLimits,
) -> int:
    try:
        find_tables = getattr(page, "find_tables", None)
        if callable(find_tables):
            found_tables = find_tables() or []
            if len(found_tables) > limits.max_pdf_tables_per_page:
                raise DocumentIngestionError(
                    "pdf_table_limit_exceeded",
                    "PDF 单页识别出的表格数量超过安全限制。",
                    details={"page": page_number, "actual": len(found_tables), "limit": limits.max_pdf_tables_per_page},
                )
            tables = []
            for table_index, table in enumerate(found_tables, start=1):
                cells = getattr(table, "cells", ()) or ()
                remaining_cells = limits.max_tabular_cells - table_budget.cell_count
                if len(cells) > remaining_cells:
                    raise DocumentIngestionError(
                        "tabular_cell_limit_exceeded",
                        "表格单元格总数超过安全限制。",
                        details={
                            "source": f"page:{page_number}:table:{table_index}",
                            "actual": table_budget.cell_count + len(cells),
                            "limit": limits.max_tabular_cells,
                        },
                    )
                tables.append(table.extract() or [])
        else:  # injectable compatibility path; real pdfplumber uses find_tables
            tables = page.extract_tables() or []
    except DocumentIngestionError:
        raise
    except (MemoryError, RecursionError):
        raise
    except Exception as exc:
        warnings.append(f"第 {page_number} 页表格提取失败：{type(exc).__name__}")
        return 0

    extracted_characters = 0
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
            content_budget.append(
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
            content_budget.append(
                blocks,
                DocumentBlock(
                    "record",
                    value,
                    locator={"page": page_number, "table": table_index, "row_start": 1, "row_end": 1},
                ),
            )
    return extracted_characters


def _paragraphs(text: str):
    import re

    start = 0
    for boundary in re.finditer(r"\n\s*\n|(?<=\.)\s*\n", text):
        part = text[start : boundary.start()].strip()
        if part:
            yield part
        start = boundary.end()
    tail = text[start:].strip()
    if tail:
        yield tail


def _non_whitespace_count(text: str) -> int:
    return sum(1 for character in text if not character.isspace())


def _page_limit(actual: int, limit: int) -> DocumentIngestionError:
    return DocumentIngestionError(
        "page_limit_exceeded",
        f"PDF 页数超过 {limit} 页限制。",
        details={"page_count": actual, "max_pages": limit},
    )


def _apply_child_memory_limit(limit: int) -> None:
    if os.name == "nt":
        return
    try:
        import resource

        current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
        hard = limit if current_hard in {-1, resource.RLIM_INFINITY} else min(limit, current_hard)
        soft = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
    except (ImportError, OSError, ValueError):
        # Parent-side RSS supervision still applies where the platform exposes it.
        return


def _process_resident_bytes(pid: int | None) -> int | None:
    if not pid:
        return None
    if os.name == "nt":
        return _windows_process_resident_bytes(pid)
    status_path = f"/proc/{pid}/status"
    try:
        with open(status_path, "r", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _windows_process_resident_bytes(pid: int) -> int | None:
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            return None
        try:
            counters = ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize)
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _stop_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=1.0)
