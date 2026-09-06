"""Small, dependency-free table formatting helpers shared by parsers."""

from __future__ import annotations

from typing import Iterable


def cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def markdown_table(rows: Iterable[Iterable[object]]) -> str:
    normalized = [[cell_text(value) for value in row] for row in rows]
    normalized = [row for row in normalized if any(row)]
    if not normalized:
        return ""
    width = max(len(row) for row in normalized)
    normalized = [row + [""] * (width - len(row)) for row in normalized]
    header = normalized[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
    return "\n".join(lines)


def markdown_row(header: Iterable[object], row: Iterable[object]) -> str:
    return markdown_table([header, row])
