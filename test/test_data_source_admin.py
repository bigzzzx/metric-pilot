import sqlite3

import pytest

from backend.catalog import service as catalog_service
from backend.catalog.repository import init_catalog_schema
from backend.data_sources import service as source_service
from backend.main import init_storage


def factory_for(path):
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    return connect


def seed_users(factory):
    connection = factory()
    connection.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('admin','admin','hash',1)")
    connection.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('analyst','analyst','hash',1)")
    connection.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','admin')")
    connection.execute("INSERT INTO catalog_group_memberships(group_id,user_id) VALUES ('group-ecommerce-analysts','analyst')")
    connection.commit()
    connection.close()


def seed_source_draft(factory, publish_status="draft"):
    connection = factory()
    connection.execute(
        """INSERT INTO data_sources(id,name,kind,endpoint,catalog_name,auth_type,credential,enabled,last_synced_at,username)
        VALUES ('source-test','测试源','postgresql','db:5432','warehouse','read_only_account','cipher',1,NULL,'reader')"""
    )
    connection.execute(
        """INSERT INTO catalog_assets(
          id,source_id,namespace,physical_name,display_name,domain_id,description,publish_status)
        VALUES ('asset-source-test','source-test','ods','orders','测试订单表','domain-ecommerce','测试草稿',?)""",
        (publish_status,),
    )
    connection.execute(
        """INSERT INTO catalog_columns(id,asset_id,physical_name,display_name,data_type,ordinal,description)
        VALUES ('column-source-test','asset-source-test','order_id','订单编号','bigint',1,'订单编号')"""
    )
    connection.commit()
    connection.close()


def test_admin_sees_drafts_but_analyst_only_sees_published(tmp_path, monkeypatch):
    factory = factory_for(tmp_path / "draft-visibility.db")
    init_storage(factory)
    seed_users(factory)
    seed_source_draft(factory)
    monkeypatch.setattr(catalog_service, "db", factory)

    assert any(item["id"] == "asset-source-test" for item in catalog_service.list_assets("admin"))
    assert not any(item["id"] == "asset-source-test" for item in catalog_service.list_assets("analyst"))
    assert catalog_service.get_asset("admin", "asset-source-test")["publish_status"] == "draft"
    with pytest.raises(LookupError):
        catalog_service.get_asset("analyst", "asset-source-test")
    ecommerce = next(item for item in catalog_service.list_domains("admin") if item["id"] == "domain-ecommerce")
    assert ecommerce["draft_asset_count"] == 1


def test_delete_source_removes_drafts_and_keeps_audit(tmp_path, monkeypatch):
    factory = factory_for(tmp_path / "delete-source.db")
    init_storage(factory)
    seed_users(factory)
    seed_source_draft(factory)
    monkeypatch.setattr(source_service, "db", factory)

    result = source_service.delete_source("source-test", "admin")

    assert result == {"deleted": True, "source_id": "source-test", "draft_assets_deleted": 1}
    connection = factory()
    assert connection.execute("SELECT 1 FROM data_sources WHERE id='source-test'").fetchone() is None
    assert connection.execute("SELECT 1 FROM catalog_assets WHERE id='asset-source-test'").fetchone() is None
    assert connection.execute("SELECT 1 FROM catalog_columns WHERE asset_id='asset-source-test'").fetchone() is None
    assert connection.execute("SELECT action FROM catalog_audit_logs WHERE entity_id='source-test'").fetchone()["action"] == "data_source_deleted"
    connection.close()


def test_delete_source_rejects_published_assets_and_builtin_source(tmp_path, monkeypatch):
    factory = factory_for(tmp_path / "protected-source.db")
    init_storage(factory)
    seed_users(factory)
    seed_source_draft(factory, publish_status="published")
    monkeypatch.setattr(source_service, "db", factory)

    with pytest.raises(source_service.DataSourceInUse):
        source_service.delete_source("source-test", "admin")
    with pytest.raises(source_service.DataSourceProtected):
        source_service.delete_source("local-ecommerce-demo", "admin")


def test_catalog_init_removes_legacy_sqlite_main_namespace_duplicates(tmp_path):
    factory = factory_for(tmp_path / "legacy-duplicate.db")
    init_storage(factory)
    connection = factory()
    connection.execute(
        """INSERT INTO catalog_assets(
          id,source_id,namespace,physical_name,display_name,domain_id,description,publish_status)
        VALUES ('legacy-main-orders','local-ecommerce-demo','main','orders','旧重复订单表','domain-ecommerce','重复草稿','draft')"""
    )
    connection.execute(
        """INSERT INTO catalog_columns(id,asset_id,physical_name,display_name,data_type,ordinal,description)
        VALUES ('legacy-main-column','legacy-main-orders','order_id','订单编号','integer',1,'订单编号')"""
    )
    connection.commit()
    connection.close()

    init_catalog_schema(factory)

    connection = factory()
    assert connection.execute("SELECT 1 FROM catalog_assets WHERE id='legacy-main-orders'").fetchone() is None
    assert connection.execute("SELECT 1 FROM catalog_columns WHERE id='legacy-main-column'").fetchone() is None
    assert connection.execute("SELECT publish_status FROM catalog_assets WHERE id='asset-orders'").fetchone()["publish_status"] == "published"
    connection.close()
