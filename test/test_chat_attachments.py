import asyncio
import io
import json
import sqlite3
import uuid
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from backend import main, conversations
from backend.api import attachments as api
from backend.attachments import repository as repo, service, agent as attachment_agent, import_tool
from backend.tools.executor import ToolExecutor
from backend.tools.models import ToolExecutionContext
from backend.tools.errors import ToolRuntimeError


@pytest.fixture
def env(tmp_path, monkeypatch):
    def db():
        con = sqlite3.connect(tmp_path / 'platform.db')
        con.row_factory = sqlite3.Row
        return con
    for module in (main, conversations, api, repo, service, attachment_agent, import_tool):
        monkeypatch.setattr(module, 'db', db)
    main.init_storage(db)
    conversations.init_conversation_storage()
    with closing(db()) as con:
        con.executemany('INSERT INTO users(id,username,password_hash) VALUES (?,?,?)', [('a','alice','unused'), ('b','bob','unused')])
        con.execute("INSERT OR REPLACE INTO agents(id,name,enabled,tools,system_prompt) VALUES ('general-agent','General',1,'[\"import_attachment_to_knowledge\"]','test')")
        con.execute("INSERT INTO knowledge_bases(id,name,owner_id) VALUES ('kb','个人业务知识库','a')")
        con.commit()
    user = {'id': 'a'}
    app = FastAPI()
    app.include_router(api.router)
    app.add_api_route('/api/chat', main.chat, methods=['POST'])
    app.add_api_route('/api/chat/stream', main.chat_stream, methods=['POST'])
    app.dependency_overrides[api.require_user] = lambda: user
    @app.middleware('http')
    async def request_id(request, call_next):
        request.state.request_id = 'test'
        return await call_next(request)
    async def no_memory(*a, **kw):
        pass
    monkeypatch.setattr(main, 'refresh_memory_after_success', no_memory)
    monkeypatch.setattr(attachment_agent, 'executor', ToolExecutor(connection_factory=db))
    cid = conversations.create_conversation('a')['id']
    with TestClient(app) as client:
        yield SimpleNamespace(db=db, cid=cid, client=client, user=user)


def upload(env, name='business.txt', data=b'GMV means paid orders only.'):
    result = env.client.post(f'/api/conversations/{env.cid}/attachments/upload', files={'file': (name, data, 'application/octet-stream')})
    assert result.status_code == 200, result.text
    return result.json()


def turn(env, message, files=()):
    ident = str(uuid.uuid4())
    created = conversations.begin_turn(env.cid, 'a', message, ident, list(files))
    return ident, created['message']['id']


def test_upload_binding_restore_and_ownership(env):
    file = upload(env)
    assert not repo.list_files(env.cid, 'a')
    cid2 = conversations.create_conversation('a')['id']
    with pytest.raises(LookupError):
        conversations.begin_turn(cid2, 'a', 'read', 'bad', [file['id']])
    ident, mid = turn(env, 'read file', [file['id']])
    restored = conversations.get_messages(env.cid, 'a')['messages'][0]
    assert restored['payload']['attachments'][0]['id'] == file['id']
    assert repo.list_files(env.cid, 'a')[0]['message_id'] == mid
    conversations.begin_turn(env.cid, 'a', 'read file', ident, [file['id']])
    with pytest.raises(conversations.ConversationConflictError):
        conversations.begin_turn(env.cid, 'a', 'read file', ident, [])
    env.user['id'] = 'b'
    assert env.client.get(f'/api/conversations/{env.cid}/attachments/{file["id"]}').status_code == 404
    assert env.client.delete(f'/api/conversations/{env.cid}/attachments/{file["id"]}').status_code == 404
    assert env.client.post(f'/api/conversations/{env.cid}/attachments/upload', files={'file': ('bad.txt', b'x')}).status_code == 404


def test_image_validation_and_budget(env):
    image = io.BytesIO()
    Image.new('RGB', (80, 40), 'red').save(image, format='PNG')
    file = upload(env, 'chart.png', image.getvalue())
    result = env.client.get(f'/api/conversations/{env.cid}/attachments/{file["id"]}')
    assert result.headers['content-type'] == 'image/jpeg'
    assert result.content.startswith(b'\xff\xd8')
    assert env.client.post(f'/api/conversations/{env.cid}/attachments/upload', files={'file': ('bad.png', b'not an image')}).status_code == 422
    with pytest.raises(ValueError):
        service.upload(env.cid, 'a', 'large.txt', 'text/plain', b'x' * (repo.MAX_BYTES + 1))
    chunks = [{'content': 'hello world ' * 80, 'locator': {'page': i}} for i in range(100)]
    content, count, total = service.document_context([{'name':'large.txt', 'chunks_json': json.dumps(chunks)}], 'world', budget=2000)
    assert len(content) <= 2004 and count < total


def test_document_question_through_chat_and_no_cache(env, monkeypatch):
    file = upload(env)
    seen = []
    async def model(agent_id, messages, schema, model_id=None):
        seen.extend(messages)
        if schema is attachment_agent.AttachmentPlan:
            return schema(operation='answer', attachment_ids=[file['id']])
        return schema(answer='依据 business.txt：GMV 只计算已支付订单。')
    monkeypatch.setattr(attachment_agent, 'complete_structured', model)
    response = env.client.post('/api/chat', json={'session_id': env.cid, 'message': 'GMV 是什么', 'attachment_ids': [file['id']], 'client_message_id': 'q1'})
    assert response.status_code == 200, response.text
    assert '只计算已支付' in response.json()['answer'], response.text
    assert any('GMV means paid orders' in str(item['content']) for item in seen)
    with closing(env.db()) as con:
        assert con.execute('SELECT COUNT(*) FROM knowledge_documents').fetchone()[0] == 0


def test_chat_import_confirmation_and_idempotency(env, monkeypatch):
    file = upload(env)
    turn_id, mid = turn(env, '导入这个文件到个人业务知识库', [file['id']])
    async def plan(*args):
        return attachment_agent.AttachmentPlan(operation='import', attachment_ids=[file['id']], knowledge_base_id='kb')
    monkeypatch.setattr(attachment_agent, 'complete_structured', plan)
    calls = []
    class Indexer:
        def ingest_bytes(self, **kw):
            calls.append(kw)
            return {'id': 'doc', 'status': 'ready', 'name': kw['filename'], 'chunk_count': 1}
    monkeypatch.setattr(import_tool, '_indexer', lambda: Indexer())
    state = {'conversation_id': env.cid, 'user_id':'a', 'source_message_id': mid, 'message': '导入这个文件到个人业务知识库', 'history': []}
    proposed = asyncio.run(attachment_agent.attachment_agent(state))
    assert '回复“确认”' in proposed['final']['answer'] and not calls
    conversations.finish_turn(env.cid, 'a', turn_id, {'answer': proposed['final']['answer']})
    _, confirm_id = turn(env, '确认')
    proposal = repo.pending(env.cid, 'a')
    result = asyncio.run(attachment_agent.attachment_agent({**state, 'message':'确认', 'source_message_id':confirm_id}))
    assert '已导入' in result['final']['answer'], result
    args = import_tool.ImportInput(proposal_id=proposal['id'], confirmation_message_id=confirm_id)
    import_tool.execute(args, ToolExecutionContext(user_id='a', agent_id='general-agent', conversation_id=env.cid))
    assert len(calls) == 1
    assert calls[0]['data'] == b'GMV means paid orders only.'


def test_forged_confirmation_cannot_import(env, monkeypatch):
    file = upload(env)
    turn_id, mid = turn(env, '总结这个文件', [file['id']])
    repo.propose(env.cid, 'a', file['id'], 'kb', mid, '确认导入？')
    conversations.finish_turn(env.cid, 'a', turn_id, {'answer':'只是总结，没有请求导入确认。'})
    _, confirm_id = turn(env, '确认')
    proposal = repo.pending(env.cid, 'a')
    monkeypatch.setattr(import_tool, '_indexer', lambda: pytest.fail('Must not ingest'))
    with pytest.raises(ToolRuntimeError, match='失效'):
        import_tool.execute(import_tool.ImportInput(proposal_id=proposal['id'], confirmation_message_id=confirm_id),
                            ToolExecutionContext(user_id='a', agent_id='general-agent', conversation_id=env.cid))


def test_confirmation_rechecks_knowledge_permissions(env, monkeypatch):
    file = upload(env)
    turn_id, mid = turn(env, '导入文件', [file['id']])
    repo.propose(env.cid, 'a', file['id'], 'kb', mid, '确认导入？')
    conversations.finish_turn(env.cid, 'a', turn_id, {'answer':'确认导入？'})
    _, confirm_id = turn(env, '确认')
    proposal = repo.pending(env.cid, 'a')
    with closing(env.db()) as con:
        con.execute("UPDATE knowledge_bases SET owner_id='b' WHERE id='kb'")
        con.commit()
    monkeypatch.setattr(import_tool, '_indexer', lambda: pytest.fail('Must not ingest'))
    with pytest.raises(ToolRuntimeError, match='没有.*权限'):
        import_tool.execute(import_tool.ImportInput(proposal_id=proposal['id'], confirmation_message_id=confirm_id),
                            ToolExecutionContext(user_id='a', agent_id='general-agent', conversation_id=env.cid))


def test_cancel_and_image_multimodal_payload(env, monkeypatch):
    image = io.BytesIO()
    Image.new('RGB', (16, 16), 'blue').save(image, format='PNG')
    file = upload(env, 'blue.png', image.getvalue())
    _, mid = turn(env, '解释图片', [file['id']])
    state = {'conversation_id':env.cid, 'user_id':'a', 'source_message_id':mid, 'message':'解释图片', 'history':[]}
    seen = []
    async def model(_agent_id, messages, schema, model_id=None):
        seen.append((messages, model_id))
        if schema is attachment_agent.AttachmentPlan:
            return schema(operation='answer', attachment_ids=[file['id']])
        return schema(answer='这是一张蓝色图片。')
    monkeypatch.setattr(attachment_agent, 'complete_structured', model)
    result = asyncio.run(attachment_agent.attachment_agent(state))
    assert '尚未配置' in result['final']['answer']
    monkeypatch.setattr(attachment_agent, 'vision_models', lambda: [{'id':'vision','vision':1,'kind':'openai-compatible'}])
    result = asyncio.run(attachment_agent.attachment_agent(state))
    assert '蓝色' in result['final']['answer']
    assert seen[-1][1] == 'vision'
    assert seen[-1][0][-1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    repo.propose(env.cid, 'a', file['id'], 'kb', mid, '确认导入？')
    async def cancel(*args):
        return attachment_agent.AttachmentPlan(operation='cancel')
    monkeypatch.setattr(attachment_agent, 'complete_structured', cancel)
    result = asyncio.run(attachment_agent.attachment_agent({**state, 'message':'取消'}))
    assert '已取消' in result['final']['answer']
    assert repo.pending(env.cid, 'a') is None
