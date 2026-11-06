"""Versioned, review-gated technical metadata synchronization."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from fastapi import HTTPException

from backend.catalog.metadata_semantics import column_semantics, table_semantics
from backend.catalog.publications import require_catalog_admin
from backend.core import db


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, fallback: Any = None) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def _hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _table_key(namespace: str, name: str) -> str:
    return f"{namespace}.{name}" if namespace else name


def normalize_table(item: dict[str, Any]) -> dict[str, Any]:
    namespace = str(item.get("namespace") or "").strip()
    name = str(item.get("name") or "").strip()
    columns = []
    for index, column in enumerate(item.get("columns", []), 1):
        column_name = str(column.get("name") or "").strip()
        if not column_name:
            continue
        columns.append({
            "name": column_name,
            "type": str(column.get("type") or "TEXT").strip(),
            "ordinal": int(column.get("ordinal") or index),
            "description": str(column.get("description") or "").strip(),
            "nullable": bool(column.get("nullable", True)),
            "is_primary_key": bool(column.get("is_primary_key")),
        })
    columns.sort(key=lambda value: (value["ordinal"], value["name"]))
    primary_key = [str(value).strip() for value in item.get("primary_key", []) if str(value).strip()]
    if not primary_key:
        primary_key = [column["name"] for column in columns if column["is_primary_key"]]
    return {
        "namespace": namespace,
        "name": name,
        "description": str(item.get("description") or "").strip(),
        "primary_key": primary_key,
        "columns": columns,
    }


def normalize_relationships(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in items:
        value = {
            "from_namespace": str(item.get("from_namespace") or "").strip(),
            "from_table": str(item.get("from_table") or "").strip(),
            "from_column": str(item.get("from_column") or "").strip(),
            "to_namespace": str(item.get("to_namespace") or "").strip(),
            "to_table": str(item.get("to_table") or "").strip(),
            "to_column": str(item.get("to_column") or "").strip(),
            "relation": str(item.get("relation") or "foreign_key"),
            "cardinality": str(item.get("cardinality") or "many_to_one"),
            "recommended_join": str(item.get("recommended_join") or "LEFT JOIN"),
            "description": str(item.get("description") or "").strip(),
            "verified": bool(item.get("verified")),
        }
        if all(value[field] for field in ("from_table", "from_column", "to_table", "to_column")):
            normalized.append(value)
    return sorted(normalized, key=lambda value: (
        value["from_namespace"], value["from_table"], value["from_column"],
        value["to_namespace"], value["to_table"], value["to_column"],
    ))


def normalize_discovery(discovery: dict[str, Any]) -> dict[str, Any]:
    tables = [normalize_table(item) for item in discovery.get("tables", [])]
    tables = sorted((item for item in tables if item["name"]), key=lambda item: (item["namespace"], item["name"]))
    return {"tables": tables, "relationships": normalize_relationships(discovery.get("relationships", []))}


def _catalog_table_payload(con, asset) -> dict[str, Any]:
    columns = [{
        "name": row["physical_name"], "type": row["data_type"], "ordinal": int(row["ordinal"]),
        "description": row["description"] or "", "nullable": bool(row["nullable"]),
        "is_primary_key": bool(row["is_primary_key"]),
    } for row in con.execute(
        "SELECT * FROM catalog_columns WHERE asset_id=? ORDER BY ordinal,physical_name", (asset["id"],)
    )]
    primary_key = [value.strip() for value in str(asset["primary_key"] or "").split(",") if value.strip()]
    return {
        "namespace": asset["namespace"] or "", "name": asset["physical_name"],
        "description": asset["description"] or "", "primary_key": primary_key, "columns": columns,
    }


def _catalog_relationship_payload(con, source_id: str) -> list[dict[str, Any]]:
    rows = con.execute("""SELECT r.*,fa.namespace AS from_namespace,fa.physical_name AS from_table,
      ta.namespace AS to_namespace,ta.physical_name AS to_table
      FROM catalog_relationships r
      JOIN catalog_assets fa ON fa.id=r.from_asset_id
      JOIN catalog_assets ta ON ta.id=r.to_asset_id
      WHERE fa.source_id=? AND ta.source_id=?""", (source_id, source_id))
    return normalize_relationships([dict(row) for row in rows])


def _state(con, source_id: str, entity_type: str, entity_key: str) -> dict[str, Any] | None:
    row = con.execute(
        "SELECT * FROM catalog_technical_state WHERE source_id=? AND entity_type=? AND entity_key=?",
        (source_id, entity_type, entity_key),
    ).fetchone()
    if not row:
        return None
    return {**dict(row), "payload": _load(row["payload_json"], {})}


def _write_state(con, source_id: str, entity_type: str, entity_key: str, payload: Any, actor_id: str) -> None:
    current = _state(con, source_id, entity_type, entity_key)
    version = int(current["version"]) + 1 if current else 1
    con.execute("""INSERT INTO catalog_technical_state(
      source_id,entity_type,entity_key,version,content_hash,payload_json,updated_by,updated_at)
      VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
      ON CONFLICT(source_id,entity_type,entity_key) DO UPDATE SET
      version=excluded.version,content_hash=excluded.content_hash,payload_json=excluded.payload_json,
      updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
      (source_id, entity_type, entity_key, version, _hash(payload), _dump(payload), actor_id),
    )


def _seed_baseline(con, source_id: str, actor_id: str) -> None:
    for asset in con.execute("SELECT * FROM catalog_assets WHERE source_id=?", (source_id,)):
        key = _table_key(asset["namespace"] or "", asset["physical_name"])
        if not _state(con, source_id, "table", key):
            _write_state(con, source_id, "table", key, _catalog_table_payload(con, asset), actor_id)
    if not _state(con, source_id, "relationships", "__all__"):
        _write_state(con, source_id, "relationships", "__all__", _catalog_relationship_payload(con, source_id), actor_id)


def _table_summary(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    before_columns = {item["name"]: item for item in (before or {}).get("columns", [])}
    after_columns = {item["name"]: item for item in (after or {}).get("columns", [])}
    return {
        "added_columns": sorted(after_columns.keys() - before_columns.keys()),
        "removed_columns": sorted(before_columns.keys() - after_columns.keys()),
        "type_changes": [{
            "column": name, "before": before_columns[name]["type"], "after": after_columns[name]["type"],
        } for name in sorted(before_columns.keys() & after_columns.keys())
          if before_columns[name]["type"] != after_columns[name]["type"]],
        "primary_key_changed": (before or {}).get("primary_key") != (after or {}).get("primary_key"),
        "comment_changed": (before or {}).get("description") != (after or {}).get("description"),
    }


def _record_pending(
    con, run_id: str, source_id: str, entity_type: str, entity_key: str,
    asset_id: str | None, change_type: str, before: Any, after: Any, summary: dict[str, Any],
) -> bool:
    fingerprint = _hash({"change_type": change_type, "before": before, "after": after})
    current = con.execute("""SELECT id,content_hash FROM catalog_sync_changes
      WHERE source_id=? AND entity_type=? AND entity_key=? AND status='pending'
      ORDER BY created_at DESC LIMIT 1""", (source_id, entity_type, entity_key)).fetchone()
    if current and current["content_hash"] == fingerprint:
        return False
    if current:
        con.execute(
            "UPDATE catalog_sync_changes SET status='superseded',decided_at=CURRENT_TIMESTAMP,decision_reason='后续采集发现了更新版本' WHERE id=?",
            (current["id"],),
        )
    con.execute("""INSERT INTO catalog_sync_changes(
      id,run_id,source_id,entity_type,entity_key,asset_id,change_type,status,content_hash,
      before_json,after_json,summary_json)
      VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?)""", (
        str(uuid.uuid4()), run_id, source_id, entity_type, entity_key, asset_id, change_type,
        fingerprint, _dump(before) if before is not None else None,
        _dump(after) if after is not None else None, _dump(summary),
    ))
    return True


def record_discovery(source_id: str, discovery: dict[str, Any], actor_id: str) -> dict[str, Any]:
    """Persist an immutable discovery snapshot and create reviewable diffs."""

    require_catalog_admin(actor_id)
    normalized = normalize_discovery(discovery)
    run_id = str(uuid.uuid4())
    snapshot_hash = _hash(normalized)
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        if not con.execute("SELECT 1 FROM data_sources WHERE id=?", (source_id,)).fetchone():
            raise LookupError("Data source not found")
        _seed_baseline(con, source_id, actor_id)
        snapshot_version = int(con.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS version FROM catalog_sync_snapshots WHERE source_id=?",
            (source_id,),
        ).fetchone()["version"])
        con.execute("""INSERT INTO catalog_sync_runs(
          id,source_id,actor_id,status,snapshot_version,snapshot_hash,table_count,relationship_count)
          VALUES (?,?,?,'pending',?,?,?,?)""", (
            run_id, source_id, actor_id, snapshot_version, snapshot_hash,
            len(normalized["tables"]), len(normalized["relationships"]),
        ))
        con.execute("""INSERT INTO catalog_sync_snapshots(
          id,run_id,source_id,version,snapshot_hash,payload_json)
          VALUES (?,?,?,?,?,?)""", (
            str(uuid.uuid4()), run_id, source_id, snapshot_version, snapshot_hash, _dump(normalized),
        ))

        discovered = {_table_key(item["namespace"], item["name"]): item for item in normalized["tables"]}
        assets = {
            _table_key(row["namespace"] or "", row["physical_name"]): row
            for row in con.execute("SELECT * FROM catalog_assets WHERE source_id=?", (source_id,))
        }
        counts = {"added": 0, "changed": 0, "removed": 0, "relationships": 0}
        created = 0
        for key, after in discovered.items():
            baseline = _state(con, source_id, "table", key)
            before = baseline["payload"] if baseline else None
            asset = assets.get(key)
            if before and before.get("removed"):
                change_type = "table_restored"
            elif before is None:
                change_type = "table_added"
            elif _hash(before) != _hash(after):
                change_type = "table_changed"
            else:
                continue
            bucket = "added" if change_type in {"table_added", "table_restored"} else "changed"
            counts[bucket] += 1
            created += int(_record_pending(
                con, run_id, source_id, "table", key,
                asset["id"] if asset else str(uuid.uuid5(uuid.NAMESPACE_URL, f"data-agent:{source_id}:{key}")),
                change_type, before, after, _table_summary(before, after),
            ))

        for key, asset in assets.items():
            baseline = _state(con, source_id, "table", key)
            before = baseline["payload"] if baseline else _catalog_table_payload(con, asset)
            if key in discovered or before.get("removed") or asset["lifecycle_status"] == "archived":
                continue
            counts["removed"] += 1
            created += int(_record_pending(
                con, run_id, source_id, "table", key, asset["id"], "table_removed",
                before, None, _table_summary(before, None),
            ))

        relation_state = _state(con, source_id, "relationships", "__all__")
        before_relations = relation_state["payload"] if relation_state else []
        if _hash(before_relations) != _hash(normalized["relationships"]):
            counts["relationships"] = 1
            created += int(_record_pending(
                con, run_id, source_id, "relationships", "__all__", None, "relationships_changed",
                before_relations, normalized["relationships"], {
                    "before_count": len(before_relations), "after_count": len(normalized["relationships"]),
                },
            ))

        pending = con.execute(
            "SELECT COUNT(*) AS count FROM catalog_sync_changes WHERE source_id=? AND status='pending'",
            (source_id,),
        ).fetchone()["count"]
        status = "pending" if pending else "no_changes"
        summary = {**counts, "new_change_records": created}
        con.execute("""UPDATE catalog_sync_runs SET status=?,added_count=?,changed_count=?,removed_count=?,
          relationship_change_count=?,pending_count=?,summary_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?""", (
            status, counts["added"], counts["changed"], counts["removed"], counts["relationships"],
            pending, _dump(summary), run_id,
        ))
        con.execute(
            "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
            (actor_id, "metadata_sync_discovered", "data_source", source_id, _dump({
                "run_id": run_id, "snapshot_version": snapshot_version, **summary, "pending_count": pending,
            })),
        )
        con.commit()
        return {
            "run_id": run_id, "snapshot_version": snapshot_version, "sync_status": status,
            "changes": counts, "pending_count": pending, "new_change_records": created,
            "message": "采集完成，变更已进入审核队列。" if pending else "采集完成，未发现元数据变化。",
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _insert_or_update_table(con, source_id: str, asset_id: str, payload: dict[str, Any], actor_id: str) -> tuple[str, bool]:
    asset = con.execute("SELECT * FROM catalog_assets WHERE id=?", (asset_id,)).fetchone()
    was_published = bool(asset and asset["publish_status"] == "published")
    description = payload["description"] or "数据源未提供表说明。"
    primary_key = ", ".join(payload["primary_key"])
    if asset:
        new_status = "draft" if asset["lifecycle_status"] == "archived" else asset["publish_status"]
        new_description = asset["description"]
        if not new_description or new_description.startswith(("数据源未提供", "技术元数据待")):
            new_description = description
        con.execute("""UPDATE catalog_assets SET namespace=?,physical_name=?,description=?,primary_key=?,
          lifecycle_status='active',publish_status=?,catalog_version=catalog_version+1,
          source_updated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (
            payload["namespace"], payload["name"], new_description, primary_key, new_status, asset_id,
        ))
    else:
        semantics = table_semantics(description, payload["namespace"])
        layer = semantics["warehouse_layer"]
        normalized_stage = {"ODS": "raw", "DWD": "detail", "DWS": "summary", "ADS": "application", "DIM": "dimension"}.get(layer, "unknown")
        con.execute("""INSERT INTO catalog_assets(
          id,source_id,namespace,physical_name,display_name,asset_type,domain_id,warehouse_layer,
          normalized_stage,description,aliases_json,data_grain,primary_key,owner_group_id,sensitivity,
          publish_status,certification_status,catalog_version,source_updated_at)
          VALUES (?,?,?,?,?,'table',?,?,?,?,?,?,?,?,?,'draft','draft',1,CURRENT_TIMESTAMP)""", (
            asset_id, source_id, payload["namespace"], payload["name"], payload["name"],
            semantics["domain_id"], layer, normalized_stage, description,
            _dump(semantics["aliases"]), semantics["data_grain"], primary_key,
            semantics["owner_group_id"], semantics["sensitivity"],
        ))

    existing = {
        row["physical_name"]: row
        for row in con.execute("SELECT * FROM catalog_columns WHERE asset_id=?", (asset_id,))
    }
    discovered_names = set()
    for column in payload["columns"]:
        name = column["name"]
        discovered_names.add(name)
        old = existing.get(name)
        description = column["description"] or "数据源未提供字段说明。"
        if old:
            effective_description = old["description"]
            if not effective_description or effective_description.startswith(("数据源未提供", "待补充")):
                effective_description = description
            con.execute("""UPDATE catalog_columns SET data_type=?,ordinal=?,description=?,nullable=?,
              is_primary_key=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (
                column["type"], column["ordinal"], effective_description, int(column["nullable"]),
                int(column["is_primary_key"]), old["id"],
            ))
        else:
            metadata = column_semantics(description, name, column["type"], column["is_primary_key"])
            column_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"data-agent-column:{asset_id}:{name}"))
            con.execute("""INSERT INTO catalog_columns(
              id,asset_id,physical_name,display_name,data_type,ordinal,description,aliases_json,
              semantic_role,nullable,is_primary_key,sensitivity,classification,masking_strategy)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                column_id, asset_id, name, metadata["display_name"], column["type"], column["ordinal"],
                description, _dump(metadata["aliases"]), metadata["semantic_role"], int(column["nullable"]),
                int(column["is_primary_key"]), metadata["sensitivity"], metadata["classification"],
                metadata["masking_strategy"],
            ))
    removed = [row["id"] for name, row in existing.items() if name not in discovered_names]
    if removed:
        con.executemany("DELETE FROM catalog_columns WHERE id=?", [(value,) for value in removed])
    con.execute(
        "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
        (actor_id, "technical_metadata_applied", "asset", asset_id, _dump({"source_id": source_id})),
    )
    return asset_id, was_published


def _archive_table(con, source_id: str, asset_id: str, actor_id: str) -> bool:
    asset = con.execute("SELECT * FROM catalog_assets WHERE id=? AND source_id=?", (asset_id, source_id)).fetchone()
    if not asset:
        raise LookupError("Catalog asset not found")
    was_published = asset["publish_status"] == "published"
    con.execute("""UPDATE catalog_assets SET lifecycle_status='archived',publish_status='archived',
      catalog_version=catalog_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (asset_id,))
    con.execute(
        "UPDATE catalog_relationships SET status='archived' WHERE from_asset_id=? OR to_asset_id=?",
        (asset_id, asset_id),
    )
    con.execute(
        "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
        (actor_id, "technical_metadata_archived", "asset", asset_id, _dump({"source_id": source_id})),
    )
    return was_published


def _apply_relationships(con, source_id: str, relationships: list[dict[str, Any]], actor_id: str) -> set[str]:
    assets = {
        (_table_key(row["namespace"] or "", row["physical_name"])): row
        for row in con.execute("SELECT * FROM catalog_assets WHERE source_id=?", (source_id,))
    }
    asset_ids = {row["id"] for row in assets.values()}
    if asset_ids:
        holders = ",".join("?" for _ in asset_ids)
        con.execute(
            f"DELETE FROM catalog_relationships WHERE from_asset_id IN ({holders}) AND to_asset_id IN ({holders})",
            [*asset_ids, *asset_ids],
        )
    affected: set[str] = set()
    for relation in relationships:
        from_asset = assets.get(_table_key(relation["from_namespace"], relation["from_table"]))
        to_asset = assets.get(_table_key(relation["to_namespace"], relation["to_table"]))
        if not from_asset or not to_asset:
            continue
        affected.update((from_asset["id"], to_asset["id"]))
        relation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, (
            f"data-agent-relation:{source_id}:{relation['from_namespace']}:{relation['from_table']}:"
            f"{relation['from_column']}:{relation['to_namespace']}:{relation['to_table']}:{relation['to_column']}"
        )))
        status = "published" if from_asset["publish_status"] == to_asset["publish_status"] == "published" else "draft"
        join_condition = (
            f"{relation['from_namespace']}.{relation['from_table']}.{relation['from_column']} = "
            f"{relation['to_namespace']}.{relation['to_table']}.{relation['to_column']}"
        )
        con.execute("""INSERT INTO catalog_relationships(
          id,from_asset_id,from_column,to_asset_id,to_column,relation_type,cardinality,
          recommended_join,join_condition,description,verified,status)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            relation_id, from_asset["id"], relation["from_column"], to_asset["id"], relation["to_column"],
            relation["relation"], relation["cardinality"], relation["recommended_join"], join_condition,
            relation["description"] or "从数据源主外键约束自动采集。", int(relation["verified"]), status,
        ))
    if affected:
        con.executemany(
            "UPDATE catalog_assets SET catalog_version=catalog_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [(asset_id,) for asset_id in affected],
        )
    con.execute(
        "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
        (actor_id, "technical_relationships_applied", "data_source", source_id, _dump({"count": len(relationships)})),
    )
    return affected


def list_sync_runs(source_id: str, actor_id: str, limit: int = 20) -> list[dict[str, Any]]:
    require_catalog_admin(actor_id)
    con = db()
    rows = [dict(row) for row in con.execute(
        "SELECT * FROM catalog_sync_runs WHERE source_id=? ORDER BY snapshot_version DESC LIMIT ?",
        (source_id, max(1, min(limit, 100))),
    )]
    con.close()
    for row in rows:
        row["summary"] = _load(row.pop("summary_json"), {})
    return rows


def list_changes(source_id: str, actor_id: str, status: str | None = "pending") -> list[dict[str, Any]]:
    require_catalog_admin(actor_id)
    con = db()
    if status:
        ordering = "created_at,id" if status == "pending" else "created_at DESC,id DESC"
        rows = con.execute(
            f"SELECT * FROM catalog_sync_changes WHERE source_id=? AND status=? ORDER BY {ordering}",
            (source_id, status),
        )
    else:
        rows = con.execute(
            "SELECT * FROM catalog_sync_changes WHERE source_id=? ORDER BY created_at DESC,id", (source_id,)
        )
    result = [dict(row) for row in rows]
    con.close()
    for row in result:
        row["before"] = _load(row.pop("before_json"), None)
        row["after"] = _load(row.pop("after_json"), None)
        row["summary"] = _load(row.pop("summary_json"), {})
    return result


def decide_change(
    source_id: str, change_id: str, actor_id: str, decision: str, reason: str = "",
    *, refresh_index: bool = True,
) -> dict[str, Any]:
    """Approve or reject one pending change and refresh only affected RAG assets."""

    require_catalog_admin(actor_id)
    if decision not in {"approve", "reject"}:
        raise HTTPException(422, "decision 必须是 approve 或 reject")
    con = db()
    affected: set[str] = set()
    refresh_required = False
    try:
        con.execute("BEGIN IMMEDIATE")
        change = con.execute(
            "SELECT * FROM catalog_sync_changes WHERE id=? AND source_id=?", (change_id, source_id)
        ).fetchone()
        if not change:
            raise LookupError("Metadata change not found")
        if change["status"] != "pending":
            raise HTTPException(409, "该变更已经处理，请刷新后重试。")
        before = _load(change["before_json"], None)
        after = _load(change["after_json"], None)
        if decision == "approve":
            if change["change_type"] in {"table_added", "table_changed", "table_restored"}:
                asset_id, was_published = _insert_or_update_table(
                    con, source_id, change["asset_id"], after, actor_id
                )
                affected.add(asset_id)
                refresh_required = refresh_required or was_published
                _write_state(con, source_id, "table", change["entity_key"], after, actor_id)
            elif change["change_type"] == "table_removed":
                refresh_required = _archive_table(con, source_id, change["asset_id"], actor_id)
                affected.add(change["asset_id"])
                _write_state(con, source_id, "table", change["entity_key"], {
                    "removed": True, "previous": before,
                }, actor_id)
            elif change["change_type"] == "relationships_changed":
                affected.update(_apply_relationships(con, source_id, after or [], actor_id))
                refresh_required = any(
                    row["publish_status"] == "published"
                    for row in con.execute(
                        f"SELECT publish_status FROM catalog_assets WHERE id IN ({','.join('?' for _ in affected)})",
                        list(affected),
                    )
                ) if affected else False
                _write_state(con, source_id, "relationships", "__all__", after or [], actor_id)
        con.execute("""UPDATE catalog_sync_changes SET status=?,decided_by=?,decision_reason=?,
          decided_at=CURRENT_TIMESTAMP WHERE id=?""", (
            "approved" if decision == "approve" else "rejected", actor_id, reason.strip(), change_id,
        ))
        pending = con.execute(
            "SELECT COUNT(*) AS count FROM catalog_sync_changes WHERE source_id=? AND status='pending'",
            (source_id,),
        ).fetchone()["count"]
        con.execute("""UPDATE catalog_sync_runs SET pending_count=?,
          status=CASE WHEN ?=0 AND status='pending' THEN 'applied' ELSE status END
          WHERE source_id=? AND status='pending'""", (pending, pending, source_id))
        con.execute(
            "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
            (actor_id, f"metadata_change_{decision}", "metadata_change", change_id, _dump({
                "source_id": source_id, "change_type": change["change_type"], "reason": reason.strip(),
            })),
        )
        if decision == 'approve' and refresh_required:
            from backend.index_worker import enqueue_catalog
            enqueue_catalog(con)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    indexed = None
    if decision == "approve" and refresh_required and refresh_index:
        from backend.catalog.vector_index import refresh_catalog_assets
        indexed = refresh_catalog_assets(affected)
    return {
        "change_id": change_id, "status": "approved" if decision == "approve" else "rejected",
        "affected_asset_ids": sorted(affected), "refresh_required": refresh_required,
        "rag_indexed": indexed,
        "message": (
            "变更已批准并应用；相关 RAG 文档已增量刷新。"
            if decision == "approve" and refresh_required and indexed
            else "变更已批准并应用。" if decision == "approve"
            else "变更已拒绝，当前目录保持不变。"
        ),
    }


def decide_changes(
    source_id: str, change_ids: list[str], actor_id: str, decision: str, reason: str = "",
) -> dict[str, Any]:
    """Process a bounded batch and refresh the union of affected RAG assets once."""

    require_catalog_admin(actor_id)
    results = []
    affected: set[str] = set()
    refresh_required = False
    for change_id in dict.fromkeys(change_ids):
        try:
            result = decide_change(
                source_id, change_id, actor_id, decision, reason, refresh_index=False,
            )
            results.append(result)
            affected.update(result["affected_asset_ids"])
            refresh_required = refresh_required or bool(result["refresh_required"])
        except LookupError:
            results.append({"change_id": change_id, "status": "failed", "message": "变更不存在。"})
        except HTTPException as exc:
            results.append({
                "change_id": change_id, "status": "failed", "status_code": exc.status_code,
                "message": str(exc.detail),
            })
    indexed = None
    if decision == "approve" and refresh_required:
        from backend.catalog.vector_index import refresh_catalog_assets
        indexed = refresh_catalog_assets(affected)
    completed = sum(item["status"] in {"approved", "rejected"} for item in results)
    return {
        "processed_count": completed, "failed_count": len(results) - completed,
        "affected_asset_ids": sorted(affected), "rag_indexed": indexed, "results": results,
        "message": f"批量处理完成：成功 {completed} 个，失败 {len(results) - completed} 个。",
    }


def rollback_change(source_id: str, change_id: str, actor_id: str) -> dict[str, Any]:
    """Rollback only the latest approved change for one technical entity."""

    require_catalog_admin(actor_id)
    con = db()
    affected: set[str] = set()
    refresh_required = False
    try:
        con.execute("BEGIN IMMEDIATE")
        change = con.execute(
            "SELECT rowid,* FROM catalog_sync_changes WHERE id=? AND source_id=?",
            (change_id, source_id),
        ).fetchone()
        if not change:
            raise LookupError("Metadata change not found")
        if change["status"] != "approved":
            raise HTTPException(409, "仅已批准且尚未回滚的变更可以回滚。")
        latest = con.execute("""SELECT id FROM catalog_sync_changes
          WHERE source_id=? AND entity_type=? AND entity_key=? AND status='approved'
          ORDER BY decided_at DESC,rowid DESC LIMIT 1""", (
            source_id, change["entity_type"], change["entity_key"],
        )).fetchone()
        if not latest or latest["id"] != change_id:
            raise HTTPException(409, "只能回滚该资产最近一次已批准变更。")
        before = _load(change["before_json"], None)
        after = _load(change["after_json"], None)
        change_type = change["change_type"]
        if change_type == "table_added":
            asset = con.execute(
                "SELECT publish_status FROM catalog_assets WHERE id=?", (change["asset_id"],)
            ).fetchone()
            if asset and asset["publish_status"] == "published":
                raise HTTPException(409, "新增资产已发布，请先在资产发布管理中撤回后再回滚。")
            if asset:
                con.execute(
                    "DELETE FROM catalog_relationships WHERE from_asset_id=? OR to_asset_id=?",
                    (change["asset_id"], change["asset_id"]),
                )
                con.execute("DELETE FROM catalog_columns WHERE asset_id=?", (change["asset_id"],))
                con.execute("DELETE FROM catalog_assets WHERE id=?", (change["asset_id"],))
            con.execute(
                "DELETE FROM catalog_technical_state WHERE source_id=? AND entity_type='table' AND entity_key=?",
                (source_id, change["entity_key"]),
            )
        elif change_type in {"table_changed", "table_restored"}:
            if before and before.get("removed"):
                refresh_required = _archive_table(con, source_id, change["asset_id"], actor_id)
                _write_state(con, source_id, "table", change["entity_key"], before, actor_id)
            else:
                asset_id, was_published = _insert_or_update_table(
                    con, source_id, change["asset_id"], before, actor_id
                )
                affected.add(asset_id)
                refresh_required = refresh_required or was_published
                _write_state(con, source_id, "table", change["entity_key"], before, actor_id)
        elif change_type == "table_removed":
            asset_id, _ = _insert_or_update_table(
                con, source_id, change["asset_id"], before, actor_id
            )
            prior_publication = con.execute(
                "SELECT 1 FROM catalog_publications WHERE entity_type='asset' AND entity_id=? AND status='published' LIMIT 1",
                (asset_id,),
            ).fetchone()
            if prior_publication:
                con.execute("""UPDATE catalog_assets SET lifecycle_status='active',publish_status='published',
                  updated_at=CURRENT_TIMESTAMP WHERE id=?""", (asset_id,))
                refresh_required = True
            affected.add(asset_id)
            _write_state(con, source_id, "table", change["entity_key"], before, actor_id)
        elif change_type == "relationships_changed":
            affected.update(_apply_relationships(con, source_id, before or [], actor_id))
            refresh_required = bool(affected)
            _write_state(con, source_id, "relationships", "__all__", before or [], actor_id)
        else:
            raise HTTPException(409, "该变更类型不支持回滚。")
        if change["asset_id"]:
            affected.add(change["asset_id"])
        con.execute("""UPDATE catalog_sync_changes SET status='rolled_back',decided_by=?,
          decision_reason='管理员回滚',decided_at=CURRENT_TIMESTAMP WHERE id=?""", (actor_id, change_id))
        con.execute(
            "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
            (actor_id, "metadata_change_rollback", "metadata_change", change_id, _dump({
                "source_id": source_id, "change_type": change_type,
            })),
        )
        if refresh_required:
            from backend.index_worker import enqueue_catalog
            enqueue_catalog(con)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    indexed = None
    if refresh_required and affected:
        from backend.catalog.vector_index import refresh_catalog_assets
        indexed = refresh_catalog_assets(affected)
    return {
        "change_id": change_id, "status": "rolled_back", "affected_asset_ids": sorted(affected),
        "rag_indexed": indexed, "message": "最近一次元数据变更已回滚。",
    }
