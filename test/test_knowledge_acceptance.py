"""API-to-storage acceptance; offline by default, opt-in real remote RAG services.

Authentication is injected, so this does not replace browser/login or Milvus E2E.
"""
import json
import os
import sqlite3
import uuid
from functools import partial

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.knowledge as api
import backend.knowledge.repository as repository
import backend.knowledge_base as knowledge
from backend.auth import require_user
from backend.knowledge.indexer import KnowledgeDocumentIndexer
from backend.knowledge.source_storage import SourceStorage
from backend.rag import prompt_context


@pytest.fixture
def acceptance_index(monkeypatch):
    """Opt-in live services; only a uniquely named throwaway collection is touched."""
    remote = os.getenv("DATA_AGENT_ACCEPTANCE_REMOTE_RAG") == "1"
    settings = {"EMBEDDING_BACKEND": "bge" if remote else "hash",
                "VECTOR_STORE_BACKEND": "milvus" if remote else "sqlite",
                "RERANK_BACKEND": "bge" if remote else "none"}
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    if not remote:
        yield False
        return
    from backend.vector_store import milvus_client
    collection = "acceptance_knowledge_" + uuid.uuid4().hex
    monkeypatch.setenv("MILVUS_URI", "http://127.0.0.1:19530")
    monkeypatch.setenv("MILVUS_DATABASE", "data_agent")
    monkeypatch.setenv("MILVUS_COLLECTION", collection)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("RERANK_BASE_URL", "http://127.0.0.1:8082")
    client = milvus_client()
    assert not client.has_collection(collection_name=collection)
    try:
        yield True
    finally:
        if client.has_collection(collection_name=collection):
            client.drop_collection(collection_name=collection)
        client.close()


@pytest.mark.parametrize("filename,media_type,content", [
    ("rules.md", "text/markdown", "# 指标口径\n\n## 星河净销售额\n\n星河净销售额是已支付金额减去已退款金额。"),
    ("rules.txt", "text/plain", "星河净销售额是已支付金额减去已退款金额。"),
    ("rules.csv", "text/csv", "指标,定义\n星河净销售额,已支付金额减去已退款金额\n"),
])
def test_upload_retrieve_isolate_and_reopen(tmp_path, monkeypatch, acceptance_index, filename, media_type, content):
    def factory():
        connection = sqlite3.connect(tmp_path / "knowledge.db")
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(knowledge, "db", factory)
    monkeypatch.setattr(knowledge, "init_knowledge_schema", partial(repository.init_knowledge_schema, connection_factory=factory))
    for name in ("init_knowledge_schema", "assert_writable_knowledge_base",
                 "get_knowledge_document", "list_knowledge_documents"):
        monkeypatch.setattr(api, name, partial(getattr(repository, name), connection_factory=factory))

    def new_indexer():
        return KnowledgeDocumentIndexer(connection_factory=factory,
                                        source_storage=SourceStorage(tmp_path / "uploads"))

    monkeypatch.setattr(api, "_indexer", new_indexer())
    active_user = {"id": "acceptance-owner", "username": "owner"}

    def new_app():
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[require_user] = lambda: active_user
        return app

    with TestClient(new_app()) as client:
        created = client.post("/api/knowledge-bases", json={"name": "验收私有指标库"})
        assert created.status_code == 201, created.text
        base_id = created.json()["id"]
        path = f"/api/knowledge-bases/{base_id}/documents"
        upload = client.post(path + "/upload", files={"file": (filename, content.encode(), media_type)})
        assert upload.status_code == 200, upload.text
        document = upload.json()["document"]
        assert document["status"] == "ready" and document["chunk_count"] > 0
        assert "storage_path" not in document
        duplicate = client.post(path + "/upload", files={"file": (filename, content.encode(), media_type)})
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["document_id"] == document["id"]
        hits = knowledge.retrieve("星河净销售额", user_id=active_user["id"])
        assert any("已退款金额" in hit["content"] for hit in hits)
        if acceptance_index:
            # No silent dense/reranker fallback may make a live test pass.
            assert any("dense_score" in hit and "bm25_score" in hit and "rerank_score" in hit for hit in hits)
            from backend.vector_store import milvus_client, knowledge_collection_name
            client_vectors = milvus_client()
            try:
                vectors = client_vectors.query(collection_name=knowledge_collection_name(),
                                               filter=f'document_id == "{document["id"]}"',
                                               output_fields=["content", "vector"], consistency_level="Strong")
                assert vectors and all(len(row["vector"]) == 512 for row in vectors)
                assert any("已退款金额" in row["content"] for row in vectors)
            finally:
                client_vectors.close()
        assert filename in prompt_context("星河净销售额", hits)
        connection = factory()
        try:
            rows = connection.execute("SELECT content,embedding FROM knowledge_chunks").fetchall()
            assert rows and all(json.loads(row["embedding"]) for row in rows)
        finally:
            connection.close()

        active_user = {"id": "acceptance-other", "username": "other"}
        assert client.get("/api/knowledge-bases").json() == []
        assert client.get(path).status_code == 404
        assert client.get(path + "/" + document["id"]).status_code == 404
        denied = client.post(path + "/upload", files={"file": (filename, content.encode(), media_type)})
        assert denied.status_code == 404
        assert knowledge.retrieve("星河净销售额", user_id=active_user["id"]) == []

    # Rebuild app/indexer on the same database: persistent state, not process restart.
    active_user = {"id": "acceptance-owner", "username": "owner"}
    monkeypatch.setattr(api, "_indexer", new_indexer())
    with TestClient(new_app()) as client:
        restored = client.get(path)
        assert restored.status_code == 200, restored.text
        assert [item["id"] for item in restored.json()] == [document["id"]]
        assert knowledge.retrieve("星河净销售额", user_id=active_user["id"])
    assert len(list((tmp_path / "uploads").rglob("original.*"))) == 1
