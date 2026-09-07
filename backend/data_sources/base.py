"""Shared contracts for metadata connectors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ConnectorUnavailable(RuntimeError):
    """Raised when a configured connector cannot be used in this runtime."""


@dataclass(frozen=True, slots=True)
class ConnectorConfig:
    kind: str
    endpoint: str = ""
    catalog_name: str = ""
    username: str = ""
    password: str = ""


class MetadataConnector(Protocol):
    def test(self) -> dict: ...
    def discover(self) -> dict: ...
