"""Persistence boundary for data-source configuration and credentials."""
from __future__ import annotations

from cryptography.fernet import InvalidToken

from backend.core import secrets
from backend.data_sources.base import ConnectorConfig, ConnectorUnavailable
from backend.data_sources.registry import connector_for


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
