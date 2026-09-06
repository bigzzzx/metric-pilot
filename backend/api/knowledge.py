"""Authenticated knowledge-base and document-ingestion HTTP routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.auth import require_user
from backend.knowledge.indexer import KnowledgeDocumentIndexer
from backend.knowledge.models import DocumentIngestionError
from backend.knowledge.repository import (
    assert_writable_knowledge_base,
    get_knowledge_document,
    init_knowledge_schema,
    list_knowledge_documents,
)
from backend.knowledge.schemas import (
    KnowledgeBaseCreate,
    KnowledgeBaseView,
    KnowledgeDocumentView,
    KnowledgeImportResponse,
    KnowledgeTextDocumentCreate,
)
from backend.knowledge_base import create_base, list_bases
from backend.retrieval_models import RetrievalModelError
from backend.vector_store import VectorStoreError, using_milvus


MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def knowledge_lifespan(_app):
    await run_in_threadpool(initialize_knowledge_runtime)
    yield


router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge"], lifespan=knowledge_lifespan)

_indexer: KnowledgeDocumentIndexer | None = None


def get_document_indexer() -> KnowledgeDocumentIndexer:
    """Lazily build the synchronous ingestion application service."""
    global _indexer
    if _indexer is None:
        # Router startup owns the explicit recovery pass. Direct users of the
        # class retain its reconcile-on-construction default.
        _indexer = KnowledgeDocumentIndexer(reconcile_on_startup=False)
    return _indexer


def initialize_knowledge_runtime() -> None:
    """Startup hook kept replaceable so isolated API tests never touch real DB."""
    init_knowledge_schema()
    get_document_indexer().reconcile_stale_processing()


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", "") or request.headers.get("X-Request-ID", "")
    return str(existing)[:100] if existing else f"req_{uuid.uuid4().hex[:12]}"


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        headers={"X-Request-ID": request_id},
        content={
            "detail": message,
            "code": code,
            "error_code": code,
            "request_id": request_id,
            "details": details or {},
        },
    )


def _ingestion_error_status(error: DocumentIngestionError) -> int:
    if error.code == "file_too_large":
        return 413
    if error.code in {"unsupported_format", "file_type_mismatch"}:
        return 415
    if error.code == "parser_dependency_missing":
        return 503
    if error.code == "ocr_required":
        return 422
    return 400


def _normalise_document(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    # The generated on-disk path is an internal implementation detail. Keep it
    # in the repository for cleanup/recovery, but never expose it to clients.
    result.pop("storage_path", None)
    warnings = result.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = list(warnings) if isinstance(warnings, (tuple, set)) else [str(warnings)]
    result["warnings"] = warnings
    # Additive alias used by the current UI; parser_name remains canonical.
    result.setdefault("parser", result.get("parser_name"))
    return result


def _success_response(document: dict[str, Any], *, message: str | None = None) -> dict[str, Any]:
    view = _normalise_document(document)
    status = str(view.get("status") or "ready")
    return {
        "document": view,
        "document_id": str(view.get("id") or ""),
        "status": status,
        "parser": view.get("parser_name"),
        "chunk_count": int(view.get("chunk_count") or 0),
        "warnings": view.get("warnings", []),
        "error_code": view.get("error_code"),
        "index": "milvus-bge-hybrid" if using_milvus() else "sqlite-local-hybrid",
        "message": message or "Document parsed, chunked and written to the configured vector index.",
    }


def _validated_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the same Pydantic contract to direct JSONResponse branches."""
    return KnowledgeImportResponse.model_validate(payload).model_dump(mode="json")


def _ingestion_response(request: Request, document: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """Distinguish completed indexing from accepted asynchronous states."""
    status = str(document.get("status") or "ready")
    if status == "ready":
        return _success_response(document)
    if status == "processing":
        message = "相同文档正在处理，可通过文档详情接口轮询状态。"
    elif status == "ocr_required":
        message = "文档已保存，但扫描版 PDF 需要配置 OCR 后才能写入向量索引。"
    else:
        message = "文档当前尚未进入可检索终态，请通过文档详情接口查看状态。"
    payload = _success_response(document, message=message)
    request_id = _request_id(request)
    payload["request_id"] = request_id
    return JSONResponse(
        status_code=202,
        headers={"X-Request-ID": request_id},
        content=_validated_import_payload(payload),
    )


def _lookup_error(request: Request) -> JSONResponse:
    return _error_response(
        request,
        status_code=404,
        code="knowledge_not_found",
        message="知识库或文档不存在，或当前用户无权访问。",
    )


def _service_error(request: Request, error: Exception) -> JSONResponse:
    if isinstance(error, DocumentIngestionError):
        return _error_response(
            request,
            status_code=_ingestion_error_status(error),
            code=error.code,
            message=error.message,
            details=error.details,
        )
    if isinstance(error, VectorStoreError):
        return _error_response(
            request,
            status_code=503,
            code="vector_store_unavailable",
            message="向量数据库暂时不可用，文档未进入可检索状态。",
        )
    if isinstance(error, RetrievalModelError):
        return _error_response(
            request,
            status_code=503,
            code="embedding_service_unavailable",
            message="向量化服务暂时不可用，文档未进入可检索状态。",
        )
    return _error_response(
        request,
        status_code=503,
        code="document_indexing_failed",
        message="文档存储或索引失败，请稍后重试。",
    )


async def _document_ingestion_error(
    request: Request,
    error: DocumentIngestionError,
    *,
    knowledge_base_id: str,
    owner_id: str,
) -> JSONResponse:
    """Return an accepted OCR document when the indexer persisted its status."""
    document_id = str(error.details.get("document_id") or "")
    if error.code == "ocr_required" and document_id:
        try:
            document = await run_in_threadpool(get_knowledge_document, document_id, owner_id)
            if str(document.get("knowledge_base_id")) != knowledge_base_id:
                raise LookupError("Document is outside this knowledge base")
        except LookupError:
            return _lookup_error(request)
        payload = _success_response(
            document,
            message="文档已保存，但扫描版 PDF 需要配置 OCR 后才能写入向量索引。",
        )
        request_id = _request_id(request)
        payload["request_id"] = request_id
        return JSONResponse(
            status_code=202,
            headers={"X-Request-ID": request_id},
            content=_validated_import_payload(payload),
        )
    return _service_error(request, error)


def _list_bases_with_permissions(owner_id: str) -> list[dict[str, Any]]:
    """Add an explicit write capability without changing the legacy repository."""
    result = []
    for base in list_bases(owner_id):
        view = dict(base)
        if "can_write" in view:
            view["can_write"] = bool(view["can_write"])
        elif "owner_id" in view:
            view["can_write"] = view.get("owner_id") == owner_id
        else:
            try:
                assert_writable_knowledge_base(str(view["id"]), owner_id)
                view["can_write"] = True
            except LookupError:
                view["can_write"] = False
        result.append(view)
    return result


@router.get("", response_model=list[KnowledgeBaseView])
async def knowledge_bases(user: dict = Depends(require_user)):
    return await run_in_threadpool(_list_bases_with_permissions, user["id"])


@router.post("", status_code=201, response_model=KnowledgeBaseView)
async def create_knowledge_base(payload: KnowledgeBaseCreate, user: dict = Depends(require_user)):
    created = await run_in_threadpool(create_base, payload.name, payload.description, user["id"])
    return {**created, "can_write": True}


@router.get("/{knowledge_base_id}/documents", response_model=list[KnowledgeDocumentView])
async def knowledge_documents(
    knowledge_base_id: str,
    request: Request,
    user: dict = Depends(require_user),
):
    try:
        documents = await run_in_threadpool(list_knowledge_documents, knowledge_base_id, user["id"])
        return [_normalise_document(document) for document in documents]
    except LookupError:
        return _lookup_error(request)


@router.get("/{knowledge_base_id}/documents/{document_id}", response_model=KnowledgeDocumentView)
async def knowledge_document(
    knowledge_base_id: str,
    document_id: str,
    request: Request,
    user: dict = Depends(require_user),
):
    try:
        document = await run_in_threadpool(get_knowledge_document, document_id, user["id"])
        if str(document.get("knowledge_base_id")) != knowledge_base_id:
            raise LookupError("Document is outside this knowledge base")
        return _normalise_document(document)
    except LookupError:
        return _lookup_error(request)


@router.post("/{knowledge_base_id}/documents", response_model=KnowledgeImportResponse)
async def ingest_knowledge_text(
    knowledge_base_id: str,
    payload: KnowledgeTextDocumentCreate,
    request: Request,
    user: dict = Depends(require_user),
):
    source_type = payload.source_type.lower().removeprefix("file:")
    if source_type == "text":
        source_type = "txt"
    try:
        document = await run_in_threadpool(
            get_document_indexer().ingest_text,
            knowledge_base_id=knowledge_base_id,
            owner_id=user["id"],
            name=payload.name,
            content=payload.content,
            source_type=source_type,
        )
        return _ingestion_response(request, document)
    except LookupError:
        return _lookup_error(request)
    except DocumentIngestionError as error:
        return await _document_ingestion_error(
            request,
            error,
            knowledge_base_id=knowledge_base_id,
            owner_id=user["id"],
        )
    except (VectorStoreError, RetrievalModelError, OSError) as error:
        return _service_error(request, error)


@router.post("/{knowledge_base_id}/documents/upload", response_model=KnowledgeImportResponse)
async def upload_knowledge_document(
    knowledge_base_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_user),
):
    try:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await file.close()
    if len(data) > MAX_UPLOAD_BYTES:
        return _error_response(
            request,
            status_code=413,
            code="file_too_large",
            message="文件超过 20 MB 限制。",
            details={"size": len(data), "max_size": MAX_UPLOAD_BYTES},
        )

    try:
        document = await run_in_threadpool(
            get_document_indexer().ingest_bytes,
            knowledge_base_id=knowledge_base_id,
            owner_id=user["id"],
            filename=file.filename or "",
            media_type=file.content_type,
            data=data,
        )
        return _ingestion_response(request, document)
    except LookupError:
        return _lookup_error(request)
    except DocumentIngestionError as error:
        return await _document_ingestion_error(
            request,
            error,
            knowledge_base_id=knowledge_base_id,
            owner_id=user["id"],
        )
    except (VectorStoreError, RetrievalModelError, OSError) as error:
        return _service_error(request, error)
