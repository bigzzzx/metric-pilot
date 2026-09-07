"""Connector registry kept separate from HTTP routes."""
from __future__ import annotations

from backend.data_sources.base import ConnectorConfig, ConnectorUnavailable, MetadataConnector
from backend.data_sources.postgresql import PostgresConnector
from backend.data_sources.sqlite import LocalSqliteConnector


class DeferredRemoteConnector:
    def __init__(self, kind: str):
        self.kind = kind

    def test(self) -> dict:
        raise ConnectorUnavailable(f"The {self.kind} metadata connector is configured but not installed.")

    def discover(self) -> dict:
        raise ConnectorUnavailable(f"The {self.kind} metadata connector has no implementation yet.")


def connector_for(config: ConnectorConfig | str) -> MetadataConnector:
    resolved = config if isinstance(config, ConnectorConfig) else ConnectorConfig(kind=config)
    if resolved.kind == "local-sqlite":
        return LocalSqliteConnector()
    if resolved.kind == "postgresql":
        return PostgresConnector(resolved)
    return DeferredRemoteConnector(resolved.kind)
