"""Permission-aware hybrid RAG over catalog metadata and user knowledge."""
from __future__ import annotations
import re

from backend.catalog.rag_documents import authorized_catalog_documents
from backend.catalog.vector_index import retrieve_catalog_vectors
from backend.hybrid_retrieval import (
    bm25_rank,
    exact_identifier_rank,
    reciprocal_rank_fusion,
    rerank_or_fallback,
    tokenize,
)
from backend.knowledge_base import retrieve as retrieve_knowledge
from backend.query_fast_path import (
    has_explicit_physical_field_intent,
    is_unambiguous_technical_query,
    qualified_table_names,
)


def requested_source_kind(query: str) -> str | None:
    lowered = str(query or "").lower()
    if "postgresql" in lowered or "postgres" in lowered or "pgsql" in lowered:
        return "postgresql"
    if "sqlite" in lowered or "本地" in lowered:
        return "local-sqlite"
    return None


def source_scoped(documents: list[dict], source_kind: str | None) -> list[dict]:
    if not source_kind:
        return documents
    return [item for item in documents if not item.get("asset_ids") or item.get("source_kind") == source_kind]


def _has_join_intent(query: str) -> bool:
    lowered = str(query or "").lower()
    if any(marker in lowered for marker in ("关联", "联表", "连接", " join ", "join到", "join 到")):
        return True
    # Natural business questions often imply a join without saying “关联”.
    product_breakdown = any(marker in lowered for marker in ("商品品类", "按品类", "类目")) \
        and any(marker in lowered for marker in ("订单", "明细", "销售", "paid"))
    customer_breakdown = any(marker in lowered for marker in ("区域", "大区", "城市")) \
        and any(marker in lowered for marker in ("订单", "成交", "paid"))
    return product_breakdown or customer_breakdown


def _has_catalog_metadata_intent(query: str) -> bool:
    lowered = str(query or "").lower()
    markers = (
        "字段", "表结构", "哪些表", "哪张表", "两张表", "维表", "明细表",
        "物理表", "数据粒度", "粒度", "关联", "连接", " join ", "join到", "join 到",
    )
    question_shapes = (
        r"哪张.*表", r"哪个.*字段", r"哪些状态", r"什么时间口径",
        r"一行代表什么", r"代表什么", r"怎么判断", r"什么地方看",
        r"哪个.*资产", r"记录在哪里",
    )
    dotted_identifier_question = bool(qualified_table_names(lowered)) and (
        "是什么" in lowered or "是" in lowered and "还是" in lowered
    )
    return (
        has_explicit_physical_field_intent(lowered)
        or any(marker in lowered for marker in markers)
        or any(re.search(pattern, lowered) for pattern in question_shapes)
        or dotted_identifier_question
    )


def _relation_evidence(query: str, documents: list[dict], ranked: list[dict], limit: int) -> list[dict]:
    """Keep relevant catalog edges for explicit multi-table questions."""
    if not _has_join_intent(query):
        return []
    seed_ids = {
        identifier
        for item in ranked[: max(8, limit)]
        for identifier in item.get("asset_ids", [])
    }
    candidates = [item for item in documents if item.get("kind") == "relation"]
    # Explicit real physical names outrank broad business synonym families.
    # This is catalog-driven, so newly reviewed relations need no hardcoded list.
    explicit_asset_ids = {
        identifier for item in documents if item.get('kind')=='table' and item.get('physical_name')
        and re.search(r'(?<![\w])'+re.escape(item['physical_name'])+r'(?![\w])',query,re.IGNORECASE)
        for identifier in item.get('asset_ids',[])
    }
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
        {"客户", "用户", "customer", "customers", "user", "users", "dim_customer"},
        {"商品", "product", "products", "dim_product"},
        {"品类", "类目", "category"},
        {"订单", "order", "orders", "fact_order"},
        {"明细", "订单明细", "order_items", "fact_order_item"},
        {"支付", "付款", "payment", "payments", "dwd_payment_detail"},
        {"退款", "refund", "refunds", "fact_refund"},
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

    def edge_coverage(item: dict) -> set[str]:
        covered = set().union(*(table_terms.get(identifier, set()) for identifier in item.get("asset_ids", [])))
        return query_terms.intersection(covered)

    ranked_positions = {
        str(item.get("id")): rank
        for rank, item in enumerate(ranked, start=1)
        if item.get("kind") == "relation"
    }

    def combined_edge_score(item: dict) -> float:
        lexical_rank = ranked_positions.get(str(item.get("id")))
        lexical_bonus = 4.0 / lexical_rank if lexical_rank else 0.0
        exact_bonus = 20.0 * len(explicit_asset_ids.intersection(item.get('asset_ids',[])))
        return edge_score(item) + lexical_bonus + exact_bonus

    ordered = sorted(
        ((combined_edge_score(item), item) for item in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    ordered = [item for score, item in ordered if score > 0]
    if not ordered:
        return []
    selected = [ordered[0]]
    component = set(ordered[0].get("asset_ids", []))
    covered_terms = edge_coverage(ordered[0])
    connected = next((
        item for item in ordered[1:]
        if component.intersection(item.get("asset_ids", []))
        and any(identifier not in component for identifier in item.get("asset_ids", []))
        and bool(edge_coverage(item) - covered_terms)
    ), None)
    if connected:
        selected.append(connected)
    endpoint_ids = {
        identifier
        for relation in selected
        for identifier in relation.get("asset_ids", [])
    }
    endpoint_tables = [
        item for item in documents
        if item.get("kind") == "table"
        and len(item.get("asset_ids", [])) == 1
        and item["asset_ids"][0] in endpoint_ids
    ]
    endpoint_tables.sort(
        key=lambda item: (
            item["asset_ids"][0] not in seed_ids,
            item["asset_ids"][0],
        )
    )
    return [*selected, *endpoint_tables]


def _merge_required_evidence(ranked: list[dict], required: list[dict], limit: int) -> list[dict]:
    if not required:
        return ranked[:limit]
    required_ids = {str(item["id"]) for item in required}
    remaining = [item for item in ranked if str(item["id"]) not in required_ids]
    return (required + remaining)[:limit]


def _qualified_technical_evidence(query: str, documents: list[dict], limit: int) -> list[dict] | None:
    """Resolve explicit physical tables locally before remote hybrid retrieval.

    Returning an empty list is intentional when an explicitly named table is
    outside the caller's authorized catalog; broader semantic matches must not
    substitute another asset or reveal its existence.
    """

    if not is_unambiguous_technical_query(query):
        return None
    requested = qualified_table_names(query)

    exact_tables: list[dict] = []
    for item in documents:
        if item.get("kind") != "table":
            continue
        namespace = str(item.get("namespace") or "").strip().casefold()
        physical_name = str(item.get("physical_name") or "").strip().casefold()
        identity = ".".join(part for part in (namespace, physical_name) if part)
        if identity in requested:
            exact_tables.append(item)
    matched_names = {
        ".".join(part for part in (
            str(item.get("namespace") or "").strip().casefold(),
            str(item.get("physical_name") or "").strip().casefold(),
        ) if part)
        for item in exact_tables
    }
    if matched_names != requested:
        return []

    asset_ids = {
        identifier
        for item in exact_tables
        for identifier in item.get("asset_ids", [])
    }
    related = [
        item for item in documents
        if item not in exact_tables
        and asset_ids.intersection(item.get("asset_ids", []))
        and (item.get("kind") != "relation" or _has_join_intent(query))
    ]
    ranked_related = bm25_rank(query, related, max(0, limit - len(exact_tables))) if related else []
    ordered = [*exact_tables, *ranked_related]
    seen: set[str] = set()
    return [
        item for item in ordered
        if not (str(item.get("id")) in seen or seen.add(str(item.get("id"))))
    ][:limit]


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
    qualified = _qualified_technical_evidence(query, normalized, limit)
    if qualified is not None:
        return qualified
    searchable = [
        {**item, "content": f"{item['title']} {' '.join(item.get('keywords', []))}\n{item['content']}"}
        for item in normalized
    ]
    lexical = bm25_rank(query, searchable, candidate_limit)
    original = {str(item["id"]): item for item in normalized}
    lexical = [{**original[str(item["id"])], "bm25_score": item["bm25_score"]} for item in lexical]
    lexical = lexical or (normalized[:candidate_limit] if fallback else [])
    catalog_exact = exact_identifier_rank(query, normalized, candidate_limit)
    local_catalog = reciprocal_rank_fusion(catalog_exact, lexical)
    if _has_catalog_metadata_intent(query):
        relations = _relation_evidence(query, normalized, local_catalog, limit)
        ranked = local_catalog
        if _has_join_intent(query):
            selected_relation_ids = {
                str(item["id"]) for item in relations if item.get("kind") == "relation"
            }
            ranked = [
                item for item in ranked
                if item.get("kind") != "relation" or str(item["id"]) in selected_relation_ids
            ]
        return _merge_required_evidence(ranked, relations, limit)
    knowledge = retrieve_knowledge(query, limit=candidate_limit, user_id=user_id, apply_rerank=False)
    exact_knowledge = [item for item in knowledge if item.get("exact_score")]
    if exact_knowledge:
        return reciprocal_rank_fusion(exact_knowledge, local_catalog)[:limit]
    dense = source_scoped(retrieve_catalog_vectors(query, user_id or "", candidate_limit), source_kind)
    catalog = reciprocal_rank_fusion(catalog_exact, dense, lexical)
    # Catalog has dense and lexical recall while knowledge has its own internal
    # hybrid recall. Fuse the two channels once each so catalog does not receive
    # an accidental two-vote advantage over knowledge.
    fused = reciprocal_rank_fusion(catalog, knowledge)
    # Keep enough fused candidates for reranking. A fixed top-10 pool caused
    # required third tables in multi-table questions to be discarded before
    # the reranker could consider them.
    rerank_pool = max(20, limit * 2)
    ranked = rerank_or_fallback(query, fused[:rerank_pool], limit)
    relations = _relation_evidence(query, normalized, ranked, limit)
    if _has_join_intent(query):
        selected_relation_ids = {
            str(item["id"]) for item in relations if item.get("kind") == "relation"
        }
        ranked = [
            item for item in ranked
            if item.get("kind") != "relation" or str(item["id"]) in selected_relation_ids
        ]
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
