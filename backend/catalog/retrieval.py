"""RAG and SQL-facing projections of published, authorized catalog metadata."""
from __future__ import annotations

from typing import Any

from backend.core import db
from backend.catalog.rag_documents import authorized_catalog_documents
from backend.catalog.service import DISCOVER, QUERY_DATA, SENSITIVITY_ORDER, can_access, list_assets, list_domains, list_metrics, list_terms


def authorized_query_schema(user_id: str) -> dict[str, set[str]]:
    """Exact physical table/column allowlists for the guarded SQL tool.

    Sensitive/restricted fields are deliberately omitted from agent-visible SQL.
    A production source can expose approved aggregate views instead, but raw PII
    never enters the model context in this reference application.
    """
    con = db(); result: dict[str, set[str]] = {}
    for asset in con.execute("SELECT id,physical_name FROM catalog_assets WHERE publish_status='published' AND source_id='local-ecommerce-demo'"):
        if not can_access(user_id, QUERY_DATA, asset_id=asset["id"], connection=con): continue
        fields = con.execute("SELECT physical_name,sensitivity FROM catalog_columns WHERE asset_id=?", (asset["id"],)).fetchall()
        result[asset["physical_name"]] = {field["physical_name"] for field in fields if field["sensitivity"] not in {"sensitive","restricted"}}
    con.close(); return result


def catalog_rag_documents(user_id: str) -> list[dict[str, str]]:
    """Compatibility projection for lexical catalog retrieval."""
    return authorized_catalog_documents(user_id)


def catalog_overview(user_id: str) -> dict[str, Any]:
    assets = list_assets(user_id)
    return {"domains":list_domains(user_id),"assets":assets,"metrics":list_metrics(user_id),"terms":list_terms(user_id),"facets":{"layers":sorted({asset["warehouse_layer"] for asset in assets}),"sources":sorted({asset["source_id"] for asset in assets}),"sensitivities":sorted({asset["sensitivity"] for asset in assets},key=lambda value:SENSITIVITY_ORDER.get(value,99))}}


def lineage_for_asset(user_id: str, asset_id: str, hops: int = 2) -> dict[str, list[dict[str, Any]]]:
    if not can_access(user_id, DISCOVER, asset_id=asset_id): raise LookupError("Lineage not authorized")
    con = db()
    if not con.execute("SELECT 1 FROM catalog_assets WHERE id=? AND publish_status='published'", (asset_id,)).fetchone():
        con.close(); raise LookupError("Lineage asset is not published")
    anchor = con.execute("SELECT id FROM catalog_lineage_nodes WHERE asset_id=? AND published=1", (asset_id,)).fetchone()
    if not anchor: con.close(); return {"nodes":[],"edges":[]}
    seen, frontier, edges = {anchor["id"]}, {anchor["id"]}, {}
    for _ in range(max(1,min(hops,3))):
        if not frontier: break
        holders = ",".join("?" for _ in frontier)
        matched = [dict(row) for row in con.execute(f"SELECT * FROM catalog_lineage_edges WHERE published=1 AND (from_node_id IN ({holders}) OR to_node_id IN ({holders}))", [*frontier,*frontier])]
        next_frontier: set[str] = set()
        for edge in matched: edges[edge['id']] = edge; next_frontier.update((edge["from_node_id"],edge["to_node_id"]))
        frontier = next_frontier - seen; seen.update(next_frontier)
    nodes = [dict(row) for row in con.execute("SELECT * FROM catalog_lineage_nodes WHERE id IN (%s)" % ",".join("?" for _ in seen),list(seen))]
    for edge in edges.values():
        edge['provenance'] = 'run_observed' if edge['id'].startswith('production:') else 'demo_seed' if edge['id'].startswith('lineage-') else 'legacy_unclassified'
    for node in nodes:
        node['provenance'] = 'run_observed' if node['id'].startswith('production:') else 'demo_seed' if node['id'].startswith('lineage-') else 'legacy_unclassified'
        if node.get("asset_id") and (not con.execute("SELECT 1 FROM catalog_assets WHERE id=? AND publish_status='published'", (node["asset_id"],)).fetchone() or not can_access(user_id, DISCOVER, asset_id=node["asset_id"],connection=con)):
            node.update({"display_name":"受限资产","description":"你无权查看此上游或下游资产的详情。","restricted":True})
    con.close(); return {"nodes":nodes,"edges":list(edges.values())}
