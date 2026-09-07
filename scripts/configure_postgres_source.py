"""Configure and synchronize a PostgreSQL source without exposing its password."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.catalog.publications import import_discovery_as_draft
from backend.core import db
from backend.data_sources.service import connector_from_row, encrypted_password
from backend.main import init_storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id", default="remote-postgresql-demo")
    parser.add_argument("--name", default="远端 PostgreSQL 测试数仓")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--username", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    password = os.environ.get("DATA_AGENT_SOURCE_PASSWORD", "")
    if not password:
        raise SystemExit("DATA_AGENT_SOURCE_PASSWORD is required")
    init_storage()
    connection = db()
    try:
        admin = connection.execute(
            "SELECT user_id FROM catalog_user_roles WHERE role_id='role-platform-admin' LIMIT 1"
        ).fetchone()
        if not admin:
            raise SystemExit("No platform administrator is provisioned")
        credential = encrypted_password(password)
        connection.execute(
            """INSERT INTO data_sources(
              id,name,kind,endpoint,catalog_name,auth_type,credential,enabled,last_synced_at,username)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,kind=excluded.kind,
              endpoint=excluded.endpoint,catalog_name=excluded.catalog_name,
              auth_type=excluded.auth_type,credential=excluded.credential,
              enabled=excluded.enabled,username=excluded.username""",
            (
                args.source_id, args.name, "postgresql", args.endpoint, args.database,
                "read_only_account", credential, 1, None, args.username,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM data_sources WHERE id=?", (args.source_id,)).fetchone()
        actor_id = admin["user_id"]
    finally:
        connection.close()
    discovery = connector_from_row(row).discover()
    imported = import_discovery_as_draft(args.source_id, discovery, actor_id)
    connection = db()
    try:
        connection.execute("UPDATE data_sources SET last_synced_at=CURRENT_TIMESTAMP WHERE id=?", (args.source_id,))
        connection.commit()
        assets = connection.execute(
            "SELECT COUNT(*) AS count FROM catalog_assets WHERE source_id=?", (args.source_id,)
        ).fetchone()["count"]
        relationships = connection.execute(
            """SELECT COUNT(*) AS count FROM catalog_relationships
            WHERE from_asset_id IN (SELECT id FROM catalog_assets WHERE source_id=?)""",
            (args.source_id,),
        ).fetchone()["count"]
    finally:
        connection.close()
    print(json.dumps({
        "source_id": args.source_id,
        "status": "configured",
        "assets": assets,
        "relationships": relationships,
        "imported": imported["imported"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
