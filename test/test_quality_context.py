"""Regression tests for quality-risk disclosure in the Agent query path."""
import json

from backend.catalog import quality_context
from backend.main import init_storage
from test.test_data_source_admin import factory_for, seed_source_draft, seed_users


def _configured(tmp_path, monkeypatch):
    factory = factory_for(tmp_path / "quality-context.db")
    init_storage(factory)
    seed_users(factory)
    seed_source_draft(factory, publish_status="published")
    monkeypatch.setattr(quality_context, "db", factory)
    return factory


def test_context_discloses_quality_warning_for_authorized_verified_table(tmp_path, monkeypatch):
    factory = _configured(tmp_path, monkeypatch)
    con = factory()
    con.execute("INSERT INTO catalog_quality_runs(id,source_id,actor_id,status,summary_json) VALUES (?,?,?,?,?)",
                ("quality-run-1", "source-test", "admin", "completed", "{}"))
    con.execute("""INSERT INTO catalog_quality_snapshots(
        id,run_id,asset_id,row_count,score,status,details_json)
        VALUES (?,?,?,?,?,?,?)""", (
            "quality-snapshot-1", "quality-run-1", "asset-source-test", 1200, 0.75, "critical",
            json.dumps({"freshness_column": "created_at", "freshness_age_days": 33}),
        ))
    con.commit(); con.close()

    context = quality_context.quality_context_for_schemas(
        "admin", {"tables": [{"asset_id": "asset-source-test"}]},
    )

    assert context["risks"][0]["table"] == "ods.orders"
    assert context["risks"][0]["status"] == "critical"
    assert "距今 33 天" in context["notice"]
    assert "不阻断本次只读查询" in context["notice"]


def test_context_discloses_failed_latest_quality_run_without_snapshot(tmp_path, monkeypatch):
    factory = _configured(tmp_path, monkeypatch)
    con = factory()
    con.execute("INSERT INTO catalog_quality_runs(id,source_id,actor_id,status,summary_json) VALUES (?,?,?,?,?)",
                ("quality-run-failed", "source-test", "admin", "failed", json.dumps({"error": "source_unavailable"})))
    con.commit(); con.close()

    context = quality_context.quality_context_for_schemas(
        "admin", {"tables": [{"asset_id": "asset-source-test"}]},
    )

    assert context["risks"][0]["status"] == "failed"
    assert "检测执行失败" in context["notice"]


def test_context_does_not_treat_a_missing_snapshot_as_a_quality_failure(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch)

    context = quality_context.quality_context_for_schemas(
        "admin", {"tables": [{"asset_id": "asset-source-test"}]},
    )

    assert context == {"risks": [], "notice": ""}
