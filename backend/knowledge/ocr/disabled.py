"""Explicit no-OCR implementation used by the first release."""

from __future__ import annotations

from backend.knowledge.models import DocumentBlock, DocumentIngestionError


class DisabledOcrProvider:
    def recognize_page(self, image: bytes, *, page_number: int) -> tuple[DocumentBlock, ...]:
        raise DocumentIngestionError(
            "ocr_required",
            "该 PDF 页面需要 OCR，但当前部署尚未配置 OCR Provider。",
            details={"page": page_number},
        )
