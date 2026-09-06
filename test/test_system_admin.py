import sqlite3

import pytest
from fastapi import HTTPException

from backend.admin import service as admin_service
from backend.catalog import service as catalog_service
from backend.main import init_storage


def _factory(path):
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    return connect


def _seed_users(factory):
    connection = factory()
    connection.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('admin','admin','hash',1)")
    connection.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('analyst','analyst','hash',1)")
    connection.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','admin')")
    connection.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-data-analyst','admin')")
    connection.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-data-analyst','analyst')")
    connection.execute("INSERT INTO catalog_group_memberships(group_id,user_id) VALUES ('group-ecommerce-analysts','analyst')")
    connection.commit(); connection.close()


def test_platform_admin_can_update_user_access_and_audit(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "system-admin.db")
    init_storage(factory)
    _seed_users(factory)
    monkeypatch.setattr(admin_service, "db", factory)
    monkeypatch.setattr(catalog_service, "db", factory)

    updated = admin_service.update_user_access(
        "admin", "analyst",
        role_ids=["role-data-analyst", "role-data-steward"],
        group_ids=["group-marketing-analysts"],
        is_active=True,
    )

    assert updated["role_ids"] == ["role-data-analyst", "role-data-steward"]
    assert updated["group_ids"] == ["group-marketing-analysts"]
    assert admin_service.audit_logs("admin")[0]["action"] == "user_access_updated"


def test_analyst_cannot_access_system_management(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "system-admin-denied.db")
    init_storage(factory)
    _seed_users(factory)
    monkeypatch.setattr(admin_service, "db", factory)
    monkeypatch.setattr(catalog_service, "db", factory)

    with pytest.raises(HTTPException, match="Platform administrator"):
        admin_service.administration_overview("analyst")
