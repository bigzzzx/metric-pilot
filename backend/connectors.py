"""Compatibility imports for callers that used the original connector module."""
from backend.data_sources.base import ConnectorConfig, ConnectorUnavailable, MetadataConnector
from backend.data_sources.registry import DeferredRemoteConnector, connector_for
from backend.data_sources.sqlite import LocalSqliteConnector

__all__ = [
    "ConnectorConfig",
    "ConnectorUnavailable",
    "DeferredRemoteConnector",
    "LocalSqliteConnector",
    "MetadataConnector",
    "connector_for",
]
