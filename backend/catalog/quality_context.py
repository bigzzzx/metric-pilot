"""Read-only data-quality context for Agent planning and answer disclosure.

This module consumes quality snapshots from an external governance platform
(or this project's lightweight fallback profiler). It does not own rules,
thresholds, scheduling, or alerting.
"""
from __future__ import annotations

import json
from typing import Any

from backend.catalog.service import VIEW_METADATA, can_access
from backend.core import db


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _display_table(asset: dict[str, Any]) -> str:
    namespace = str(asset.get("namespace") or "").strip()
    name = str(asset.get("physical_name") or asset.get("name") or "未知表").strip()
    return f"{namespace}.{name}" if namespace else name


def _risk_reason(snapshot: dict[str, Any]) -> str:
    details = _json(snapshot.get("details_json"), {})
    reasons: list[str] = []
    freshness_age = details.get("freshness_age_days")
    freshness_column = details.get("freshness_column")
    if isinstance(freshness_age, (int, float)) and freshness_age > 7:
        label = f"新鲜度字段 {freshness_column}" if freshness_column else "最新数据"
        reasons.append(f"{label}距今 {freshness_age:g} 天")
    duplicate_rows = int(details.get("primary_key_duplicate_rows") or 0)
    if duplicate_rows:
        reasons.append(f"主键重复 {duplicate_rows} 行")
    null_rows = int(details.get("primary_key_null_rows") or 0)
    if null_rows:
        reasons.append(f"主键为空 {null_rows} 行")
    if int(snapshot.get("row_count") or 0) == 0:
        reasons.append("表当前为空")
    if not reasons:
        score = snapshot.get("score")
        reasons.append(f"质量评分 {float(score):.3f}" if isinstance(score, (int, float)) else "质量状态异常")
    return "；".join(reasons)


def _risk(asset: dict[str, Any], status: str, reason: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "asset_id": asset["id"],
        "source_id": asset["source_id"],
        "table": _display_table(asset),
        "status": status,
        "reason": reason,
        "score": snapshot.get("score") if snapshot else None,
        "observed_at": snapshot.get("observed_at") if snapshot else None,
    }


def _status_label(status: str) -> str:
    return {"warning": "预警", "critical": "严重", "failed": "检测失败"}.get(status, status)


def quality_context_for_schemas(user_id: str, schemas: dict[str, Any] | None) -> dict[str, Any]:
    """Return only authorized quality risks for verified table schemas.

    A missing snapshot is intentionally silent: monitoring rollout should not
    make unmonitored tables look broken. A failed or partial latest run is
    disclosed because verification was attempted but could not complete.
    """

    asset_ids = list(dict.fromkeys(
        str(table.get("asset_id") or "")
        for table in (schemas or {}).get("tables", [])
        if table.get("asset_id")
    ))
    if not asset_ids or not user_id:
        return {"risks": [], "notice": ""}

    con = db()
    try:
        placeholders = ",".join("?" for _ in asset_ids)
        assets = [dict(row) for row in con.execute(
            f"SELECT id,source_id,namespace,physical_name FROM catalog_assets WHERE id IN ({placeholders})",
            asset_ids,
        )]
        assets = [
            asset for asset in assets
            if can_access(user_id, VIEW_METADATA, asset_id=asset["id"], connection=con)
        ]
        if not assets:
            return {"risks": [], "notice": ""}

        latest_runs: dict[str, dict[str, Any]] = {}
        for source_id in {asset["source_id"] for asset in assets}:
            row = con.execute(
                "SELECT id,status,summary_json,completed_at FROM catalog_quality_runs "
                "WHERE source_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if row:
                latest_runs[source_id] = dict(row)

        risks_by_asset: dict[str, dict[str, Any]] = {}
        for asset in assets:
            run = latest_runs.get(asset["source_id"])
            if not run:
                continue
            summary = _json(run.get("summary_json"), {})
            failed_assets = {
                str(item.get("asset_id"))
                for item in summary.get("failures", [])
                if isinstance(item, dict) and item.get("asset_id")
            }
            if run["status"] == "failed":
                risks_by_asset[asset["id"]] = _risk(asset, "failed", "最近一次数据质量检测执行失败，当前无法确认质量")
            elif run["status"] == "partial" and asset["id"] in failed_assets:
                risks_by_asset[asset["id"]] = _risk(asset, "failed", "最近一次数据质量检测未能完成该表的校验")

        for asset in assets:
            if asset["id"] in risks_by_asset:
                continue
            row = con.execute(
                "SELECT row_count,score,freshness_at,completeness,validity,status,details_json,observed_at "
                "FROM catalog_quality_snapshots WHERE asset_id=? ORDER BY observed_at DESC,id DESC LIMIT 1",
                (asset["id"],),
            ).fetchone()
            if not row:
                continue
            snapshot = dict(row)
            if snapshot["status"] in {"warning", "critical"}:
                risks_by_asset[asset["id"]] = _risk(asset, snapshot["status"], _risk_reason(snapshot), snapshot)
    finally:
        con.close()

    risks = list(risks_by_asset.values())
    if not risks:
        return {"risks": [], "notice": ""}
    parts = [f"{item['table']}（{_status_label(item['status'])}：{item['reason']}）" for item in risks]
    return {
        "risks": risks,
        "notice": "数据质量提示（不阻断本次只读查询）：" + "；".join(parts) + "。请结合该风险谨慎解读结果。",
    }


def quality_prompt_context(context: dict[str, Any] | None) -> str:
    """Expose quality facts to the model without treating them as query rules."""

    risks = (context or {}).get("risks") or []
    if not risks:
        return "\nData quality governance context: no known quality risk was found for the verified tables."
    facts = "\n".join(
        f"- {item['table']}: status={item['status']}; reason={item['reason']}"
        for item in risks
    )
    return (
        "\nData quality governance context (trusted facts; it does not change SQL permissions or filters):\n"
        + facts
        + "\nDo not hide these risks. If you answer with data from a listed table, clearly qualify the conclusion."
    )


def append_quality_notice(answer: str, notice: str | None) -> str:
    """Guarantee an explicit user-visible warning even if the model omits it."""

    text = str(answer or "").strip()
    notice = str(notice or "").strip()
    if not notice or notice in text:
        return text
    return f"{text}\n\n{notice}" if text else notice
