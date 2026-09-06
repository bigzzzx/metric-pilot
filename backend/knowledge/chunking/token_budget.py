"""Token counting and hard-limit splitting utilities."""

from __future__ import annotations

import re
from typing import Protocol

from backend.knowledge.models import dependency_missing


_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]{1,8}|[^\w\s]", flags=re.UNICODE)
_SPLIT_PATTERN = re.compile(r"(?<=[。！？；.!?;])\s*|\n+")
DEFAULT_MAX_CHARACTERS_PER_TOKEN = 8


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...

    def truncate(self, text: str, max_tokens: int) -> str: ...


class RegexTokenCounter:
    """Conservative local fallback when the exact BGE tokenizer is unavailable.

    Chinese characters and punctuation each count as one token; long contiguous
    ASCII text is split into eight-character units. It intentionally overestimates common BGE tokenization
    rather than silently sending overlong input to the model.
    """

    def count(self, text: str) -> int:
        return len(_TOKEN_PATTERN.findall(text))

    def truncate(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        matches = list(_TOKEN_PATTERN.finditer(text))
        if len(matches) <= max_tokens:
            return text
        return text[: matches[max_tokens - 1].end()].rstrip()


class BgeTokenCounter:
    """Exact token counter backed by a locally available Hugging Face tokenizer."""

    def __init__(self, tokenizer) -> None:
        self._tokenizer = tokenizer

    @classmethod
    def from_pretrained(cls, model_path: str, *, local_files_only: bool = True) -> "BgeTokenCounter":
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:  # pragma: no cover - optional local optimization
            raise dependency_missing("transformers", "BGE tokenizer") from exc
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=local_files_only)
        return cls(tokenizer)

    def count(self, text: str) -> int:
        return len(self._tokenizer.encode(text, add_special_tokens=False))

    def truncate(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        try:
            encoded = self._tokenizer(
                text,
                add_special_tokens=False,
                return_offsets_mapping=True,
                truncation=True,
                max_length=max_tokens,
            )
            offsets = encoded.get("offset_mapping") or []
            if offsets:
                return text[: int(offsets[-1][1])].rstrip()
        except (TypeError, NotImplementedError):
            pass
        # Slow tokenizers may not expose offsets. Binary search preserves an
        # exact prefix of the source instead of slicing by decoded text length.
        lower, upper = 0, len(text)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            if self.count(text[:middle]) <= max_tokens:
                lower = middle
            else:
                upper = middle - 1
        return text[:lower].rstrip()


def split_to_token_limit(
    text: str,
    counter: TokenCounter,
    max_tokens: int,
    *,
    max_characters_per_token: int = DEFAULT_MAX_CHARACTERS_PER_TOKEN,
) -> list[str]:
    """Split text at sentence/line boundaries, with a final token-hard fallback."""
    text = text.strip()
    if not text:
        return []
    max_characters = max_tokens * max_characters_per_token
    if counter.count(text) <= max_tokens and len(text) <= max_characters:
        return [text]

    units = [unit.strip() for unit in _SPLIT_PATTERN.split(text) if unit.strip()]
    if len(units) <= 1:
        return _hard_slices(text, counter, max_tokens, max_characters=max_characters)

    pieces: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n{unit}".strip() if current else unit
        if current and (counter.count(candidate) > max_tokens or len(candidate) > max_characters):
            pieces.extend(_hard_slices(current, counter, max_tokens, max_characters=max_characters))
            current = unit
        else:
            current = candidate
    if current:
        pieces.extend(_hard_slices(current, counter, max_tokens, max_characters=max_characters))
    return [piece for piece in pieces if piece]


def _hard_slices(text: str, counter: TokenCounter, max_tokens: int, *, max_characters: int) -> list[str]:
    result: list[str] = []
    remaining = text.strip()
    while remaining:
        if counter.count(remaining) <= max_tokens and len(remaining) <= max_characters:
            result.append(remaining)
            break
        piece = counter.truncate(remaining, max_tokens).strip()
        if len(piece) > max_characters:
            piece = piece[:max_characters].rstrip()
        if not piece:
            # A defensive fallback for custom counters with a broken truncate.
            piece = remaining[:1]
        result.append(piece)
        remaining = remaining[len(piece):].lstrip()
    return result
