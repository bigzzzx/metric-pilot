"""Read-only PostgreSQL table profiling without retrieving business values."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from psycopg import sql


FRESHNESS_NAMES = (
    "updated_at", "modified_at", "last_updated_at", "event_time", "event_at",
    "created_at", "order_date", "business_date", "stat_date", "dt", "date",
)


def _freshness_column(columns: list[dict[str, Any]]) -> str | None:
    temporal = {
        item["physical_name"]: str(item.get("data_type") or "").lower()
        for item in columns
        if "date" in str(item.get("data_type") or "").lower()
        or "time" in str(item.get("data_type") or "").lower()
    }
    for name in FRESHNESS_NAMES:
        if name in temporal:
            return name
    return next(iter(temporal), None)


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _freshness_score(value: Any) -> tuple[float | None, float | None]:
    timestamp = _as_utc(value)
    if not timestamp:
        return None, None
    age_days = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 86400)
    if age_days <= 1:
        score = 1.0
    elif age_days <= 7:
        score = 0.9
    elif age_days <= 30:
        score = 0.75
    else:
        score = 0.5
    return score, round(age_days, 2)


def _profile_table(connection, asset: dict[str, Any]) -> dict[str, Any]:
    columns = list(asset.get("columns") or [])[:200]
    namespace = asset.get("namespace") or "public"
    table = asset["physical_name"]
    freshness_column = _freshness_column(columns)
    projections = [sql.SQL("COUNT(*) AS row_count")]
    for index, column in enumerate(columns):
        projections.append(sql.SQL("COUNT(*) FILTER (WHERE {} IS NULL) AS {}").format(
            sql.Identifier(column["physical_name"]), sql.Identifier(f"null_{index}"),
        ))
    if freshness_column:
        projections.append(sql.SQL("MAX({}) AS freshness_at").format(sql.Identifier(freshness_column)))
    statement = sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(projections), sql.Identifier(namespace), sql.Identifier(table),
    )
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '30s'")
        cursor.execute(statement)
        aggregate = cursor.fetchone()
        row_count = int(aggregate["row_count"] or 0)
        null_counts = [int(aggregate[f"null_{index}"] or 0) for index in range(len(columns))]
        primary_key = [value for value in asset.get("primary_key", []) if value]
        pk_null_rows = 0
        duplicate_rows = 0
        if primary_key and row_count:
            null_condition = sql.SQL(" OR ").join(
                sql.SQL("{} IS NULL").format(sql.Identifier(name)) for name in primary_key
            )
            grouped_columns = sql.SQL(", ").join(sql.Identifier(name) for name in primary_key)
            pk_statement = sql.SQL("""
                SELECT
                  COUNT(*) FILTER (WHERE {null_condition}) AS null_rows,
                  (SELECT COALESCE(SUM(group_count - 1), 0) FROM (
                    SELECT COUNT(*) AS group_count FROM {schema}.{table}
                    WHERE NOT ({null_condition}) GROUP BY {grouped_columns} HAVING COUNT(*) > 1
                  ) duplicate_groups) AS duplicate_rows
                FROM {schema}.{table}
            """).format(
                null_condition=null_condition, grouped_columns=grouped_columns,
                schema=sql.Identifier(namespace), table=sql.Identifier(table),
            )
            cursor.execute(pk_statement)
            pk_stats = cursor.fetchone()
            pk_null_rows = int(pk_stats["null_rows"] or 0)
            duplicate_rows = int(pk_stats["duplicate_rows"] or 0)

    null_cells = sum(null_counts)
    cell_count = row_count * len(columns)
    completeness = 1.0 if not cell_count else max(0.0, 1 - null_cells / cell_count)
    invalid_rows = min(row_count, pk_null_rows + duplicate_rows)
    validity = 1.0 if not row_count or not primary_key else max(0.0, 1 - invalid_rows / row_count)
    freshness_at = aggregate.get("freshness_at") if freshness_column else None
    freshness, freshness_age_days = _freshness_score(freshness_at)
    weights = [(completeness, 0.7), (validity, 0.3)] if freshness is None else [
        (completeness, 0.55), (validity, 0.3), (freshness, 0.15),
    ]
    score = round(sum(value * weight for value, weight in weights), 4)
    status = "healthy" if score >= 0.95 else "warning" if score >= 0.8 else "critical"
    if row_count == 0:
        status = "warning"
    column_null_rates = [{
        "column": column["physical_name"],
        "null_count": null_counts[index],
        "null_rate": round(null_counts[index] / row_count, 4) if row_count else 0.0,
    } for index, column in enumerate(columns)]
    return {
        "asset_id": asset["id"], "row_count": row_count, "score": score,
        "completeness": round(completeness, 4), "validity": round(validity, 4),
        "freshness_at": freshness_at.isoformat() if hasattr(freshness_at, "isoformat") else None,
        "status": status,
        "details": {
            "profile_version": 1, "column_count": len(columns), "null_cells": null_cells,
            "primary_key": primary_key, "primary_key_null_rows": pk_null_rows,
            "primary_key_duplicate_rows": duplicate_rows, "freshness_column": freshness_column,
            "freshness_age_days": freshness_age_days, "column_null_rates": column_null_rates,
            "privacy_notice": "仅保存聚合统计，不保存或返回任何原始字段值。",
        },
    }


def profile_postgres_tables(connector, assets: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Profile all catalog assets over one read-only source connection."""
    results: list[dict] = []
    failures: list[dict] = []
    with connector._connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        for asset in assets:
            try:
                results.append(_profile_table(connection, asset))
            except Exception as exc:  # One bad table must not discard healthy table snapshots.
                connection.rollback()
                failures.append({
                    "asset_id": asset["id"], "table": f"{asset.get('namespace') or 'public'}.{asset['physical_name']}",
                    "code": "quality_query_failed", "error_type": type(exc).__name__,
                })
    return results, failures
