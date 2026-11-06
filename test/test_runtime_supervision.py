"""Lifecycle wiring without application storage or remote service access."""
import asyncio
import threading
from types import SimpleNamespace

from backend import runtime_recovery_monitor as monitor


def test_monitor_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv('PRODUCTION_RECOVERY_MONITOR_ENABLED','0')
    assert monitor.start_monitor(lambda:None) is None


def test_monitor_only_marks_expiry_and_stops(monkeypatch):
    from backend.data_services import production_runner
    called=threading.Event()
    factory=lambda:None
    def scan(*,connection_factory):
        assert connection_factory is factory
        called.set()
        return {'marked':0}
    monkeypatch.setattr(production_runner,'mark_expired_runs',scan)
    handle=monitor.RecoveryMonitor(factory,interval=.1).start()
    try:
        assert called.wait(2)
    finally:
        handle.stop()
    assert not handle.thread.is_alive()


def test_app_lifecycle_starts_and_stops_both_consumers(monkeypatch):
    from backend import main,index_worker
    events=[]
    for name in ('configure_logging','init_storage','init_conversation_storage','register_mcp_tools'):
        monkeypatch.setattr(main,name,lambda *_a:None)
    monkeypatch.setattr(main,'backfill_explicit_memories_once',lambda *_a:None)
    def start(name):
        return lambda factory:(events.append('start:'+name) or SimpleNamespace(stop=lambda **_kw:events.append('stop:'+name)))
    monkeypatch.setattr(index_worker,'start_worker',start('index'))
    monkeypatch.setattr(monitor,'start_monitor',start('recovery'))
    async def run():
        async with main.lifespan(main.app):
            assert events==['start:index','start:recovery']
    asyncio.run(run())
    assert events==['start:index','start:recovery','stop:index','stop:recovery']
