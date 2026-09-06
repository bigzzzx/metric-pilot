"""HTML parser built on Beautiful Soup."""

from __future__ import annotations

from backend.knowledge.models import DocumentBlock, ParsedDocument, dependency_missing
from backend.knowledge.parsers.base import decode_text, document_title, require_readable


def _markdown_table(element) -> str:
    rows = []
    for row in element.find_all("tr"):
        values = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
        if values:
            rows.append(values)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


class HtmlParser:
    name = "html"
    extensions = frozenset({"html", "htm"})
    media_types = frozenset({"text/html", "application/xhtml+xml"})

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise dependency_missing("beautifulsoup4", self.name) from exc

        soup = BeautifulSoup(decode_text(data, parser_name=self.name), "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        section: list[str] = []
        blocks: list[DocumentBlock] = []
        table_index = 0
        for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table"]):
            name = element.name.lower()
            if name.startswith("h"):
                level = int(name[1])
                value = element.get_text(" ", strip=True)
                if not value:
                    continue
                section = section[: level - 1]
                section.append(value)
                blocks.append(DocumentBlock("heading", value, tuple(section), {"tag": name, "level": level}))
            elif name == "table":
                table_index += 1
                value = _markdown_table(element)
                if value:
                    blocks.append(DocumentBlock("table", value, tuple(section), {"table": table_index}))
            elif name == "pre":
                value = element.get_text("\n", strip=True)
                if value:
                    blocks.append(DocumentBlock("code", value, tuple(section), {"tag": "pre"}))
            else:
                # Ignore nested list items already represented by their parent item.
                if name == "li" and element.find_parent("li") is not None:
                    continue
                value = element.get_text(" ", strip=True)
                if value:
                    blocks.append(DocumentBlock("list" if name == "li" else "paragraph", value, tuple(section), {"tag": name}))

        readable = [block for block in blocks if block.block_type != "heading"]
        require_readable(len(readable), self.name)
        html_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        heading_title = next((block.text for block in blocks if block.block_type == "heading"), "")
        return ParsedDocument(html_title or heading_title or document_title(filename), "html", tuple(blocks), {"media_type": media_type or "text/html"})
