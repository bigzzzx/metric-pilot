"""Permission-aware hybrid RAG over catalog metadata and user knowledge."""
from __future__ import annotations

from backend.catalog.rag_documents import authorized_catalog_documents
from backend.catalog.vector_index import retrieve_catalog_vectors
from backend.hybrid_retrieval import bm25_rank, reciprocal_rank_fusion, rerank_or_fallback
from backend.knowledge_base import retrieve as retrieve_knowledge


def retrieve(query: str, limit: int = 8, fallback: bool = True, user_id: str | None = None) -> list[dict]:
    """Return only permission-scoped catalog metadata and knowledge chunks.

    Catalog dense candidates and lexical candidates are both rebuilt from the
    governed SQLite source.  No vector result can reach RRF, reranking or the
    Agent prompt before the central catalog policy check succeeds.
    """
    candidate_limit = max(limit * 2, 10)
    authorized = authorized_catalog_documents(user_id or "")
    normalized = [
        {**item, "kind": item.get("kind", "catalog"), "keywords": item.get("keywords", [])}
        for item in authorized
    ]
    searchable = [
        {**item, "content": f"{item['title']} {' '.join(item.get('keywords', []))}\n{item['content']}"}
        for item in normalized
    ]
    lexical = bm25_rank(query, searchable, candidate_limit)
    original = {str(item["id"]): item for item in normalized}
    lexical = [{**original[str(item["id"])], "bm25_score": item["bm25_score"]} for item in lexical]
    lexical = lexical or (normalized[:candidate_limit] if fallback else [])
    dense = retrieve_catalog_vectors(query, user_id or "", candidate_limit)
    knowledge = retrieve_knowledge(query, limit=candidate_limit, user_id=user_id, apply_rerank=False)
    fused = reciprocal_rank_fusion(dense, lexical, knowledge)
    return rerank_or_fallback(query, fused[:10], limit)


def _source_hint(item: dict) -> str:
    """Render stable human/model-readable source coordinates when available."""
    parts: list[str] = []
    section = item.get("section_path") or []
    if section:
        parts.append("章节 " + " > ".join(str(value) for value in section))
    locator = item.get("locator") or {}
    if locator.get("page") is not None:
        parts.append(f"第 {locator['page']} 页")
    if locator.get("slide") is not None:
        parts.append(f"第 {locator['slide']} 页幻灯片")
    if locator.get("sheet"):
        parts.append(f"工作表 {locator['sheet']}")
    if locator.get("row_start") is not None:
        end = locator.get("row_end", locator["row_start"])
        parts.append(f"第 {locator['row_start']}-{end} 行")
    return "；来源：" + "，".join(parts) if parts else ""


def prompt_context(query: str, chunks: list[dict] | None = None) -> str:
    chunks = chunks if chunks is not None else retrieve(query)
    return "\n\n".join(
        f"[{item['kind']}] {item['title']}{_source_hint(item)}: {item['content']}"
        for item in chunks
    )
