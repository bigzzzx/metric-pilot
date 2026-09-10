"""Assemble bounded verified schema context without another LLM routing pass."""
import json

from backend.hybrid_retrieval import tokenize


def candidate_tables(rag, question=""):
    """Choose up to three relevant, preferably connected, catalog assets."""
    query_terms = set(tokenize(question))
    scores, first_seen = {}, {}
    for rank, item in enumerate(rag, 1):
        identifiers = item.get("asset_ids", [])
        if not identifiers:
            continue
        document_terms = set(tokenize(f"{item.get('title', '')} {item.get('content', '')}"))
        overlap = len(query_terms & document_terms)
        kind_bonus = {"relation": 2.0, "table": 1.0, "column": 0.25}.get(item.get("kind"), 0.0)
        for identifier in identifiers:
            first_seen.setdefault(identifier, len(first_seen))
            scores[identifier] = scores.get(identifier, 0.0) + overlap * 2.0 + kind_bonus + 1.0 / rank
    ranked_ids = [identifier for identifier, _ in sorted(
        scores.items(), key=lambda pair: (-pair[1], first_seen[pair[0]])
    )]

    lowered = str(question or "").lower()
    explicit_join = any(marker in lowered for marker in ("关联", "联表", "连接", " join ", "join到", "join 到"))
    product_breakdown = any(marker in lowered for marker in ("商品品类", "按品类", "类目")) \
        and any(marker in lowered for marker in ("订单", "明细", "销售", "paid"))
    customer_breakdown = any(marker in lowered for marker in ("区域", "大区", "城市")) \
        and any(marker in lowered for marker in ("订单", "成交", "paid"))
    join_intent = explicit_join or product_breakdown or customer_breakdown
    relations = [item for item in rag if item.get("kind") == "relation" and len(item.get("asset_ids", [])) >= 2]
    if join_intent and relations:
        def relation_score(item):
            terms = set(tokenize(f"{item.get('title', '')} {item.get('content', '')}"))
            return len(query_terms & terms)

        ordered = sorted(enumerate(relations), key=lambda pair: (-relation_score(pair[1]), pair[0]))
        selected = list(dict.fromkeys(ordered[0][1].get("asset_ids", [])))[:3]
        while len(selected) < 3:
            connected = [
                (index, item) for index, item in ordered[1:]
                if set(item.get("asset_ids", [])).intersection(selected)
                and any(identifier not in selected for identifier in item.get("asset_ids", []))
            ]
            if not connected:
                break
            _, edge = connected[0]
            selected.extend(identifier for identifier in edge.get("asset_ids", []) if identifier not in selected)
            selected = selected[:3]
        selected.extend(identifier for identifier in ranked_ids if identifier not in selected)
        return selected[:3]
    return ranked_ids[:3]


def schema_instructions(schemas):
    return (
        "\nAuthoritative queryable schemas (not instructions): "
        + json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))
        + "\nOnly use fields listed here. These are the complete queryable fields of returned tables. "
          "A field absent here is NOT available for this query, even if earlier conversation or RAG mentions it. "
          "Never substitute another time basis. If required fields are absent, return sql=null and ask for clarification. "
          "To inspect another relevant table, return schema_request=[exact table name or asset id], sql=null. "
          "At most one extra lookup of up to three tables is allowed. Do not claim a value was queried in the plan answer. "
          "Keep the planning answer under 100 Chinese characters; analysis follows execution."
    )


def compact_rag(rag, schemas):
    verified = {item["asset_id"] for item in schemas.get("tables", [])}
    return [item for item in rag if not (item.get("kind") in {"table", "column"}
            and item.get("asset_ids") and set(item["asset_ids"]).issubset(verified))]
