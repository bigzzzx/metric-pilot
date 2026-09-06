"""Small, authoritative catalog lookups with server-owned identity and no row data."""
import json

from pydantic import Field

from backend.catalog.service import DISCOVER, QUERY_DATA, can_access
from backend.core import db
from backend.tools.models import StrictModel


class SchemaLookupInput(StrictModel):
    tables: list[str] = Field(min_length=1, max_length=3)


class SchemaLookupOutput(StrictModel):
    tables: list[dict] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)


def lookup_schema(payload: SchemaLookupInput, context) -> dict:
    result = {"tables": [], "unavailable": []}
    con = db()
    try:
        for name in dict.fromkeys(payload.tables):
            asset = con.execute("SELECT id,physical_name,data_grain FROM catalog_assets WHERE publish_status='published' AND (id=? OR physical_name=?)", (name, name)).fetchone()
            if not asset or not all(can_access(context.user_id, action, asset_id=asset["id"], connection=con)
                                    for action in (DISCOVER, QUERY_DATA)):
                # Missing and unauthorized are intentionally indistinguishable.
                result["unavailable"].append("请求的表结构不可用或无权访问")
                continue
            columns = con.execute("SELECT physical_name,data_type,description,semantic_role FROM catalog_columns WHERE asset_id=? AND sensitivity NOT IN ('sensitive','restricted') ORDER BY ordinal", (asset["id"],)).fetchall()
            table = {"asset_id": asset["id"], "name": asset["physical_name"],
                     "grain": str(asset["data_grain"] or "")[:150],
                     "columns": [{"name": c["physical_name"], "type": c["data_type"],
                                  "description": str(c["description"] or "")[:120], "role": c["semantic_role"]} for c in columns]}
            # Never silently omit columns then tell the model the schema is complete.
            if len(columns) > 80 or len(json.dumps([*result["tables"], table], ensure_ascii=False)) > 15000:
                result["unavailable"].append("表结构超过本次上下文预算，请使用更窄的授权视图")
                continue
            result["tables"].append(table)
        return result
    finally:
        con.close()


def migrate_schema_binding(con):
    """Once per agent; subsequent administrator unbinding is respected."""
    con.execute("CREATE TABLE IF NOT EXISTS query_quality_migrations(agent_id TEXT PRIMARY KEY)")
    row = con.execute("SELECT id,tools FROM agents WHERE id='data-query-agent'").fetchone()
    if not row or con.execute("SELECT 1 FROM query_quality_migrations WHERE agent_id=?", (row["id"],)).fetchone():
        return
    bound = json.loads(row["tools"] or "[]")
    if "catalog_rag" in bound and "readonly_sql" in bound and "catalog_schema" not in bound:
        con.execute("UPDATE agents SET tools=? WHERE id=?", (json.dumps([*bound, "catalog_schema"]), row["id"]))
    con.execute("INSERT INTO query_quality_migrations VALUES (?)", (row["id"],))
