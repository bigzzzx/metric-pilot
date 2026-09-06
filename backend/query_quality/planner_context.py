"""Assemble bounded verified schema context without another LLM routing pass."""
import json


def candidate_tables(rag):
    ids = []
    for item in rag:
        for identifier in item.get("asset_ids", []):
            if identifier not in ids:
                ids.append(identifier)
    return ids[:3]


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
