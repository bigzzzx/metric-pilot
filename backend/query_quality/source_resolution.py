"""Deterministic source binding from parsed SQL to verified catalog schemas."""
from __future__ import annotations

from dataclasses import dataclass

from backend.data_sources.query_catalog import QueryTargetError
from backend.data_sources.sql_validation import parse_query


@dataclass(frozen=True, slots=True)
class SourceResolution:
    valid: bool
    source_id: str | None
    planner_source_id: str | None
    reason: str


def resolve_source_id(sql: str, planner_source_id: str | None, schemas: dict | None) -> SourceResolution:
    """Bind every parsed physical table uniquely and require a single source."""

    try:
        _, parsed_tables = parse_query(sql)
    except QueryTargetError:
        return SourceResolution(False, None, planner_source_id, "sql_parse_failed")
    verified = list((schemas or {}).get("tables") or [])
    if not parsed_tables or not verified:
        return SourceResolution(False, None, planner_source_id, "verified_schema_missing")

    mapped = []
    for table in parsed_tables:
        table_name = str(table.name or "").casefold()
        namespace = str(table.db or "").casefold()
        candidates = [
            item for item in verified
            if str(item.get("physical_name") or item.get("name") or "").casefold() == table_name
            and (
                not namespace
                or str(item.get("namespace") or "").casefold() == namespace
            )
        ]
        identities = {
            (
                str(item.get("source_id") or ""),
                str(item.get("namespace") or "").casefold(),
                str(item.get("physical_name") or item.get("name") or "").casefold(),
            )
            for item in candidates
        }
        if len(identities) != 1:
            reason = "sql_table_not_verified" if not identities else "sql_table_mapping_ambiguous"
            return SourceResolution(False, None, planner_source_id, reason)
        identity = next(iter(identities))
        if not identity[0]:
            return SourceResolution(False, None, planner_source_id, "verified_source_missing")
        mapped.append(identity)

    sources = {identity[0] for identity in mapped}
    if len(sources) != 1:
        return SourceResolution(False, None, planner_source_id, "cross_source_sql")
    source_id = next(iter(sources))
    if not planner_source_id:
        reason = "inferred_from_verified_sql_tables"
    elif planner_source_id != source_id:
        reason = "corrected_from_verified_sql_tables"
    else:
        reason = "planner_source_verified"
    return SourceResolution(True, source_id, planner_source_id, reason)
