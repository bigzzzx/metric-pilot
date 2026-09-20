"""Bounded, permission-scoped business definitions for query planning."""
import hashlib
import json
import re

from backend.catalog.rag_documents import authorized_catalog_documents
from backend.query_fast_path import has_explicit_physical_field_intent


def retrieval_question(state: dict) -> str:
    """Use a grounded router rewrite, or the bounded history expansion."""
    question = state["message"]
    if not state.get("contextual_follow_up"):
        return question
    recent = [item["content"] for item in state.get("history", [])
              if item.get("role") == "user" and not item.get("content", "").startswith("Tool result:")]
    context = "\n".join(recent[-2:])[-800:]
    summary = str(state.get("conversation_summary") or "")[-400:]
    rewritten = str(state.get("retrieval_query") or "").strip()
    source = f"{question}\n{context}\n{summary}".casefold()
    # Reject new dates, counts, qualified tables and acronym metric names.
    # Explicit identifiers in the current request must survive the rewrite.
    concrete = re.compile(r"\d+(?:[./:-]\d+)*|(?<![a-z0-9_])[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*(?![a-z0-9_])|(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{1,}(?![A-Za-z0-9_])")
    required = {token.casefold() for token in concrete.findall(question)}
    found = {token.casefold() for token in concrete.findall(rewritten)}
    if (rewritten and len(rewritten) <= 300 and "\n" not in rewritten
            and required <= found and all(token in source for token in found)):
        return rewritten
    return f"{question}\n最近数据话题：{context}\n会话摘要：{summary}"


def fingerprint(document: dict) -> str:
    payload = {key: document.get(key) for key in ("id", "title", "content", "asset_ids", "domain_id")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def metric_context(query: str, rag: list[dict], user_id: str) -> list[dict]:
    if not user_id:
        return []
    # A qualified-table request for raw physical columns is intentionally not
    # a semantic-metric request. This keeps governance strict for business
    # questions while allowing engineers to audit or reconcile stored columns.
    if has_explicit_physical_field_intent(query):
        return []
    # Text-to-SQL planning must not advertise a metric which the strict
    # reference validator would later reject. Explicit metric requests have
    # already passed through metric_router; this context is only supporting
    # evidence for an unmatched/ad-hoc plan.
    from backend.semantic_metrics.repository import metric_records
    executable = {
        item.document_id: item
        for item in metric_records(user_id)
        if item.can_query and item.certification_status == "certified"
        and item.contract and item.contract.status == "active"
    }
    retrieved_ids = {
        item.get("id") for item in rag
        if item.get("kind") == "metric" and item.get("id") in executable
    }
    metrics = []
    for item in authorized_catalog_documents(user_id):
        if item.get("kind") != "metric":
            continue
        if item.get("id") not in executable:
            continue
        aliases = [str(word) for word in item.get("keywords", []) if str(word) not in {"指标", "口径"}]
        explicit = any(word and word.casefold() in query.casefold() for word in aliases)
        if explicit or item["id"] in retrieved_ids:
            metrics.append({"id": item["id"], "version": fingerprint(item),
                            "definition": item["content"], "required": explicit})
    return sorted(metrics, key=lambda item: not item["required"])[:5]


def executable_planning_rag(rag: list[dict], user_id: str) -> list[dict]:
    """Remove metric claims which cannot be compiled under current policy."""
    if not user_id:
        return [item for item in rag if item.get("kind") not in {"metric", "metric_wiki"}]
    from backend.semantic_metrics.repository import metric_records
    executable = {
        item.id for item in metric_records(user_id)
        if item.can_query and item.certification_status == "certified"
        and item.contract and item.contract.status == "active"
    }
    allowed_ids = {
        *(f"catalog:metric:{metric_id}" for metric_id in executable),
        *(f"catalog:metric-wiki:{metric_id}" for metric_id in executable),
    }
    return [
        item for item in rag
        if item.get("kind") not in {"metric", "metric_wiki"}
        or str(item.get("id")) in allowed_ids
    ]


def check_metric_references(plan: dict, metrics: list[dict], user_id: str) -> str | None:
    """Check evidence identity/freshness, not arbitrary SQL semantic equivalence."""
    if not plan.get("sql"):
        return None
    references = plan.get("metric_refs") or []
    required = {item["id"] for item in metrics if item.get("required")}
    cited = {item["id"] for item in references}
    if required - cited:
        return "查询计划未引用本次问题对应的指标口径，请补充口径后重新规划。"
    if not references:
        return None
    supplied = {item["id"]: item["version"] for item in metrics}
    current = {item["id"]: fingerprint(item) for item in authorized_catalog_documents(user_id)
               if item.get("kind") == "metric"}
    if any(supplied.get(item["id"]) != item["version"] or current.get(item["id"]) != item["version"]
           for item in references):
        return "指标口径已变更、未提供或当前无权访问，请重新提问以获取最新口径。"
    return None


def planning_instructions(metrics: list[dict], examples: list[dict]) -> str:
    return (
        "\nBusiness contract: preserve published metric definitions, status filters, units and grain. "
        "For each used metric copy its id/version exactly into metric_refs. Required metrics must be cited. "
        "Do not multiply measures through one-to-many joins: aggregate or deduplicate first. "
        "If time range, metric meaning or join grain is ambiguous, return sql=null and ask a clarification. "
        "Use only columns explicitly present in the provided evidence. Never invent a payment-time column "
        "or substitute order/creation time for actual payment time. If the required field is missing, "
        "return sql=null, explain the missing information and ask for the correct field or definition. "
        "Examples are references only; adapt dates and dimensions to this question. "
        "Return metric_refs (list of {id,version}), time_column, filters (list of strings), "
        "join_relationships (list of strings) alongside answer/sql.\n"
        + json.dumps({"metrics": metrics, "verified_examples": examples}, ensure_ascii=False)
    )
