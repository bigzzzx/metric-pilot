"""Read-side catalog access, permission evaluation, masking and Agent allowlists."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from backend.core import DEMO_DB, db

DISCOVER, VIEW_METADATA, QUERY_DATA, MANAGE = "discover", "view_metadata", "query_data", "manage"
DATA_WRITE, ETL_EXECUTE, TOOL_ADMIN = "data.write", "etl.execute", "tool.admin"
ALL_ACTIONS = (DISCOVER, VIEW_METADATA, QUERY_DATA, MANAGE, DATA_WRITE, ETL_EXECUTE, TOOL_ADMIN)
SENSITIVITY_ORDER = {"public": 0, "internal": 1, "sensitive": 2, "restricted": 3}


def _rows(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _json(value: str | None, fallback: Any) -> Any:
    try: return json.loads(value or "")
    except json.JSONDecodeError: return fallback


def _quality_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    result = dict(row)
    result["details"] = _json(result.pop("details_json", "{}"), {})
    return result


def _subjects(con: sqlite3.Connection, user_id: str) -> list[tuple[str, str]]:
    groups = [("group", row["group_id"]) for row in con.execute("SELECT group_id FROM catalog_group_memberships WHERE user_id=?", (user_id,))]
    roles = [("role", row["role_id"]) for row in con.execute("SELECT role_id FROM catalog_user_roles WHERE user_id=?", (user_id,))]
    return [("user", user_id), *groups, *roles]


def is_platform_admin(user_id: str, connection: sqlite3.Connection | None = None) -> bool:
    own = connection is None; con = connection or db()
    row = con.execute("SELECT 1 FROM catalog_user_roles WHERE user_id=? AND role_id='role-platform-admin'", (user_id,)).fetchone()
    if own: con.close()
    return bool(row)


def can_access(user_id: str, action: str, *, asset_id: str | None = None, domain_id: str | None = None, connection: sqlite3.Connection | None = None) -> bool:
    """Evaluate the central policy contract. Explicit deny always wins."""
    if action not in ALL_ACTIONS: return False
    own = connection is None; con = connection or db()
    try:
        if is_platform_admin(user_id, con): return True
        if asset_id and not domain_id:
            asset = con.execute("SELECT domain_id FROM catalog_assets WHERE id=?", (asset_id,)).fetchone()
            domain_id = asset["domain_id"] if asset else None
        policies: list[sqlite3.Row] = []
        for subject_type, subject_id in _subjects(con, user_id):
            for resource_type, resource_id in (("asset",asset_id),("domain",domain_id),("platform","*")):
                if resource_id:
                    policies.extend(con.execute("SELECT effect FROM catalog_policies WHERE subject_type=? AND subject_id=? AND resource_type=? AND resource_id IN (?, '*') AND action IN (?, '*')", (subject_type,subject_id,resource_type,resource_id,action)).fetchall())
        if any(policy["effect"] == "deny" for policy in policies): return False
        return any(policy["effect"] == "allow" for policy in policies)
    finally:
        if own: con.close()


def _asset_view(con: sqlite3.Connection, asset: sqlite3.Row, user_id: str) -> dict[str, Any]:
    result = dict(asset); result["aliases"] = _json(result.pop("aliases_json", "[]"), [])
    source = con.execute("SELECT kind,enabled FROM data_sources WHERE id=?", (result['source_id'],)).fetchone()
    result["can_query"] = bool(source and source['enabled'] and source['kind'] in {'local-sqlite','postgresql'} and result["publish_status"] == "published" and can_access(user_id, QUERY_DATA, asset_id=result["id"], connection=con))
    result["can_manage"] = can_access(user_id, MANAGE, connection=con)
    quality = con.execute("SELECT run_id,row_count,score,freshness_at,completeness,validity,status,details_json,observed_at FROM catalog_quality_snapshots WHERE asset_id=? ORDER BY observed_at DESC,id DESC LIMIT 1", (result["id"],)).fetchone()
    result["quality"] = _quality_view(quality)
    return result


def list_domains(user_id: str) -> list[dict[str, Any]]:
    con = db(); result = []
    can_manage = can_access(user_id, MANAGE, connection=con)
    for row in con.execute("SELECT * FROM catalog_domains WHERE active=1 ORDER BY display_name"):
        if can_access(user_id, DISCOVER, domain_id=row["id"], connection=con):
            item = dict(row)
            item["asset_count"] = con.execute("SELECT COUNT(*) AS count FROM catalog_assets WHERE domain_id=? AND publish_status='published'", (row["id"],)).fetchone()["count"]
            item["draft_asset_count"] = con.execute("SELECT COUNT(*) AS count FROM catalog_assets WHERE domain_id=? AND publish_status='draft'", (row["id"],)).fetchone()["count"] if can_manage else 0
            result.append(item)
    con.close(); return result


def list_assets(user_id: str, *, query: str = "", domain_id: str | None = None, layer: str | None = None, source_id: str | None = None, sensitivity: str | None = None) -> list[dict[str, Any]]:
    con = db(); clauses = [] if can_access(user_id, MANAGE, connection=con) else ["publish_status='published'"]; values: list[Any] = []
    if domain_id: clauses.append("domain_id=?"); values.append(domain_id)
    if layer: clauses.append("warehouse_layer=?"); values.append(layer)
    if source_id: clauses.append("source_id=?"); values.append(source_id)
    if sensitivity: clauses.append("sensitivity=?"); values.append(sensitivity)
    if query.strip():
        clauses.append("(physical_name LIKE ? OR display_name LIKE ? OR description LIKE ? OR aliases_json LIKE ?)"); values.extend([f"%{query.strip()}%"] * 4)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = "SELECT * FROM catalog_assets" + where + " ORDER BY publish_status='published' DESC,certification_status='certified' DESC,display_name"
    result = [_asset_view(con,row,user_id) for row in con.execute(sql,values) if can_access(user_id,DISCOVER,asset_id=row["id"],connection=con)]
    con.close(); return result


def _relationship_view(con: sqlite3.Connection, row: sqlite3.Row, user_id: str) -> dict[str, Any]:
    result = dict(row)
    for prefix, field in (("from","from_asset_id"),("to","to_asset_id")):
        asset = con.execute("SELECT display_name,physical_name,publish_status FROM catalog_assets WHERE id=?", (row[field],)).fetchone()
        if asset and asset["publish_status"] != "published" and not can_access(user_id, MANAGE, connection=con):
            asset = None
        result[f"{prefix}_asset"] = dict(asset) if asset else None
    result["visible"] = bool(result["from_asset"] and result["to_asset"] and can_access(user_id,DISCOVER,asset_id=row["from_asset_id"],connection=con) and can_access(user_id,DISCOVER,asset_id=row["to_asset_id"],connection=con))
    return result


def get_asset(user_id: str, asset_id: str) -> dict[str, Any]:
    con = db(); asset = con.execute("SELECT * FROM catalog_assets WHERE id=?", (asset_id,)).fetchone()
    can_manage = can_access(user_id, MANAGE, connection=con)
    if not asset or (asset["publish_status"] != "published" and not can_manage) or not can_access(user_id,VIEW_METADATA,asset_id=asset_id,connection=con): con.close(); raise LookupError("Asset not found or not authorized")
    result = _asset_view(con,asset,user_id); columns = _rows(con.execute("SELECT * FROM catalog_columns WHERE asset_id=? ORDER BY ordinal", (asset_id,)))
    for column in columns:
        column["aliases"] = _json(column.pop("aliases_json", "[]"), []); column["enum_values"] = _json(column.pop("enum_values_json", "[]"), [])
        column["masked_in_samples"] = column["masking_strategy"] != "none" or column["sensitivity"] in {"sensitive","restricted"}
    result["columns"] = columns
    result["relationships"] = [_relationship_view(con,row,user_id) for row in con.execute("SELECT * FROM catalog_relationships WHERE from_asset_id=? OR to_asset_id=?", (asset_id,asset_id)) if can_manage or row["status"] == "published"]
    if can_manage:
        note = con.execute("SELECT detail_json FROM catalog_audit_logs WHERE entity_id=? AND action IN ('reject','withdraw') ORDER BY id DESC LIMIT 1", (asset_id,)).fetchone()
        result["review_note"] = _json(note["detail_json"], {}).get("reason", "") if note else ""
    result["metrics"] = _rows(con.execute("SELECT id,name,display_name,description,formula,grain,certification_status FROM catalog_metrics WHERE domain_id=? AND publish_status='published' ORDER BY display_name", (asset["domain_id"],)))
    result["quality_history"] = [
        _quality_view(row) for row in con.execute(
            "SELECT run_id,row_count,score,freshness_at,completeness,validity,status,details_json,observed_at FROM catalog_quality_snapshots WHERE asset_id=? ORDER BY observed_at DESC,id DESC LIMIT 12",
            (asset_id,),
        )
    ]
    con.close(); return result


def _mask(value: Any, sensitivity: str, strategy: str) -> Any:
    if value is None or (sensitivity not in {"sensitive","restricted"} and strategy == "none"): return value
    if sensitivity == "restricted": return "[受限]"
    text = str(value)
    return text[0] + "***" if strategy == "partial" and text else "***"


def sample_asset(user_id: str, asset_id: str, limit: int = 5) -> dict[str, Any]:
    limit = max(1,min(int(limit),5)); con = db(); asset = con.execute("SELECT * FROM catalog_assets WHERE id=? AND publish_status='published'", (asset_id,)).fetchone()
    source_config = con.execute("SELECT * FROM data_sources WHERE id=? AND enabled=1", (asset['source_id'],)).fetchone() if asset else None
    if not asset or not source_config or not can_access(user_id,QUERY_DATA,asset_id=asset_id,connection=con): con.close(); raise LookupError("Sample data not authorized or source unavailable")
    columns = _rows(con.execute("SELECT physical_name,sensitivity,masking_strategy FROM catalog_columns WHERE asset_id=? ORDER BY ordinal", (asset_id,))); con.close()
    names = [column["physical_name"] for column in columns]
    if not names: return {"rows": [],"columns": [],"limit": limit,"masked": True}
    quoted = ", ".join('"' + name.replace('"','""') + '"' for name in names); physical = '"' + asset["physical_name"].replace('"','""') + '"'
    if source_config['kind'] == 'postgresql':
        from backend.data_sources.postgresql_query import execute_postgres
        namespace = '"' + (asset['namespace'] or 'public').replace('"','""') + '"'
        # Project masked literals on the server: sensitive values never leave PostgreSQL.
        projected = []
        for column in columns:
            name = '"' + column['physical_name'].replace('"','""') + '"'
            if column['sensitivity'] == 'restricted':
                projected.append("'[受限]' AS " + name)
            elif column['sensitivity'] == 'sensitive' or column['masking_strategy'] != 'none':
                projected.append("'***' AS " + name)
            else:
                projected.append(name)
        rows = execute_postgres(source_config, f"SELECT {', '.join(projected)} FROM {namespace}.{physical}", limit)
    elif source_config['kind'] == 'local-sqlite':
        source = db(DEMO_DB)
        try: rows = [dict(row) for row in source.execute(f"SELECT {quoted} FROM {physical} LIMIT ?", (limit,)).fetchall()]
        finally: source.close()
    else:
        raise LookupError('Source executor unavailable')
    for row in rows:
        for column in columns:
            if source_config['kind'] != 'postgresql':
                row[column["physical_name"]] = _mask(row.get(column["physical_name"]),column["sensitivity"],column["masking_strategy"])
    return {"rows":rows,"columns":names,"limit":limit,"masked":True,"notice":"仅展示最多 5 条经脱敏的样例数据；原始数据不会写入目录或发送给模型。"}


def list_metrics(user_id: str) -> list[dict[str, Any]]:
    con = db(); result = []
    for row in con.execute("SELECT * FROM catalog_metrics WHERE publish_status='published' ORDER BY certification_status='certified' DESC,display_name"):
        if can_access(user_id, DISCOVER, domain_id=row["domain_id"], connection=con):
            from backend.semantic_metrics.wiki import visible_wiki
            item=dict(row)
            contract=_json(item.pop('contract_json','{}'),{})
            if contract.get('asset_id') and not can_access(user_id,DISCOVER,asset_id=contract['asset_id'],connection=con): continue
            item['contract']=contract
            item['aliases']=_json(item.pop('aliases_json','[]'),[])
            item['wiki']=visible_wiki(con,row,user_id)
            item.pop('wiki_json',None)
            draft=item.pop('wiki_draft_json','{}')
            item['can_manage']=can_access(user_id,MANAGE,connection=con)
            if item['can_manage']: item['wiki_draft']=_json(draft,{})
            result.append(item)
    con.close(); return result


def list_terms(user_id: str) -> list[dict[str, Any]]:
    con = db(); result = []
    for row in con.execute("SELECT * FROM catalog_terms WHERE publish_status='published' ORDER BY display_name"):
        if not row["domain_id"] or can_access(user_id, DISCOVER, domain_id=row["domain_id"], connection=con):
            item = dict(row); item["aliases"] = _json(item.pop("aliases_json", "[]"), []); result.append(item)
    con.close(); return result
