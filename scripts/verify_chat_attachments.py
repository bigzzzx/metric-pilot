"""Live acceptance using a synthetic document and a dedicated personal knowledge base.

Run explicitly: python scripts/verify_chat_attachments.py --username user1
Creates a visible acceptance conversation/base; never prints authentication tokens.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path
from contextlib import closing
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.core import db
from backend.auth import create_session, revoke_session, SESSION_COOKIE


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--username', required=True)
    args = parser.parse_args()
    with closing(db()) as con:
        user = con.execute('SELECT id FROM users WHERE username=? AND is_active=1', (args.username,)).fetchone()
    if user is None:
        raise SystemExit('Active test user not found')
    token = create_session(user['id'])
    try:
        with httpx.Client(base_url='http://127.0.0.1:18001', trust_env=False, timeout=180, cookies={SESSION_COOKIE:token}) as client:
            def call(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
            cid = call('POST', '/api/conversations')['id']
            title = '附件验收-' + uuid.uuid4().hex[:8]
            base = call('POST', '/api/knowledge-bases', json={'name':title,'description':'合成附件导入验收'})
            file = call('POST', f'/api/conversations/{cid}/attachments/upload', files={'file':('attachment-acceptance.txt', '合成验收规则：验收指标 AT-42 只统计已支付订单，退款订单全部排除。'.encode(), 'text/plain')})
            print(json.dumps({'conversation_id':cid,'knowledge_base_id':base['id'],'uploaded':file['id']},ensure_ascii=False), flush=True)
            def chat(message, attachments=()):
                response = client.post('/api/chat/stream',json={'session_id':cid,'message':message,'attachment_ids':list(attachments),'client_message_id':str(uuid.uuid4())})
                response.raise_for_status()
                events = []
                for block in response.text.split('\n\n'):
                    lines = block.splitlines()
                    event = next((line[6:].strip() for line in lines if line.startswith('event:')), '')
                    data = next((line[5:].strip() for line in lines if line.startswith('data:')), '')
                    if data:
                        events.append((event,json.loads(data)))
                assert not [item for item in events if item[0]=='error'], events[-1]
                result = next(data for event,data in events if event=='done')
                print(result['answer'], flush=True)
                return result
            answer = chat('根据这个附件，AT-42 是否包括退款订单？',[file['id']])
            assert '退款' in answer['answer']
            proposal = chat(f'把 attachment-acceptance.txt 导入知识库“{title}”。')
            assert '回复“确认”' in proposal['answer'], proposal
            assert not call('GET', f'/api/knowledge-bases/{base["id"]}/documents'), 'Imported without confirmation'
            completed = chat('确认')
            assert '已导入知识库' in completed['answer'], completed
            documents = call('GET', f'/api/knowledge-bases/{base["id"]}/documents')
            assert len(documents)==1 and documents[0]['status']=='ready', documents
            restored = call('GET', f'/api/conversations/{cid}/messages')
            assert restored['messages'][0]['payload']['attachments'][0]['id']==file['id']
            print('PASS live: upload, model answer, chat confirmation, persisted indexed document, history attachment restoration', flush=True)
    finally:
        revoke_session(token)


if __name__ == '__main__':
    main()
