"""Persistence boundary for data-source configuration and credentials."""
from __future__ import annotations

import json

from cryptography.fernet import InvalidToken

from backend.core import db, secrets
from backend.data_sources.base import ConnectorConfig, ConnectorUnavailable
from backend.data_sources.registry import connector_for


class DataSourceProtected(RuntimeError):
    pass


class DataSourceInUse(RuntimeError):
    pass


def source_view(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "endpoint": row["endpoint"],
        "catalog_name": row["catalog_name"],
        "auth_type": row["auth_type"],
        "username": row["username"] if "username" in row.keys() else "",
        "credential_configured": bool(row["credential"]),
        "enabled": bool(row["enabled"]),
        "last_synced_at": row["last_synced_at"],
    }


def encrypted_password(password: str, existing: str = "") -> str:
    return secrets().encrypt(password.encode()).decode() if password else existing


def connector_from_row(row):
    try:
        password = secrets().decrypt(row["credential"].encode()).decode() if row["credential"] else ""
    except (InvalidToken, ValueError) as exc:
        raise ConnectorUnavailable("Stored data-source credential cannot be decrypted") from exc
    return connector_for(
        ConnectorConfig(
            kind=row["kind"],
            endpoint=row["endpoint"] or "",
            catalog_name=row["catalog_name"] or "",
            username=row["username"] if "username" in row.keys() else "",
            password=password,
        )
    )


def delete_source(source_id: str, actor_id: str) -> dict:
    """Delete one non-built-in source after proving it has no published assets."""
    if source_id == "local-ecommerce-demo":
        raise DataSourceProtected("内置演示数据源不能删除，如不使用可将其停用")
    connection = db()
    try:
        source = connection.execute("SELECT id,name FROM data_sources WHERE id=?", (source_id,)).fetchone()
        if not source:
            raise LookupError("Data source not found")
        published = connection.execute(
            "SELECT COUNT(*) AS count FROM catalog_assets WHERE source_id=? AND publish_status='published'",
            (source_id,),
        ).fetchone()["count"]
        if published:
            raise DataSourceInUse(
                f"该数据源仍有 {published} 个已发布资产，请先归档或取消发布后再删除"
            )
        asset_ids = [
            row["id"] for row in connection.execute("SELECT id FROM catalog_assets WHERE source_id=?", (source_id,))
        ]
        if asset_ids:
            holders = ",".join("?" for _ in asset_ids)
            node_ids = [
                row["id"] for row in connection.execute(
                    f"SELECT id FROM catalog_lineage_nodes WHERE asset_id IN ({holders})", asset_ids
                )
            ]
            if node_ids:
                node_holders = ",".join("?" for _ in node_ids)
                connection.execute(
                    f"DELETE FROM catalog_lineage_edges WHERE from_node_id IN ({node_holders}) OR to_node_id IN ({node_holders})",
                    [*node_ids, *node_ids],
                )
                connection.execute(f"DELETE FROM catalog_lineage_nodes WHERE id IN ({node_holders})", node_ids)
            connection.execute(
                f"DELETE FROM catalog_relationships WHERE from_asset_id IN ({holders}) OR to_asset_id IN ({holders})",
                [*asset_ids, *asset_ids],
            )
            connection.execute(f"DELETE FROM catalog_quality_snapshots WHERE asset_id IN ({holders})", asset_ids)
            connection.execute(f"DELETE FROM catalog_columns WHERE asset_id IN ({holders})", asset_ids)
            connection.execute(
                f"DELETE FROM catalog_publications WHERE entity_type='asset' AND entity_id IN ({holders})",
                asset_ids,
            )
            connection.execute(f"DELETE FROM catalog_assets WHERE id IN ({holders})", asset_ids)
        connection.execute("DELETE FROM catalog_sync_changes WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM catalog_sync_snapshots WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM catalog_sync_runs WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM catalog_technical_state WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM data_sources WHERE id=?", (source_id,))
        connection.execute(
            "INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)",
            (actor_id, "data_source_deleted", "data_source", source_id, json.dumps({
                "name": source["name"], "draft_assets_deleted": len(asset_ids),
            }, ensure_ascii=False)),
        )
        connection.commit()
        return {"deleted": True, "source_id": source_id, "draft_assets_deleted": len(asset_ids)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
