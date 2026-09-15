"""Permission-aware access to governed metric contracts."""
from __future__ import annotations

import json
from typing import Any

from backend.semantic_metrics.storage import metric_document
from backend.catalog.service import DISCOVER, QUERY_DATA, can_access
from backend.core import db
from backend.query_quality.evidence import fingerprint
from backend.semantic_metrics.models import GovernedMetric, MetricContract


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def metric_records(user_id: str, *, include_unauthorized: bool = False) -> list[GovernedMetric]:
    """Return published metrics and validated contracts without row data."""
    if not user_id:
        return []
    con = db()
    try:
        result: list[GovernedMetric] = []
        for row in con.execute(
            "SELECT * FROM catalog_metrics WHERE publish_status='published' ORDER BY id"
        ):
            raw_contract = _json(dict(row).get("contract_json"), {})
            contract = None
            try:
                if raw_contract:
                    contract = MetricContract.model_validate(raw_contract)
            except ValueError:
                contract = None
            authorized = can_access(user_id, DISCOVER, domain_id=row["domain_id"], connection=con)
            if not authorized:
                continue
            if contract and not can_access(user_id,DISCOVER,asset_id=contract.asset_id,connection=con):
                continue
            query_allowed = can_access(user_id, QUERY_DATA, domain_id=row['domain_id'], connection=con)
            if contract:
                query_allowed = query_allowed and can_access(
                    user_id, QUERY_DATA, asset_id=contract.asset_id, connection=con
                )
            if not authorized and not include_unauthorized:
                continue
            document_id = f"catalog:metric:{row['id']}"
            document = metric_document(row)
            from backend.semantic_metrics.wiki import visible_wiki
            result.append(GovernedMetric(
                id=row["id"], document_id=document_id,
                version=fingerprint(document) if document else "",
                name=row["name"], display_name=row["display_name"],
                domain_id=row["domain_id"], description=row["description"],
                aliases=_json(dict(row).get("aliases_json"), []),
                certification_status=row["certification_status"],
                contract=contract, can_query=query_allowed,
                wiki=visible_wiki(con,row,user_id),
            ))
        return result
    finally:
        con.close()
