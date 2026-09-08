"""Metadata discovery for the bundled SQLite demo warehouse."""
from __future__ import annotations

import sqlite3

from backend.core import DEMO_DB


class LocalSqliteConnector:
    def test(self) -> dict:
        connection = sqlite3.connect(DEMO_DB)
        try:
            connection.execute("SELECT 1").fetchone()
            return {"status": "connected", "message": "Local SQLite metadata source is reachable."}
        finally:
            connection.close()

    def discover(self) -> dict:
        connection = sqlite3.connect(DEMO_DB)
        try:
            table_names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            tables = []
            for name in table_names:
                columns = [
                    {
                        "name": row[1],
                        "type": row[2] or "TEXT",
                        "nullable": not bool(row[3]),
                        "is_primary_key": bool(row[5]),
                    }
                    for row in connection.execute(f'PRAGMA table_info("{name}")')
                ]
                primary_key = [column["name"] for column in columns if column["is_primary_key"]]
                tables.append(
                    {
                        "namespace": "",
                        "name": name,
                        "description": "",
                        "primary_key": primary_key,
                        "columns": columns,
                    }
                )
            return {
                "status": "discovered",
                "tables": tables,
                "relationships": [],
                "table_count": len(tables),
            }
        finally:
            connection.close()
