"""Real data-quality profiling persistence, privacy and HTTP contracts."""
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import backend.api.quality as quality_api
from backend.api.quality import router
from backend.auth import require_user
from backend.catalog import quality, service
from backend.data_sources import postgresql_quality
from backend.main import init_storage
from test.test_data_source_admin import factory_for, seed_source_draft, seed_users


def configured_quality(tmp_path, monkeypatch):
    factory = factory_for(tmp_path / "quality.db")
    init_storage(factory)
    seed_users(factory)
    seed_source_draft(factory, publish_status="published")
    connection = factory()
    connection.execute("UPDATE catalog_assets SET primary_key='order_id' WHERE id='asset-source-test'")
    connection.commit(); connection.close()
    monkeypatch.setattr(quality, "db", factory)
    monkeypatch.setattr(quality, "require_platform_admin", lambda _actor: None)
    monkeypatch.setattr(quality, "connector_from_row", lambda _row: object())
    return factory


def profile_result(status="healthy"):
    return {
        "asset_id": "asset-source-test", "row_count": 1200, "score": 0.99,
        "freshness_at": "2026-09-11T00:00:00+00:00", "completeness": 0.98,
        "validity": 1.0, "status": status,
        "details": {
            "column_count": 1, "null_cells": 24, "primary_key": ["order_id"],
            "primary_key_null_rows": 0, "primary_key_duplicate_rows": 0,
            "column_null_rates": [{"column": "order_id", "null_count": 0, "null_rate": 0.0}],
            "privacy_notice": "仅保存聚合统计，不保存或返回任何原始字段值。",
        },
    }


def test_quality_run_persists_aggregates_and_asset_history(tmp_path, monkeypatch):
    factory = configured_quality(tmp_path, monkeypatch)
    monkeypatch.setattr(
        postgresql_quality, "profile_postgres_tables",
        lambda _connector, assets: ([profile_result()], []),
    )

    result = quality.run_source_quality("source-test", "admin")

    assert result["status"] == "completed"
    assert result["healthy_count"] == 1
    connection = factory()
    snapshot = connection.execute(
        "SELECT run_id,row_count,score,status,details_json FROM catalog_quality_snapshots WHERE asset_id='asset-source-test' ORDER BY observed_at DESC,id DESC LIMIT 1"
    ).fetchone()
    assert tuple(snapshot)[:4] == (result["run_id"], 1200, 0.99, "healthy")
    assert "原始字段值" in snapshot["details_json"]
    connection.close()

    monkeypatch.setattr(service, "db", factory)
    asset = service.get_asset("admin", "asset-source-test")
    assert asset["quality"]["row_count"] == 1200
    assert asset["quality"]["details"]["primary_key_duplicate_rows"] == 0


def test_quality_run_keeps_successful_tables_when_one_profile_fails(tmp_path, monkeypatch):
    factory = configured_quality(tmp_path, monkeypatch)
    monkeypatch.setattr(
        postgresql_quality, "profile_postgres_tables",
        lambda _connector, _assets: ([profile_result("warning")], [{
            "asset_id": "missing", "table": "ods.missing", "code": "quality_query_failed",
            "error_type": "UndefinedTable",
        }]),
    )

    result = quality.run_source_quality("source-test", "admin")

    assert result["status"] == "partial"
    assert result["warning_count"] == 1
    assert result["failed_count"] == 1
    run = quality.list_quality_runs("source-test", "admin")[0]
    assert run["summary"]["failures"][0]["code"] == "quality_query_failed"
    connection = factory()
    assert connection.execute(
        "SELECT action FROM catalog_audit_logs WHERE entity_id='source-test' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == "data_quality_profiled"
    connection.close()


def test_quality_http_contract_requires_platform_admin(tmp_path, monkeypatch):
    configured_quality(tmp_path, monkeypatch)
    monkeypatch.setattr(postgresql_quality, "profile_postgres_tables", lambda _connector, _assets: ([], []))
    monkeypatch.setattr(
        quality_api, "require_platform_admin",
        lambda actor: None if actor == "admin" else (_ for _ in ()).throw(HTTPException(403, "admin required")),
    )
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "analyst"}
    client = TestClient(app)

    assert client.post("/api/data-sources/source-test/quality/run").status_code == 403
    app.dependency_overrides[require_user] = lambda: {"id": "admin"}
    assert client.post("/api/data-sources/source-test/quality/run").status_code == 200
    assert client.get("/api/data-sources/source-test/quality/runs").json()[0]["status"] == "completed"


def test_freshness_column_prefers_operational_timestamp():
    columns = [
        {"physical_name": "business_date", "data_type": "date"},
        {"physical_name": "updated_at", "data_type": "timestamp without time zone"},
    ]
    assert postgresql_quality._freshness_column(columns) == "updated_at"
