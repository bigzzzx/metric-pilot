"""OCR extension boundary; no concrete OCR engine is enabled in v1."""

from backend.knowledge.ocr.base import OcrProvider
from backend.knowledge.ocr.disabled import DisabledOcrProvider

__all__ = ["DisabledOcrProvider", "OcrProvider"]
