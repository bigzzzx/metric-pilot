"""Review authorization, atomic updates, lifecycle and retrieval boundaries."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.api.catalog import router
from backend.auth import require_user
from backend.catalog import publications, review, service, rag_documents, retrieval, vector_index
from backend.data_sources import service as sources
from backend.main import init_storage
from test.test_data_source_admin import factory_for, seed_users, seed_source_draft


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    factory = factory_for(tmp_path / "review.db")
    init_storage(factory)
    seed_users(factory)
    seed_source_draft(factory)
    for module in (review, publications, service, rag_documents, retrieval, sources):
        monkeypatch.setattr(module, "db", factory)
    monkeypatch.setattr(vector_index, "ensure_catalog_index", lambda: False)
    return factory


def payload():
    return review.AssetReview(version=1, display_name="订单", description="订单明细", aliases=["销售单"],
        domain_id="domain-ecommerce", warehouse_layer="ODS", data_grain="一行一笔订单", sensitivity="internal",
        columns=[review.ReviewColumn(id="column-source-test", display_name="订单编号", description="订单唯一编号", sensitivity="sensitive", masking_strategy="redact")])


def test_review_publish_withdraw_and_delete(catalog):
    asset_id = "asset-source-test"
    with pytest.raises(HTTPException) as missing:
        publications.publish_asset(asset_id, "admin", 1)
    assert missing.value.status_code == 422
    review.save_review(asset_id, "admin", payload())
    assert not service.get_asset("admin", asset_id)["can_query"]
    result = publications.publish_asset(asset_id, "admin", 2)
    assert result["rag_indexed"] is False
    assert service.get_asset("analyst", asset_id)["publish_status"] == "published"
    assert any(asset_id in doc.get("asset_ids", []) for doc in rag_documents.authorized_catalog_documents("analyst"))
    with pytest.raises(sources.DataSourceInUse):
        sources.delete_source("source-test", "admin")
    review.change_status(asset_id, "admin", review.ReviewDecision(version=3, reason="核对字段说明"), withdraw=True)
    with pytest.raises(LookupError):
        service.get_asset("analyst", asset_id)
    with pytest.raises(LookupError):
        retrieval.lineage_for_asset("analyst", asset_id)
    assert not any(asset_id in doc.get("asset_ids", []) for doc in rag_documents.authorized_catalog_documents("analyst"))
    assert sources.delete_source("source-test", "admin")["deleted"]


def test_permissions_stale_version_and_column_ownership(catalog):
    for actor in ("analyst", "unknown"):
        with pytest.raises(HTTPException) as denied:
            review.save_review("asset-source-test", actor, payload())
        assert denied.value.status_code == 403
    wrong = payload()
    wrong.columns[0].id = "column-from-another-asset"
    with pytest.raises(HTTPException) as mismatch:
        review.save_review("asset-source-test", "admin", wrong)
    assert mismatch.value.status_code == 409
    assert service.get_asset("admin", "asset-source-test")["catalog_version"] == 1
    review.save_review("asset-source-test", "admin", payload())
    with pytest.raises(HTTPException) as stale:
        publications.publish_asset("asset-source-test", "admin", 1)
    assert stale.value.status_code == 409


def test_reject_requires_new_review_and_keeps_reason(catalog):
    review.change_status("asset-source-test", "admin", review.ReviewDecision(version=1, reason="缺少粒度"))
    assert service.get_asset("admin", "asset-source-test")["review_note"] == "缺少粒度"
    with pytest.raises(HTTPException):
        publications.publish_asset("asset-source-test", "admin", 2)
    edited = payload().model_copy(update={"version": 2})
    review.save_review("asset-source-test", "admin", edited)
    assert publications.publish_asset("asset-source-test", "admin", 3)["status"] == "published"


def test_sync_preserves_reviewed_metadata_and_relationships(catalog):
    review.save_review("asset-source-test", "admin", payload())
    discovery = {"tables": [{"namespace": "ods", "name": "orders", "description": "新采集注释", "columns": [{"name": "new_field"}]}]}
    result = publications.import_discovery_as_draft("source-test", discovery, "admin")
    asset = service.get_asset("admin", "asset-source-test")
    assert result["preserved"] == 1
    assert asset["description"] == "订单明细"
    assert asset["columns"][0]["sensitivity"] == "sensitive"
    assert asset["catalog_version"] == 2
    publications.publish_asset(asset["id"], "admin", 2)
    publications.import_discovery_as_draft("source-test", discovery, "admin")
    assert service.get_asset("admin", asset["id"])["publish_status"] == "published"


def test_postgres_publication_never_routes_to_sqlite(catalog):
    review.save_review("asset-source-test", "admin", payload())
    publications.publish_asset("asset-source-test", "admin", 2)
    assert not service.get_asset("admin", "asset-source-test")["can_query"]
    with pytest.raises(LookupError):
        service.sample_asset("admin", "asset-source-test")
    # Same physical name cannot overwrite the local orders allowlist.
    assert "order_id" in retrieval.authorized_query_schema("admin")["orders"]


def test_http_contract_and_unauthorized_requests(catalog):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "analyst"}
    client = TestClient(app)
    url = "/api/catalog/admin/assets/asset-source-test"
    assert client.put(url + "/review", json=payload().model_dump()).status_code == 403
    app.dependency_overrides[require_user] = lambda: {"id": "admin"}
    assert client.put(url + "/review", json={"arbitrary": True}).status_code == 422
    assert client.put(url + "/review", json=payload().model_dump()).status_code == 200
    assert client.post(url + "/publish?version=1").status_code == 409
    assert client.post(url + "/publish?version=2").status_code == 200
    assert client.post(url + "/withdraw", json={"version": 3, "reason": "测试撤回"}).status_code == 200


def test_relationship_only_published_with_both_endpoints(catalog):
    con = catalog()
    con.execute("""INSERT INTO catalog_relationships(id,from_asset_id,from_column,to_asset_id,to_column,
        relation_type,cardinality,recommended_join,join_condition,description,status)
        VALUES ('review-relation','asset-source-test','order_id','asset-orders','order_id',
        'foreign_key','many_to_one','LEFT JOIN','a.order_id=b.order_id','测试关联','draft')""")
    con.commit(); con.close()
    assert not any(r["id"] == "review-relation" for r in service.get_asset("analyst", "asset-orders")["relationships"])
    edited = payload()
    edited.columns[0].sensitivity = "internal"
    review.save_review("asset-source-test", "admin", edited)
    publications.publish_asset("asset-source-test", "admin", 2)
    assert any(r["id"] == "review-relation" for r in service.get_asset("analyst", "asset-orders")["relationships"])
    assert any(doc["id"] == "catalog:relation:review-relation" for doc in rag_documents.authorized_catalog_documents("analyst"))
    review.change_status("asset-source-test", "admin", review.ReviewDecision(version=3, reason="重新核对"), withdraw=True)
    assert not any(r["id"] == "review-relation" for r in service.get_asset("analyst", "asset-orders")["relationships"])
    assert not any(doc["id"] == "catalog:relation:review-relation" for doc in rag_documents.authorized_catalog_documents("analyst"))
