import sqlite3

from backend.catalog import rag_documents, vector_index
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
    connection.execute("INSERT INTO catalog_group_memberships(group_id,user_id) VALUES ('group-ecommerce-analysts','analyst')")
    connection.commit()
    connection.close()


def test_catalog_rag_documents_apply_domain_acl_and_hide_sensitive_fields(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "catalog-rag.db")
    init_storage(factory)
    _seed_users(factory)
    monkeypatch.setattr(rag_documents, "db", factory)

    all_documents = rag_documents.published_catalog_documents()
    analyst_documents = rag_documents.authorized_catalog_documents("analyst")

    assert any(item["domain_id"] == "domain-marketing" for item in all_documents)
    assert analyst_documents
    assert all(item["domain_id"] == "domain-ecommerce" for item in analyst_documents)
    assert not any(item["id"] == "catalog:column:column-users-name" for item in all_documents)
    orders = next(item for item in all_documents if item["id"] == "catalog:asset:asset-orders")
    assert orders["source_id"] == "local-ecommerce-demo"
    assert orders["source_kind"] == "local-sqlite"


def test_dense_catalog_hits_are_filtered_before_they_leave_the_index(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "catalog-rag-dense.db")
    init_storage(factory)
    _seed_users(factory)
    monkeypatch.setattr(rag_documents, "db", factory)

    class Client:
        def has_collection(self, **_):
            return True

        def search(self, **_):
            return [[
                {"id": "catalog:asset:asset-ads_campaign_roi", "distance": 0.99},
                {"id": "catalog:asset:asset-orders", "distance": 0.88},
            ]]

    monkeypatch.setattr(vector_index, "ensure_catalog_index", lambda: True)
    monkeypatch.setattr(vector_index, "milvus_client", lambda: Client())
    monkeypatch.setattr(vector_index, "embed_texts", lambda *_args, **_kwargs: [[0.0] * 512])

    hits = vector_index.retrieve_catalog_vectors("指标口径", "analyst", 5)

    assert [item["id"] for item in hits] == ["catalog:asset:asset-orders"]
