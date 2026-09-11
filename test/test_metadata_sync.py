import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.metadata_sync import router
from backend.auth import require_user
from backend.catalog import publications, service, sync, vector_index
from backend.main import init_storage
from test.test_data_source_admin import factory_for, seed_source_draft, seed_users


def configured_catalog(tmp_path, monkeypatch, *, published=False):
    factory = factory_for(tmp_path / "metadata-sync.db")
    init_storage(factory)
    seed_users(factory)
    seed_source_draft(factory, "published" if published else "draft")
    monkeypatch.setattr(sync, "db", factory)
    monkeypatch.setattr(publications, "db", factory)
    monkeypatch.setattr(service, "db", factory)
    return factory


def orders_discovery(*, data_type="bigint", extra_columns=()):
    columns = [{
        "name": "order_id", "type": data_type, "ordinal": 1, "description": "订单编号",
        "nullable": True, "is_primary_key": False,
    }, *extra_columns]
    return {
        "table_count": 1,
        "relationship_count": 0,
        "tables": [{
            "namespace": "ods", "name": "orders", "description": "测试草稿",
            "primary_key": [], "columns": columns,
        }],
        "relationships": [],
    }


def test_discovery_snapshot_with_no_change_is_versioned_without_pending_diff(tmp_path, monkeypatch):
    factory = configured_catalog(tmp_path, monkeypatch)
    result = sync.record_discovery("source-test", orders_discovery(), "admin")
    assert result["snapshot_version"] == 1
    assert result["sync_status"] == "no_changes"
    assert result["pending_count"] == 0

    second = sync.record_discovery("source-test", orders_discovery(), "admin")
    assert second["snapshot_version"] == 2
    assert sync.list_sync_runs("source-test", "admin")[0]["snapshot_version"] == 2
    connection = factory()
    assert connection.execute(
        "SELECT COUNT(*) FROM catalog_sync_snapshots WHERE source_id='source-test'"
    ).fetchone()[0] == 2
    connection.close()


def test_changed_table_waits_for_approval_then_preserves_semantics(tmp_path, monkeypatch):
    factory = configured_catalog(tmp_path, monkeypatch, published=True)
    sync.record_discovery("source-test", orders_discovery(), "admin")
    changed = orders_discovery(data_type="numeric(20,0)", extra_columns=[{
        "name": "source_system", "type": "text", "ordinal": 2, "description": "来源系统",
        "nullable": False, "is_primary_key": False,
    }])
    result = sync.record_discovery("source-test", changed, "admin")
    assert result["changes"]["changed"] == 1
    pending = sync.list_changes("source-test", "admin")
    assert len(pending) == 1
    assert pending[0]["summary"]["added_columns"] == ["source_system"]
    assert pending[0]["summary"]["type_changes"][0]["column"] == "order_id"

    connection = factory()
    assert connection.execute(
        "SELECT data_type FROM catalog_columns WHERE id='column-source-test'"
    ).fetchone()[0] == "bigint"
    connection.close()

    refreshed = []
    monkeypatch.setattr(vector_index, "refresh_catalog_assets", lambda ids: refreshed.append(set(ids)) or True)
    applied = sync.decide_change("source-test", pending[0]["id"], "admin", "approve", "类型变更已核对")
    assert applied["rag_indexed"] is True
    assert refreshed == [{"asset-source-test"}]
    connection = factory()
    asset = connection.execute(
        "SELECT display_name,publish_status,catalog_version FROM catalog_assets WHERE id='asset-source-test'"
    ).fetchone()
    assert tuple(asset) == ("测试订单表", "published", 2)
    columns = connection.execute(
        "SELECT physical_name,data_type FROM catalog_columns WHERE asset_id='asset-source-test' ORDER BY ordinal"
    ).fetchall()
    assert [tuple(row) for row in columns] == [
        ("order_id", "numeric(20,0)"), ("source_system", "text"),
    ]
    connection.close()


def test_rejected_change_leaves_catalog_untouched(tmp_path, monkeypatch):
    factory = configured_catalog(tmp_path, monkeypatch)
    sync.record_discovery("source-test", orders_discovery(), "admin")
    sync.record_discovery("source-test", orders_discovery(data_type="integer"), "admin")
    pending = sync.list_changes("source-test", "admin")[0]
    result = sync.decide_change("source-test", pending["id"], "admin", "reject", "等待上游确认")
    assert result["status"] == "rejected"
    connection = factory()
    assert connection.execute(
        "SELECT data_type FROM catalog_columns WHERE id='column-source-test'"
    ).fetchone()[0] == "bigint"
    connection.close()


def test_removed_published_table_is_archived_only_after_approval(tmp_path, monkeypatch):
    factory = configured_catalog(tmp_path, monkeypatch, published=True)
    sync.record_discovery("source-test", orders_discovery(), "admin")
    result = sync.record_discovery(
        "source-test", {"table_count": 0, "relationship_count": 0, "tables": [], "relationships": []}, "admin"
    )
    assert result["changes"]["removed"] == 1
    connection = factory()
    assert connection.execute(
        "SELECT publish_status FROM catalog_assets WHERE id='asset-source-test'"
    ).fetchone()[0] == "published"
    connection.close()

    monkeypatch.setattr(vector_index, "refresh_catalog_assets", lambda _ids: True)
    pending = sync.list_changes("source-test", "admin")[0]
    sync.decide_change("source-test", pending["id"], "admin", "approve", "上游已删除")
    connection = factory()
    status = connection.execute(
        "SELECT lifecycle_status,publish_status FROM catalog_assets WHERE id='asset-source-test'"
    ).fetchone()
    assert tuple(status) == ("archived", "archived")
    connection.close()


def test_new_table_becomes_reviewable_draft_after_approval(tmp_path, monkeypatch):
    factory = configured_catalog(tmp_path, monkeypatch)
    sync.record_discovery("source-test", orders_discovery(), "admin")
    discovery = orders_discovery()
    discovery["tables"].append({
        "namespace": "ods", "name": "payments", "description": "支付流水",
        "primary_key": ["payment_id"], "columns": [{
            "name": "payment_id", "type": "bigint", "ordinal": 1, "description": "支付编号",
            "nullable": False, "is_primary_key": True,
        }],
    })
    result = sync.record_discovery("source-test", discovery, "admin")
    assert result["changes"]["added"] == 1
    pending = next(item for item in sync.list_changes("source-test", "admin") if item["entity_key"] == "ods.payments")
    sync.decide_change("source-test", pending["id"], "admin", "approve")
    connection = factory()
    asset = connection.execute(
        "SELECT publish_status,primary_key FROM catalog_assets WHERE source_id='source-test' AND physical_name='payments'"
    ).fetchone()
    assert tuple(asset) == ("draft", "payment_id")
    connection.close()


def test_metadata_sync_http_contract_requires_admin_and_applies_decision(tmp_path, monkeypatch):
    configured_catalog(tmp_path, monkeypatch)
    sync.record_discovery("source-test", orders_discovery(), "admin")
    sync.record_discovery("source-test", orders_discovery(data_type="integer"), "admin")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "analyst"}
    client = TestClient(app)
    assert client.get("/api/data-sources/source-test/changes").status_code == 403

    app.dependency_overrides[require_user] = lambda: {"id": "admin"}
    changes = client.get("/api/data-sources/source-test/changes").json()
    assert len(changes) == 1
    response = client.post(
        f"/api/data-sources/source-test/changes/{changes[0]['id']}/decision",
        json={"decision": "reject", "reason": "接口测试"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_latest_approved_change_can_be_rolled_back(tmp_path, monkeypatch):
    factory = configured_catalog(tmp_path, monkeypatch, published=True)
    sync.record_discovery("source-test", orders_discovery(), "admin")
    sync.record_discovery("source-test", orders_discovery(data_type="integer"), "admin")
    change = sync.list_changes("source-test", "admin")[0]
    monkeypatch.setattr(vector_index, "refresh_catalog_assets", lambda _ids: True)
    sync.decide_change("source-test", change["id"], "admin", "approve")
    connection = factory()
    assert connection.execute(
        "SELECT data_type FROM catalog_columns WHERE id='column-source-test'"
    ).fetchone()[0] == "integer"
    connection.close()
    rolled_back = sync.rollback_change("source-test", change["id"], "admin")
    assert rolled_back["status"] == "rolled_back"
    connection = factory()
    assert connection.execute(
        "SELECT data_type FROM catalog_columns WHERE id='column-source-test'"
    ).fetchone()[0] == "bigint"
    connection.close()
