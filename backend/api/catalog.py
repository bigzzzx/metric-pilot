"""Authenticated HTTP routes for the governed data catalog."""
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from backend.auth import require_user
from backend.catalog.publications import publish_asset, publish_assets, require_catalog_admin
from backend.catalog.retrieval import catalog_overview, lineage_for_asset
from backend.catalog.service import get_asset, list_assets, list_domains, list_metrics, list_terms, sample_asset
from backend.catalog.review import AssetReview, ReviewDecision, save_review, change_status
from backend.model_client import ModelOutputError
from backend.model_reliability import ModelProviderError
from backend.knowledge.models import DocumentIngestionError
from backend.observability import log_event

router = APIRouter(prefix="/api/catalog", tags=["catalog"])
from backend.semantic_metrics.wiki import (
    MetricReview, WikiSourceInput, add_source, generate_draft, publish, retry_index, run_index_job, upload_source_file,
)


@router.post('/admin/metrics/{metric_id}/wiki-draft')
async def metric_wiki_draft(metric_id: str, user: dict = Depends(require_user)):
    try:
        return await generate_draft(metric_id, user['id'])
    except ModelProviderError as exc:
        log_event('api.metric_wiki_draft_error', level=30, metric_id=metric_id, code=exc.code, http_status=exc.status)
        if exc.code == 'model_deadline_exceeded':
            detail = '生成 Wiki 草稿超时，系统已自动重试一次仍未完成。请稍后点击“重新生成草稿”；原指标和已发布口径未受影响。'
        elif exc.status == 429:
            detail = '模型服务当前请求较多，系统已自动重试一次。请稍后点击“重新生成草稿”；原指标和已发布口径未受影响。'
        else:
            detail = '模型服务暂时无法连接或响应超时，系统已自动重试一次仍未成功。请稍后点击“重新生成草稿”；原指标和已发布口径未受影响。'
        raise HTTPException(status_code=503, detail=detail) from exc
    except ModelOutputError as exc:
        log_event('api.metric_wiki_draft_error', level=30, metric_id=metric_id, code='invalid_model_output')
        raise HTTPException(status_code=422, detail='模型生成的草稿格式或引用证据未通过校验。系统已自动修正一次仍未成功，请重新生成或补充更完整的原始来源。') from exc


@router.post('/admin/metrics/{metric_id}/wiki-sources')
def metric_wiki_source(metric_id: str, payload: WikiSourceInput, user: dict = Depends(require_user)):
    return add_source(metric_id, user['id'], payload)


@router.post('/admin/metrics/{metric_id}/wiki-sources/upload')
async def upload_metric_wiki_source(metric_id: str, file: UploadFile = File(...), user: dict = Depends(require_user)):
    try:
        data = await file.read(20 * 1024 * 1024 + 1)
        return await run_in_threadpool(upload_source_file, metric_id, user['id'], file.filename or '', file.content_type, data)
    except DocumentIngestionError as exc:
        status = 413 if exc.code == 'file_too_large' else 415 if exc.code in {'unsupported_format', 'file_type_mismatch'} else 422
        raise HTTPException(status, exc.message) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()

@router.put('/admin/metrics/{metric_id}/publish')
def publish_metric(metric_id: str, payload: MetricReview, background_tasks: BackgroundTasks,
                   user: dict = Depends(require_user)):
    result = publish(metric_id,user['id'],payload)
    background_tasks.add_task(run_index_job, result['index_job_id'])
    return result


@router.post('/admin/metrics/{metric_id}/wiki-index/retry')
def retry_metric_wiki_index(metric_id: str, background_tasks: BackgroundTasks,
                            user: dict = Depends(require_user)):
    result = retry_index(metric_id, user['id'])
    if result['status'] != 'completed':
        background_tasks.add_task(run_index_job, result['id'])
    return result


class BatchPublishItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=0)


class BatchPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BatchPublishItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_assets(self):
        ids = [item.asset_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("同一资产不能重复提交")
        return self


@router.get("/overview")
def overview(user: dict = Depends(require_user)): return catalog_overview(user["id"])


@router.get("/domains")
def domains(user: dict = Depends(require_user)): return list_domains(user["id"])


@router.get("/assets")
def assets(q: str = "", domain_id: str | None = None, layer: str | None = None, source_id: str | None = None, sensitivity: str | None = None, user: dict = Depends(require_user)):
    return list_assets(user["id"],query=q,domain_id=domain_id,layer=layer,source_id=source_id,sensitivity=sensitivity)


@router.get("/assets/{asset_id}")
def asset(asset_id: str,user: dict = Depends(require_user)):
    try: return get_asset(user["id"],asset_id)
    except LookupError as exc: raise HTTPException(404,"Data asset not found or not authorized") from exc


@router.get("/assets/{asset_id}/sample")
def asset_sample(asset_id: str,limit: int = Query(default=5,ge=1,le=5),user: dict = Depends(require_user)):
    try: return sample_asset(user["id"],asset_id,limit)
    except LookupError as exc: raise HTTPException(404,"Sample data not found or not authorized") from exc


@router.get("/assets/{asset_id}/lineage")
def asset_lineage(asset_id: str,hops: int = Query(default=2,ge=1,le=3),user: dict = Depends(require_user)):
    try: return lineage_for_asset(user["id"],asset_id,hops)
    except LookupError as exc: raise HTTPException(404,"Lineage not found or not authorized") from exc


@router.get("/metrics")
def metrics(user: dict = Depends(require_user)): return list_metrics(user["id"])


@router.get("/terms")
def terms(user: dict = Depends(require_user)): return list_terms(user["id"])


@router.put("/admin/assets/{asset_id}/review")
def review_catalog_asset(asset_id: str, payload: AssetReview, user: dict = Depends(require_user)):
    try:
        return save_review(asset_id, user["id"], payload)
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc


@router.post("/admin/assets/{asset_id}/reject")
def reject_catalog_asset(asset_id: str, payload: ReviewDecision, user: dict = Depends(require_user)):
    try:
        return change_status(asset_id, user["id"], payload)
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc


@router.post("/admin/assets/{asset_id}/withdraw")
def withdraw_catalog_asset(asset_id: str, payload: ReviewDecision, user: dict = Depends(require_user)):
    try:
        return change_status(asset_id, user["id"], payload, withdraw=True)
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc


@router.post("/admin/assets/{asset_id}/publish")
def publish_catalog_asset(asset_id: str, version: int | None = Query(default=None, ge=0), user: dict = Depends(require_user)):
    try:
        return publish_asset(asset_id, user["id"], expected_version=version)
    except LookupError as exc:
        raise HTTPException(404, "Data asset not found") from exc


@router.post("/admin/assets/batch-publish")
def batch_publish_catalog_assets(payload: BatchPublishRequest, user: dict = Depends(require_user)):
    return publish_assets([item.model_dump() for item in payload.items], user["id"])
