"""Convert normalized blocks into structure-preserving intermediate segments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from backend.knowledge.chunking.semantic import SemanticSplitter
from backend.knowledge.chunking.token_budget import TokenCounter, split_to_token_limit
from backend.knowledge.models import DocumentBlock, ParsedDocument


@dataclass(frozen=True, slots=True)
class StructuralSegment:
    content: str
    section_path: tuple[str, ...]
    locators: tuple[Mapping[str, Any], ...]
    block_types: tuple[str, ...]


PROSE_TYPES = {"paragraph", "list"}


def build_segments(
    document: ParsedDocument,
    *,
    counter: TokenCounter,
    semantic_splitter: SemanticSplitter,
    target_tokens: int,
    max_tokens: int,
    max_characters_per_token: int = 8,
) -> list[StructuralSegment]:
    raw: list[StructuralSegment] = []
    headings: list[DocumentBlock] = []
    for block in document.blocks:
        if block.block_type == "heading":
            headings.append(block)
            continue
        if not block.text:
            continue
        if block.block_type in PROSE_TYPES:
            pieces = semantic_splitter.split(block.text)
        elif block.block_type == "table":
            pieces = split_table_to_limit(
                block.text,
                counter,
                max_tokens,
                max_characters_per_token=max_characters_per_token,
            )
        else:
            pieces = split_to_token_limit(
                block.text,
                counter,
                max_tokens,
                max_characters_per_token=max_characters_per_token,
            )
        for part, piece in enumerate(pieces, start=1):
            locator = dict(block.locator)
            if len(pieces) > 1:
                locator.update({"part": part, "part_count": len(pieces)})
            raw.append(StructuralSegment(piece, block.section_path, (locator,), (block.block_type,)))

    if not raw and headings:
        heading = headings[-1]
        raw.append(StructuralSegment(heading.text, heading.section_path, (heading.locator,), ("heading",)))
    return merge_small_adjacent(raw, counter=counter, target_tokens=target_tokens)


def merge_small_adjacent(
    segments: Iterable[StructuralSegment],
    *,
    counter: TokenCounter,
    target_tokens: int,
) -> list[StructuralSegment]:
    merged: list[StructuralSegment] = []
    for segment in segments:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        if previous.section_path != segment.section_path:
            merged.append(segment)
            continue
        if set(previous.block_types) == {"table"} and set(segment.block_types) == {"table"}:
            candidate = merge_markdown_tables(previous.content, segment.content)
        elif set(previous.block_types).issubset(PROSE_TYPES) and set(segment.block_types).issubset(PROSE_TYPES):
            candidate = f"{previous.content}\n\n{segment.content}".strip()
        else:
            merged.append(segment)
            continue
        if candidate is None or counter.count(candidate) > target_tokens:
            merged.append(segment)
            continue
        merged[-1] = StructuralSegment(
            candidate,
            previous.section_path,
            (*previous.locators, *segment.locators),
            tuple(dict.fromkeys((*previous.block_types, *segment.block_types))),
        )
    return merged


def split_table_to_limit(
    text: str,
    counter: TokenCounter,
    max_tokens: int,
    *,
    max_characters_per_token: int = 8,
) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if counter.count(text) <= max_tokens:
        return [text.strip()]
    if len(lines) < 3 or not _looks_like_separator(lines[1]):
        return split_to_token_limit(
            text,
            counter,
            max_tokens,
            max_characters_per_token=max_characters_per_token,
        )
    header = lines[:2]
    rows = lines[2:]
    chunks: list[str] = []
    current: list[str] = []
    for row in rows:
        candidate = "\n".join([*header, *current, row])
        if current and counter.count(candidate) > max_tokens:
            chunks.append("\n".join([*header, *current]))
            current = [row]
        elif not current and counter.count(candidate) > max_tokens:
            # A single pathological row still passes through the hard guard.
            chunks.extend(
                split_to_token_limit(
                    candidate,
                    counter,
                    max_tokens,
                    max_characters_per_token=max_characters_per_token,
                )
            )
        else:
            current.append(row)
    if current:
        chunks.append("\n".join([*header, *current]))
    return chunks


def merge_markdown_tables(left: str, right: str) -> str | None:
    left_lines = [line for line in left.splitlines() if line.strip()]
    right_lines = [line for line in right.splitlines() if line.strip()]
    if len(left_lines) < 3 or len(right_lines) < 3:
        return None
    if left_lines[:2] != right_lines[:2]:
        return None
    return "\n".join([*left_lines, *right_lines[2:]])


def combined_locator(locators: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    if not locators:
        return {}
    if len(locators) == 1:
        return dict(locators[0])
    result = dict(locators[0])
    for key in ("row_start", "page", "paragraph", "line_start"):
        values = [locator.get(key) for locator in locators if isinstance(locator.get(key), int)]
        if values:
            result[key] = min(values)
    for key in ("row_end", "page", "paragraph", "line_end"):
        values = [locator.get(key) for locator in locators if isinstance(locator.get(key), int)]
        if values:
            result[key] = max(values)
    result["locations"] = [dict(locator) for locator in locators]
    return result


def _looks_like_separator(line: str) -> bool:
    stripped = line.replace("|", "").replace(":", "").replace("-", "").strip()
    return not stripped
