from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import backend.api.knowledge as knowledge_api
from backend.auth import require_user
from backend.knowledge.models import DocumentIngestionError
from backend.vector_store import VectorStoreError


def _document(**overrides):
    value = {
        "id": "document-1",
        "knowledge_base_id": "base-1",
        "name": "metrics.md",
        "source_type": "md",
        "media_type": "text/markdown",
        "byte_size": 12,
        "sha256": "abc",
        "storage_path": ".data/uploads/user-1/document-1/source.md",
        "status": "ready",
        "parser_name": "markdown",
        "warnings": [],
        "error_code": None,
        "error_message": None,
        "chunk_count": 2,
        "created_at": "2026-08-21 12:00:00",
        "updated_at": "2026-08-21 12:00:00",
    }
    value.update(overrides)
    return value


def _app(monkeypatch, *, user_id: str | None = "user-1") -> FastAPI:
    monkeypatch.setattr(knowledge_api, "init_knowledge_schema", lambda: None)
    monkeypatch.setattr(knowledge_api, "initialize_knowledge_runtime", lambda: None)
    app = FastAPI()
    app.include_router(knowledge_api.router)
    if user_id is not None:
        app.dependency_overrides[require_user] = lambda: {"id": user_id, "username": user_id}
    return app


def test_routes_require_authentication(monkeypatch):
    app = _app(monkeypatch, user_id=None)
    with TestClient(app) as client:
        response = client.get("/api/knowledge-bases")
    assert response.status_code == 401


def test_binary_upload_calls_indexer_and_returns_frontend_compatible_shape(monkeypatch):
    captured = {}

    class FakeIndexer:
        def ingest_bytes(self, **kwargs):
            captured.update(kwargs)
            return _document()

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge-bases/base-1/documents/upload",
            files={"file": ("metrics.md", b"# GMV\npaid amount", "text/markdown")},
        )

    assert response.status_code == 200
    assert captured["owner_id"] == "user-1"
    assert captured["knowledge_base_id"] == "base-1"
    assert captured["filename"] == "metrics.md"
    assert captured["data"].startswith(b"# GMV")
    assert response.json()["document"]["id"] == "document-1"
    assert response.json()["document_id"] == "document-1"
    assert response.json()["parser"] == "markdown"
    assert response.json()["chunk_count"] == 2


def test_duplicate_processing_upload_is_accepted_and_can_be_polled(monkeypatch):
    processing = _document(
        status="processing",
        parser_name="markdown",
        chunk_count=0,
        error_code="",
        error_message="",
    )

    class FakeIndexer:
        def ingest_bytes(self, **_kwargs):
            return processing

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    monkeypatch.setattr(
        knowledge_api,
        "get_knowledge_document",
        lambda document_id, owner_id: {**processing, "id": document_id},
    )
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge-bases/base-1/documents/upload",
            files={"file": ("metrics.md", b"same content", "text/markdown")},
        )
        detail = client.get("/api/knowledge-bases/base-1/documents/document-1")

    assert response.status_code == 202
    assert response.json()["document_id"] == "document-1"
    assert response.json()["status"] == "processing"
    assert response.json()["document"]["status"] == "processing"
    assert "相同文档正在处理" in response.json()["message"]
    assert "轮询" in response.json()["message"]
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert detail.status_code == 200
    assert detail.json()["status"] == "processing"


def test_returned_ocr_document_is_accepted_even_without_new_exception(monkeypatch):
    class FakeIndexer:
        def ingest_text(self, **_kwargs):
            return _document(status="ocr_required", parser_name="pdf", chunk_count=0)

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge-bases/base-1/documents",
            json={"name": "scan.pdf", "content": "scan placeholder", "source_type": "txt"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "ocr_required"


def test_ocr_required_returns_persisted_document_as_accepted(monkeypatch):
    class FakeIndexer:
        def ingest_bytes(self, **_kwargs):
            raise DocumentIngestionError(
                "ocr_required",
                "扫描版 PDF 需要 OCR。",
                details={"document_id": "document-ocr", "parser": "pdf"},
            )

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    monkeypatch.setattr(
        knowledge_api,
        "get_knowledge_document",
        lambda document_id, owner_id: _document(
            id=document_id,
            status="ocr_required",
            parser_name="pdf",
            error_code="ocr_required",
            error_message="扫描版 PDF 需要 OCR。",
            chunk_count=0,
        ),
    )
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge-bases/base-1/documents/upload",
            files={"file": ("scan.pdf", b"%PDF scan", "application/pdf")},
        )

    assert response.status_code == 202
    assert response.json()["document_id"] == "document-ocr"
    assert response.json()["document"]["status"] == "ocr_required"
    assert response.json()["status"] == "ocr_required"
    assert response.json()["error_code"] == "ocr_required"
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_json_text_import_is_preserved_and_normalises_legacy_text_type(monkeypatch):
    captured = {}

    class FakeIndexer:
        def ingest_text(self, **kwargs):
            captured.update(kwargs)
            return _document(name=kwargs["name"], source_type=kwargs["source_type"], parser_name="text")

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge-bases/base-1/documents",
            json={"name": "指标口径", "content": "GMV 是已支付金额之和。", "source_type": "text"},
        )

    assert response.status_code == 200
    assert captured["source_type"] == "txt"
    assert captured["owner_id"] == "user-1"
    assert response.json()["status"] == "ready"


def test_ingestion_format_error_has_stable_code_and_request_id(monkeypatch):
    class FakeIndexer:
        def ingest_bytes(self, **_kwargs):
            raise DocumentIngestionError("unsupported_format", "不支持该文件格式。")

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge-bases/base-1/documents/upload",
            headers={"X-Request-ID": "req_test_format"},
            files={"file": ("legacy.doc", b"legacy", "application/msword")},
        )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_format"
    assert response.json()["request_id"] == "req_test_format"
    assert response.headers["X-Request-ID"] == "req_test_format"


def test_vector_index_failure_is_service_unavailable(monkeypatch):
    class FakeIndexer:
        def ingest_text(self, **_kwargs):
            raise VectorStoreError("connection refused")

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge-bases/base-1/documents",
            json={"name": "指标口径", "content": "GMV 定义"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "vector_store_unavailable"
    assert "connection refused" not in response.text


def test_document_lookup_does_not_leak_across_users(monkeypatch):
    requested = {}

    def fake_get(document_id, owner_id):
        requested.update(document_id=document_id, owner_id=owner_id)
        raise LookupError("not owned")

    monkeypatch.setattr(knowledge_api, "get_knowledge_document", fake_get)
    app = _app(monkeypatch, user_id="user-2")
    with TestClient(app) as client:
        response = client.get("/api/knowledge-bases/base-1/documents/document-1")

    assert requested == {"document_id": "document-1", "owner_id": "user-2"}
    assert response.status_code == 404
    assert response.json()["code"] == "knowledge_not_found"
    assert "not owned" not in response.text


def test_delete_document_passes_authenticated_owner_to_indexer(monkeypatch):
    captured = {}

    class FakeIndexer:
        def delete_document(self, **kwargs):
            captured.update(kwargs)
            return _document(id=kwargs["document_id"])

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.delete("/api/knowledge-bases/base-1/documents/document-1")

    assert response.status_code == 200
    assert captured == {
        "knowledge_base_id": "base-1",
        "document_id": "document-1",
        "owner_id": "user-1",
    }
    assert response.json()["id"] == "document-1"


def test_delete_knowledge_base_rejects_processing_documents(monkeypatch):
    class FakeIndexer:
        def delete_knowledge_base(self, **_kwargs):
            raise RuntimeError("knowledge_document_processing")

    monkeypatch.setattr(knowledge_api, "get_document_indexer", lambda: FakeIndexer())
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.delete("/api/knowledge-bases/base-1")

    assert response.status_code == 409
    assert response.json()["code"] == "knowledge_document_processing"


def test_base_list_exposes_explicit_write_capability(monkeypatch):
    monkeypatch.setattr(
        knowledge_api,
        "list_bases",
        lambda _owner_id: [
            {"id": "shared", "name": "共享库", "description": "", "document_count": 0},
            {"id": "owned", "name": "个人库", "description": "", "document_count": 0},
            {"id": "declared", "name": "声明只读", "description": "", "document_count": 0, "can_write": False},
        ],
    )

    def writable(base_id, _owner_id):
        if base_id == "shared":
            raise LookupError("read only")
        return {"id": base_id}

    monkeypatch.setattr(knowledge_api, "assert_writable_knowledge_base", writable)
    app = _app(monkeypatch)
    with TestClient(app) as client:
        response = client.get("/api/knowledge-bases")

    assert response.status_code == 200
    capabilities = {base["id"]: base["can_write"] for base in response.json()}
    assert capabilities == {"shared": False, "owned": True, "declared": False}


def test_router_lifespan_invokes_replaceable_runtime_initializer(monkeypatch):
    called = []
    monkeypatch.setattr(knowledge_api, "initialize_knowledge_runtime", lambda: called.append("started"))
    app = FastAPI()
    app.include_router(knowledge_api.router)
    app.dependency_overrides[require_user] = lambda: {"id": "user-1", "username": "user-1"}
    monkeypatch.setattr(knowledge_api, "list_bases", lambda _owner_id: [])
    with TestClient(app) as client:
        assert client.get("/api/knowledge-bases").status_code == 200
    assert called == ["started"]


@pytest.mark.parametrize("path", ["/api/knowledge-bases/base-1/documents", "/api/knowledge-bases/base-1/documents/missing"])
def test_cross_user_repository_lookup_maps_to_404(monkeypatch, path):
    monkeypatch.setattr(knowledge_api, "list_knowledge_documents", lambda *_args: (_ for _ in ()).throw(LookupError()))
    monkeypatch.setattr(knowledge_api, "get_knowledge_document", lambda *_args: (_ for _ in ()).throw(LookupError()))
    app = _app(monkeypatch, user_id="other-user")
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 404
