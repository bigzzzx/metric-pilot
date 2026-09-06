"""Limited, sentence-level context inheritance between neighboring prose chunks."""

from __future__ import annotations

from backend.knowledge.chunking.semantic import split_sentences
from backend.knowledge.chunking.token_budget import TokenCounter


def with_previous_sentence(
    current: str,
    previous: str | None,
    *,
    counter: TokenCounter,
    max_content_tokens: int,
    max_overlap_tokens: int = 48,
) -> str:
    if not previous:
        return current
    sentences = split_sentences(previous)
    if not sentences:
        return current
    overlap = sentences[-1]
    if overlap in current or counter.count(overlap) > max_overlap_tokens:
        return current
    candidate = f"{overlap}\n{current}".strip()
    return candidate if counter.count(candidate) <= max_content_tokens else current
