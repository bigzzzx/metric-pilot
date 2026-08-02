"""A transparent metadata RAG layer for the local data lake."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = json.loads((ROOT / "metadata" / "rag_documents.json").read_text(encoding="utf-8"))


def retrieve(query: str, limit: int = 5, fallback: bool = True) -> list[dict]:
    """Return ranked catalog, metric, and SQL-example chunks for an LLM prompt."""
    tokens = set(re.findall(r"[a-zA-Z_]+|[\u4e00-\u9fff]{2,}", query.lower()))
    ranked = []
    for item in DOCUMENTS:
        text = (item["title"] + " " + item["content"] + " " + " ".join(item.get("keywords", []))).lower()
        score = sum(token in text for token in tokens)
        if score:
            ranked.append((score, item))
    matches = [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit]]
    return matches or (DOCUMENTS[:limit] if fallback else [])


def prompt_context(query: str, chunks: list[dict] | None = None) -> str:
    chunks = chunks if chunks is not None else retrieve(query)
    return "\n\n".join(f"[{item['kind']}] {item['title']}: {item['content']}" for item in chunks)
