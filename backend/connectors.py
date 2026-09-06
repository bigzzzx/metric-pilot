"""Metadata-only connectors. Data queries remain in separately guarded tools."""
import sqlite3
from typing import Protocol

from backend.core import DEMO_DB


class ConnectorUnavailable(RuntimeError):
    pass


class MetadataConnector(Protocol):
    def test(self) -> dict: ...
    def discover(self) -> dict: ...


class LocalSqliteConnector:
    """Working reference connector used by the local demo source."""

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
            table_names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            tables = []
            for name in table_names:
                columns = [
                    {"name": row[1], "type": row[2] or "TEXT", "is_primary_key": bool(row[5])}
                    for row in connection.execute(f"PRAGMA table_info({name})")
                ]
                tables.append({"name": name, "columns": columns})
            return {"status": "discovered", "tables": tables, "table_count": len(tables)}
        finally:
            connection.close()


class DeferredRemoteConnector:
    """Stable extension point for cloud and self-hosted adapters not installed in the demo."""

    def __init__(self, kind: str):
        self.kind = kind

    def test(self) -> dict:
        raise ConnectorUnavailable(f"The {self.kind} metadata connector is configured but not installed in this local demo.")

    def discover(self) -> dict:
        raise ConnectorUnavailable(f"The {self.kind} metadata connector has no implementation yet. Add its SDK/driver, then implement MetadataConnector.discover().")


def connector_for(kind: str) -> MetadataConnector:
    if kind == "local-sqlite":
        return LocalSqliteConnector()
    return DeferredRemoteConnector(kind)
