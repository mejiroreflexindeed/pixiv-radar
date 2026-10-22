import threading
import time

from app.discovery.runner import DiscoveryBusyError
from app.scheduler.discovery import DiscoveryScheduler


class RecordingRunner:
    def __init__(self, *, busy=False):
        self.calls = []
        self.event = threading.Event()
        self.busy = busy

    def start(self, *, tag):
        self.calls.append(tag)
        self.event.set()
        if self.busy:
            raise DiscoveryBusyError()


def test_scheduler_waits_full_interval_then_runs_and_stops():
    runner = RecordingRunner()
    scheduler = DiscoveryScheduler(runner, interval_seconds=0.05, tag="ウマ娘")
    scheduler.start()
    assert runner.calls == []  # 启动不立即触发请求
    assert scheduler.status()["next_run_at"] is not None
    assert runner.event.wait(0.5)
    assert runner.calls == ["ウマ娘"]
    assert scheduler.stop()
    calls = len(runner.calls)
    time.sleep(0.08)
    assert len(runner.calls) == calls
    assert scheduler.status()["next_run_at"] is None


def test_scheduler_busy_run_is_a_bounded_skip_not_a_crash():
    runner = RecordingRunner(busy=True)
    scheduler = DiscoveryScheduler(runner, interval_seconds=0.02, tag="tag")
    scheduler.start()
    assert runner.event.wait(0.5)
    deadline = time.monotonic() + 0.5
    while scheduler.status()["last_attempt_at"] is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert scheduler.status()["last_error_code"] == "discovery_run_in_progress"
    assert scheduler.stop()
