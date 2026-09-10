"""Permission-aware hybrid RAG over catalog metadata and user knowledge."""
from __future__ import annotations

from backend.catalog.rag_documents import authorized_catalog_documents
from backend.catalog.vector_index import retrieve_catalog_vectors
from backend.hybrid_retrieval import bm25_rank, reciprocal_rank_fusion, rerank_or_fallback, tokenize
from backend.knowledge_base import retrieve as retrieve_knowledge


def requested_source_kind(query: str) -> str | None:
    lowered = str(query or "").lower()
    if "postgresql" in lowered or "postgres" in lowered or "pgsql" in lowered:
        return "postgresql"
    if "sqlite" in lowered or "本地演示" in lowered:
        return "local-sqlite"
    return None


def source_scoped(documents: list[dict], source_kind: str | None) -> list[dict]:
    if not source_kind:
        return documents
    return [item for item in documents if not item.get("asset_ids") or item.get("source_kind") == source_kind]


def _has_join_intent(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(marker in lowered for marker in ("关联", "联表", "连接", " join ", "join到", "join 到"))


def _relation_evidence(query: str, documents: list[dict], ranked: list[dict], limit: int) -> list[dict]:
    """Keep relevant catalog edges for explicit multi-table questions."""
    if not _has_join_intent(query):
        return []
    seed_ids = {
        identifier
        for item in ranked[: max(8, limit)]
        for identifier in item.get("asset_ids", [])
    }
    candidates = [
        item for item in documents
        if item.get("kind") == "relation" and seed_ids.intersection(item.get("asset_ids", []))
    ]
    shape_terms = {
        "使用", "回答", "测试", "数仓", "关联", "联表", "连接", "统计", "去重",
        "订单数", "单数", "件数", "数量", "金额", "总额", "合计", "postgresql",
    }
    query_terms = {
        term for term in tokenize(query)
        if (len(term) > 1 or term.isascii()) and term not in shape_terms
    }
    term_families = (
        {"区域", "大区", "地区", "region"},
        {"客户", "用户", "customer", "customers", "dim_customer"},
        {"商品", "product", "products", "dim_product"},
        {"品类", "类目", "category"},
        {"订单", "order", "orders", "fact_order"},
        {"明细", "订单明细", "order_items", "fact_order_item"},
    )
    for family in term_families:
        if query_terms.intersection(family):
            query_terms.update(family)
    table_terms = {
        item["asset_ids"][0]: {
            term for term in tokenize(f"{item.get('title', '')} {item.get('content', '')}")
            if len(term) > 1 or term.isascii()
        }
        for item in documents
        if item.get("kind") == "table" and len(item.get("asset_ids", [])) == 1
    }
    frequencies = {
        term: sum(term in terms for terms in table_terms.values())
        for term in query_terms
    }

    def edge_score(item: dict) -> float:
        covered = set().union(*(table_terms.get(identifier, set()) for identifier in item.get("asset_ids", [])))
        return sum(1.0 / max(1, frequencies[term]) for term in query_terms.intersection(covered))

    ordered = sorted(candidates, key=edge_score, reverse=True)
    if not ordered:
        return []
    selected = [ordered[0]]
    component = set(ordered[0].get("asset_ids", []))
    connected = next((
        item for item in ordered[1:]
        if component.intersection(item.get("asset_ids", []))
        and any(identifier not in component for identifier in item.get("asset_ids", []))
    ), None)
    if connected:
        selected.append(connected)
    selected.extend(item for item in ordered[1:] if item not in selected)
    return selected[:4]


def _merge_required_evidence(ranked: list[dict], required: list[dict], limit: int) -> list[dict]:
    if not required:
        return ranked[:limit]
    required_ids = {str(item["id"]) for item in required}
    remaining = [item for item in ranked if str(item["id"]) not in required_ids]
    return (required + remaining)[:limit]


def retrieve(query: str, limit: int = 8, fallback: bool = True, user_id: str | None = None) -> list[dict]:
    """Return only permission-scoped catalog metadata and knowledge chunks.

    Catalog dense candidates and lexical candidates are both rebuilt from the
    governed SQLite source.  No vector result can reach RRF, reranking or the
    Agent prompt before the central catalog policy check succeeds.
    """
    candidate_limit = max(limit * 2, 10)
    authorized = authorized_catalog_documents(user_id or "")
    source_kind = requested_source_kind(query)
    normalized = source_scoped([
        {**item, "kind": item.get("kind", "catalog"), "keywords": item.get("keywords", [])}
        for item in authorized
    ], source_kind)
    searchable = [
        {**item, "content": f"{item['title']} {' '.join(item.get('keywords', []))}\n{item['content']}"}
        for item in normalized
    ]
    lexical = bm25_rank(query, searchable, candidate_limit)
    original = {str(item["id"]): item for item in normalized}
    lexical = [{**original[str(item["id"])], "bm25_score": item["bm25_score"]} for item in lexical]
    lexical = lexical or (normalized[:candidate_limit] if fallback else [])
    dense = source_scoped(retrieve_catalog_vectors(query, user_id or "", candidate_limit), source_kind)
    knowledge = retrieve_knowledge(query, limit=candidate_limit, user_id=user_id, apply_rerank=False)
    fused = reciprocal_rank_fusion(dense, lexical, knowledge)
    # Keep enough fused candidates for reranking. A fixed top-10 pool caused
    # required third tables in multi-table questions to be discarded before
    # the reranker could consider them.
    rerank_pool = max(20, limit * 2)
    ranked = rerank_or_fallback(query, fused[:rerank_pool], limit)
    relations = _relation_evidence(query, normalized, ranked, limit)
    return _merge_required_evidence(ranked, relations, limit)


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
