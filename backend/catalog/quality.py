"""Data-quality orchestration, persistence and audit boundaries."""
from __future__ import annotations

import json
import uuid
from typing import Any

from backend.catalog.publications import require_platform_admin
from backend.core import db
from backend.data_sources.base import ConnectorUnavailable
from backend.data_sources.service import connector_from_row


class QualityProfilerUnsupported(RuntimeError):
    pass


def _asset_specs(con, source_id: str) -> list[dict[str, Any]]:
    assets = []
    for row in con.execute("""SELECT id,namespace,physical_name,primary_key,publish_status
      FROM catalog_assets WHERE source_id=? AND lifecycle_status='active' AND asset_type='table'
      ORDER BY namespace,physical_name""", (source_id,)):
        item = dict(row)
        item["primary_key"] = [value.strip() for value in (item["primary_key"] or "").split(",") if value.strip()]
        item["columns"] = [dict(column) for column in con.execute(
            "SELECT physical_name,data_type FROM catalog_columns WHERE asset_id=? ORDER BY ordinal",
            (item["id"],),
        )]
        assets.append(item)
    return assets


def _finish_failed_run(run_id: str, error_code: str) -> None:
    con = db()
    con.execute("""UPDATE catalog_quality_runs SET status='failed',failed_count=asset_count,
      summary_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?""", (
        json.dumps({"error": error_code}, ensure_ascii=False), run_id,
    ))
    con.commit(); con.close()


def run_source_quality(source_id: str, actor_id: str) -> dict[str, Any]:
    """Run deterministic read-only profiling for all active assets in one source."""
    require_platform_admin(actor_id)
    con = db()
    source = con.execute("SELECT * FROM data_sources WHERE id=?", (source_id,)).fetchone()
    if not source:
        con.close()
        raise LookupError("Data source not found")
    if not source["enabled"]:
        con.close()
        raise ConnectorUnavailable("Data source is disabled")
    assets = _asset_specs(con, source_id)
    run_id = str(uuid.uuid4())
    con.execute("""INSERT INTO catalog_quality_runs(id,source_id,actor_id,status,asset_count)
      VALUES (?,?,?,'running',?)""", (run_id, source_id, actor_id, len(assets)))
    con.commit(); con.close()
    if source["kind"] != "postgresql":
        _finish_failed_run(run_id, "profiler_not_supported")
        raise QualityProfilerUnsupported("当前真实质量检测首版支持 PostgreSQL 数据源")

    try:
        from backend.data_sources.postgresql_quality import profile_postgres_tables
        results, failures = profile_postgres_tables(connector_from_row(source), assets)
    except Exception as exc:
        _finish_failed_run(run_id, "source_unavailable")
        raise ConnectorUnavailable("数据质量检测无法连接数据源或执行只读统计") from exc

    counts = {"healthy": 0, "warning": 0, "critical": 0}
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        for result in results:
            counts[result["status"]] += 1
            con.execute("""INSERT INTO catalog_quality_snapshots(
              id,run_id,asset_id,row_count,score,freshness_at,completeness,validity,status,details_json)
              VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                str(uuid.uuid4()), run_id, result["asset_id"], result["row_count"], result["score"],
                result["freshness_at"], result["completeness"], result["validity"], result["status"],
                json.dumps(result["details"], ensure_ascii=False, separators=(",", ":")),
            ))
        status = "completed" if not failures else "partial" if results else "failed"
        summary = {
            "profiled_count": len(results), "failed_count": len(failures),
            "healthy_count": counts["healthy"], "warning_count": counts["warning"],
            "critical_count": counts["critical"], "failures": failures,
            "privacy_notice": "质量检测只保存聚合统计，未读取或持久化原始业务值。",
        }
        con.execute("""UPDATE catalog_quality_runs SET status=?,healthy_count=?,warning_count=?,
          critical_count=?,failed_count=?,summary_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?""", (
            status, counts["healthy"], counts["warning"], counts["critical"], len(failures),
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")), run_id,
        ))
        con.execute(
            "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
            (actor_id, "data_quality_profiled", "data_source", source_id, json.dumps({
                "run_id": run_id, "status": status, **{key: value for key, value in summary.items() if key != "failures"},
            }, ensure_ascii=False)),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {"run_id": run_id, "status": status, "asset_count": len(assets), **summary}


def list_quality_runs(source_id: str, actor_id: str, limit: int = 10) -> list[dict[str, Any]]:
    require_platform_admin(actor_id)
    con = db()
    rows = [dict(row) for row in con.execute(
        "SELECT * FROM catalog_quality_runs WHERE source_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
        (source_id, max(1, min(int(limit), 50))),
    )]
    con.close()
    for row in rows:
        try:
            row["summary"] = json.loads(row.pop("summary_json") or "{}")
        except json.JSONDecodeError:
            row["summary"] = {}
    return rows
