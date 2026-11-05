"""Authenticated evidence import/review and authorized lineage browsing."""
import json
import hashlib
from pathlib import Path
from fastapi import APIRouter, Depends, BackgroundTasks, File, Form, UploadFile, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from backend.auth import require_user
from backend.core import db
from backend.lineage import service
from backend.lineage.models import EvidenceInput, ReviewInput, SourceStateInput, VersionInput
from backend.lineage.events import EventInput, import_event
from backend.lineage.capture import collect_available
from backend.lineage.documents import generate_document_candidates, upload_document
from backend.lineage.validation import validate_candidate
from backend.lineage.projection import lineage
from backend.knowledge.models import DocumentIngestionError

router = APIRouter(prefix='/api/catalog',tags=['lineage-evidence'])


@router.get('/evidence/sources')
def sources(user:dict=Depends(require_user)): return service.list_sources(user['id'])


@router.post('/evidence/import')
def import_source(payload:EvidenceInput,user:dict=Depends(require_user)):
    return service.import_evidence(payload,user['id'])


@router.post('/evidence/events')
def event(payload:EventInput,user:dict=Depends(require_user)): return import_event(payload,user['id'])


@router.post('/evidence/collect')
def collect(user:dict=Depends(require_user)): return collect_available(user['id'])


@router.post('/evidence/upload')
async def upload(file:UploadFile=File(...),source_id:str=Form(''),asset_ids:str=Form('[]'),user:dict=Depends(require_user)):
    try:
        ids = json.loads(asset_ids)
        if not isinstance(ids,list) or len(ids)>12 or any(not isinstance(x,str) for x in ids):
            raise HTTPException(422,'最多绑定 12 个实际资产 ID。')
        data = await file.read(20*1024*1024+1)
        return await run_in_threadpool(upload_document,file.filename or '',file.content_type,data,user['id'],source_id or None,ids)
    except json.JSONDecodeError as exc: raise HTTPException(422,'asset_ids 必须是 JSON 数组。') from exc
    except DocumentIngestionError as exc: raise HTTPException(413 if exc.code=='file_too_large' else 422,exc.message) from exc
    finally: await file.close()


@router.get('/evidence/sources/{source_ref}')
def source(source_ref:str,user:dict=Depends(require_user)): return service.get_source(source_ref,user['id'])


@router.get('/evidence/sources/{source_ref}/file')
def source_file(source_ref:str,user:dict=Depends(require_user)):
    source = service.get_source(source_ref,user['id'])
    stored = source['metadata'].get('storage_path')
    if not stored: raise HTTPException(404,'此来源没有上传原文件，原文可在证据详情查看。')
    root = service.SOURCE_ROOT.resolve()
    path = (root.parent/stored).resolve()
    if root not in path.parents or not path.is_file(): raise HTTPException(404,'原始证据文件不存在。')
    if source['metadata'].get('file_hash')!=hashlib.sha256(path.read_bytes()).hexdigest():
        raise HTTPException(409,'原始证据文件校验失败，请核对存储，不使用被改动的文件。')
    return FileResponse(path,filename=source['metadata'].get('original_filename','evidence.bin'),media_type='application/octet-stream',headers={'X-Content-Type-Options':'nosniff'})


@router.post('/evidence/sources/{source_ref}/extract')
async def extract(source_ref:str,user:dict=Depends(require_user)):
    return await generate_document_candidates(source_ref,user['id'])


@router.post('/evidence/sources/{source_ref}/reanalyze')
def reanalyze(source_ref:str,user:dict=Depends(require_user)): return service.reanalyze(source_ref,user['id'])


def enqueue(result,background_tasks):
    if result.get('index_job_id'): background_tasks.add_task(service.run_index_job,result['index_job_id'])
    return result


@router.put('/evidence/sources/{source_ref}/state')
def state(source_ref:str,payload:SourceStateInput,background_tasks:BackgroundTasks,user:dict=Depends(require_user)):
    return enqueue(service.change_source_status(source_ref,payload,user['id']),background_tasks)


@router.get('/evidence/candidates')
def candidates(status:str|None=None,asset_id:str|None=None,source_ref:str|None=None,user:dict=Depends(require_user)):
    return service.list_candidates(user['id'],status=status,asset_id=asset_id,source_ref=source_ref)


@router.post('/evidence/candidates/{identifier}/validate')
def validate(identifier:str,payload:VersionInput,profile:bool=False,user:dict=Depends(require_user)):
    return validate_candidate(identifier,payload.expected_version,user['id'],profile=profile)


@router.post('/evidence/candidates/{identifier}/review')
def review(identifier:str,payload:ReviewInput,user:dict=Depends(require_user)):
    return service.review_candidate(identifier,payload,user['id'])


@router.post('/evidence/candidates/{identifier}/publish')
def publish(identifier:str,payload:VersionInput,background_tasks:BackgroundTasks,user:dict=Depends(require_user)):
    return enqueue(service.change_candidate_status(identifier,payload.expected_version,user['id'],publish=True),background_tasks)


@router.post('/evidence/candidates/{identifier}/withdraw')
def withdraw(identifier:str,payload:VersionInput,background_tasks:BackgroundTasks,user:dict=Depends(require_user)):
    return enqueue(service.change_candidate_status(identifier,payload.expected_version,user['id']),background_tasks)


@router.post('/evidence/index/retry')
def retry_index(background_tasks:BackgroundTasks,user:dict=Depends(require_user)):
    con = db()
    try:
        service.require_admin(con,user['id'])
        ids = [r['id'] for r in con.execute("SELECT id FROM lineage_index_jobs WHERE status='pending' ORDER BY created_at LIMIT 20")]
    finally: con.close()
    for identifier in ids: background_tasks.add_task(service.run_index_job,identifier)
    return dict(pending=ids)


@router.get('/evidence/index/jobs')
def index_jobs(user:dict=Depends(require_user)):
    con=db()
    try:
        service.require_admin(con,user['id'])
        return dict(jobs=[dict(r) for r in con.execute('SELECT * FROM lineage_index_jobs ORDER BY created_at DESC,id DESC LIMIT 20')],
            total_pending=con.execute("SELECT COUNT(*) FROM lineage_index_jobs WHERE status='pending'").fetchone()[0])
    finally:con.close()


@router.get('/assets/{asset_id}/field-lineage')
def field_lineage(asset_id:str,column:str='',direction:str='upstream',hops:int=Query(2,ge=1,le=3),include_influences:bool=False,user:dict=Depends(require_user)):
    return lineage(user['id'],asset_id,column,direction,hops,include_influences)
