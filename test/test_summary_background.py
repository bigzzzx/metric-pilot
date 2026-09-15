import asyncio
from types import SimpleNamespace

from fastapi import BackgroundTasks
from backend import main, summary_background


def test_cached_stream_sends_done_before_summary_and_contains_failure(monkeypatch):
    monkeypatch.setattr(main.attachment_repository, 'list_files', lambda *a, **kw: [])
    sent = []
    async def compact(*args):
        assert any(b'event: done' in item.get('body', b'') for item in sent)
        assert sent[-1]['more_body'] is False
        raise RuntimeError('summary provider unavailable')
    monkeypatch.setattr(summary_background, 'compact_conversation_if_needed', compact)
    monkeypatch.setattr(main, 'ensure_conversation', lambda *a: None)
    monkeypatch.setattr(main, 'begin_turn', lambda **kw: {'message': {'id': 'm'}})
    monkeypatch.setattr(main, 'completed_response', lambda *a: None)
    monkeypatch.setattr(main, 'context_for_agent', lambda *a, **kw: {})
    monkeypatch.setattr(main, 'response_cache_identity', lambda *a, **kw: {})
    monkeypatch.setattr(main, 'load_cached_response', lambda *a: {'answer': 'cached answer', 'intent': 'CHAT'})
    monkeypatch.setattr(main, 'finish_turn', lambda **kw: {'payload': kw['response_payload']})
    monkeypatch.setattr(main, 'apply_memory_actions', lambda *a: [])
    monkeypatch.setattr(main, 'synchronize_forgotten_memory_vectors', lambda *a: None)
    monkeypatch.setattr(main, 'remember_explicit_request', lambda *a: None)
    async def run():
        request = SimpleNamespace(state=SimpleNamespace(request_id='background-test'))
        body = SimpleNamespace(client_message_id='turn', session_id='session', message='解释kv cache', model_id=None)
        response = await main.chat_stream(body, request, BackgroundTasks(), {'id': 'user'})
        async def send(message):
            sent.append(message)
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    asyncio.run(run())
    assert not summary_background._running


def test_same_conversation_compaction_is_deduplicated(monkeypatch):
    calls = []
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def compact(*args):
            calls.append(args)
            started.set()
            await release.wait()
        monkeypatch.setattr(summary_background, 'compact_conversation_if_needed', compact)
        task = asyncio.create_task(summary_background.summarize_after_response('c', 'u'))
        await started.wait()
        await summary_background.summarize_after_response('c', 'u')
        release.set()
        await task
    asyncio.run(run())
    assert len(calls) == 1
