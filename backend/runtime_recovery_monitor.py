"""Detect abandoned production leases; never reconnect or replay warehouse writes."""
import os
import threading


class RecoveryMonitor:
    def __init__(self, connection_factory=None, interval=10):
        self.factory = connection_factory
        self.interval = max(0.1, interval)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, name='production-recovery-monitor', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self, timeout=2):
        self.stopped.set()
        self.thread.join(timeout)

    def _loop(self):
        from backend.data_services.production_runner import mark_expired_runs
        from backend.observability import log_event
        while not self.stopped.is_set():
            try:
                count = mark_expired_runs(connection_factory=self.factory)['marked']
                if count:
                    log_event('production.recovery_required', runs=count)
            except Exception as exc:
                log_event('production.recovery_monitor_error', error_type=type(exc).__name__)
            self.stopped.wait(self.interval)


def start_monitor(connection_factory=None):
    if os.getenv('PRODUCTION_RECOVERY_MONITOR_ENABLED', '1').lower() in {'0','false','off'}:
        return None
    return RecoveryMonitor(connection_factory).start()
