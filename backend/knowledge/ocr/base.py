"""Protocol implemented by future local or cloud OCR adapters."""

from __future__ import annotations

from typing import Protocol

from backend.knowledge.models import DocumentBlock


class OcrProvider(Protocol):
    def recognize_page(self, image: bytes, *, page_number: int) -> tuple[DocumentBlock, ...]: ...
