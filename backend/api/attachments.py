from contextlib import closing
from urllib.parse import quote
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from backend.auth import require_user
from backend.core import db
from backend.catalog.publications import require_platform_admin
from backend.attachments import repository as repo, service
from backend.knowledge.models import DocumentIngestionError

router = APIRouter(prefix='/api', tags=['chat-attachments'])


@router.post('/conversations/{conversation_id}/attachments/upload')
async def upload(conversation_id: str, file: UploadFile = File(...), user=Depends(require_user)):
    try:
        with closing(db()) as con:
            repo.owned_conversation(con, conversation_id, user['id'])
        data = await file.read(repo.MAX_BYTES + 1)
        return await run_in_threadpool(service.upload, conversation_id, user['id'], file.filename or '', file.content_type, data)
    except LookupError as exc:
        raise HTTPException(404, '会话不存在') from exc
    except (ValueError, DocumentIngestionError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()


@router.get('/conversations/{conversation_id}/attachments')
def list_attachments(conversation_id: str, user=Depends(require_user)):
    try:
        return repo.list_files(conversation_id, user['id'], bound_only=False)
    except LookupError as exc:
        raise HTTPException(404, '会话不存在') from exc


@router.get('/conversations/{conversation_id}/attachments/{attachment_id}')
def download(conversation_id: str, attachment_id: str, user=Depends(require_user)):
    try:
        file = repo.get_file(attachment_id, conversation_id, user['id'])
    except LookupError as exc:
        raise HTTPException(404, '附件不存在') from exc
    disposition = 'inline' if file['kind'] == 'image' else 'attachment'
    return Response(file['data'], media_type=file['media_type'], headers={
        'Content-Disposition': f"{disposition}; filename*=UTF-8''{quote(file['name'])}",
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


@router.delete('/conversations/{conversation_id}/attachments/{attachment_id}')
def delete(conversation_id: str, attachment_id: str, user=Depends(require_user)):
    try:
        repo.get_file(attachment_id, conversation_id, user['id'])
    except LookupError as exc:
        raise HTTPException(404, '附件不存在') from exc
    with closing(db()) as con:
        con.execute('DELETE FROM chat_attachments WHERE id=? AND user_id=?', (attachment_id, user['id']))
        con.execute("UPDATE attachment_imports SET status='cancelled' WHERE attachment_id=? AND user_id=? AND status='pending'", (attachment_id, user['id']))
        con.commit()
    return {'deleted': True}


@router.get('/attachment-models')
def models(user=Depends(require_user)):
    return service.vision_models()


class Capability(BaseModel):
    vision: bool


@router.put('/attachment-models/{model_id}')
def set_capability(model_id: str, payload: Capability, user=Depends(require_user)):
    require_platform_admin(user['id'])
    with closing(db()) as con:
        if not con.execute('SELECT 1 FROM models WHERE id=?', (model_id,)).fetchone():
            raise HTTPException(404, '模型不存在')
        con.execute('INSERT INTO attachment_model_capabilities(model_id,vision) VALUES (?,?) ON CONFLICT(model_id) DO UPDATE SET vision=excluded.vision', (model_id, int(payload.vision)))
        con.commit()
    return payload.model_dump()
