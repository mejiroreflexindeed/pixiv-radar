"""W03 真实出站边界测试：单并发、间隔、取消、冷却、暂停与失败同步。"""

from __future__ import annotations

import threading
import time

import pytest

from app.content.service import (
    ContentAnalysisService,
    ContentOperationConflict,
    ContentRateLimited,
)
from app.discovery.fake import FakeDiscoveryAdapter
from app.discovery.runner import DiscoveryRunner
from app.pixiv.adapter import NovelSummary
from app.pixiv.exceptions import RateLimited, TemporaryFailure
from app.pixiv.request_gate import (
    PixivCooldownActive,
    PixivOperationCancelled,
    PixivOperationInProgress,
    PixivPaused,
    RequestGate,
    to_pixiv_error,
)
from app.storage.connection import connect
from app.storage.content_repository import ContentRepository
from app.storage.discovery_repository import DiscoveryRepository
from app.storage.migrations import apply_migrations
from app.storage.scan_repository import ScanRepository


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_gate(*, interval: float = 3.0, clock: FakeClock | None = None, waits: list | None = None):
    clock = clock or FakeClock()
    recorded: list[float] = waits if waits is not None else []

    def wait(seconds: float, cancel: threading.Event) -> bool:
        recorded.append(seconds)
        return cancel.is_set()

    return RequestGate(min_interval_seconds=interval, clock=clock, wait=wait), clock, recorded


def test_single_concurrency_and_interval():
    gate, clock, waits = make_gate()
    with gate.operation():
        gate.before_call()
        # 同一时刻第二个操作必须冲突
        with pytest.raises(PixivOperationInProgress):
            gate.acquire()
    # 第二次操作：与上一次调用间隔不足 → 等待补足
    with gate.operation():
        gate.before_call()
        assert waits and waits[0] == pytest.approx(3.0)
    # 补足后再次调用无需等待
    clock.value += 3.0
    with gate.operation():
        gate.before_call()
        assert len(waits) == 1


def test_cancel_interrupts_interval_wait():
    gate, clock, waits = make_gate()
    with gate.operation():
        gate.before_call()
    with gate.operation():
        gate.cancel()
        with pytest.raises(PixivOperationCancelled):
            gate.before_call()


def test_cooldown_blocks_and_reports_remaining():
    gate, clock, _ = make_gate()
    gate.pause("rate_limited", cooldown_seconds=60)
    with pytest.raises(PixivPaused):
        gate.acquire()
    gate.resume()
    gate.pause("rate_limited", cooldown_seconds=60)
    # resume 清冷却；重新设置后 acquire 立即通过（paused 已清）→ 需要新的门
    gate.resume()
    gate._cooldown_until = clock.value + 60  # noqa: SLF001 直接构造冷却窗口
    with pytest.raises(PixivCooldownActive) as exc_info:
        gate.acquire()
    assert exc_info.value.remaining_seconds == pytest.approx(60.0)


def test_pause_then_resume():
    gate, _, _ = make_gate()
    gate.pause("auth_failed")
    with pytest.raises(PixivPaused) as exc_info:
        gate.acquire()
    assert exc_info.value.reason == "auth_failed"
    gate.resume()
    with gate.operation():
        gate.before_call()


def test_to_pixiv_error_mapping():
    cooldown = to_pixiv_error(PixivCooldownActive(123.0, 45.0))
    assert isinstance(cooldown, RateLimited)
    assert cooldown.cooldown_seconds == 45.0

    cancelled = to_pixiv_error(PixivOperationCancelled())
    assert isinstance(cancelled, TemporaryFailure)

    paused = to_pixiv_error(PixivPaused("auth_failed"))
    assert isinstance(paused, TemporaryFailure)
    assert "auth_failed" in str(paused)

    # NaN / Infinity 不得进入冷却时间
    assert to_pixiv_error(PixivCooldownActive(None, float("nan"))).cooldown_seconds is None
    assert to_pixiv_error(PixivCooldownActive(None, float("inf"))).cooldown_seconds is None


@pytest.fixture
def db_path(tmp_path):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return db_path


def _wait_terminal(runner, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.status()
        if status["state"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("run did not terminate")


def test_runner_rate_limited_pauses_gate_and_preserves_cooldown(db_path):
    gate = RequestGate(min_interval_seconds=0.0)
    adapter = FakeDiscoveryAdapter(
        {"exact_tag:a": RateLimited("429", cooldown_seconds=60)}
    )
    runner = DiscoveryRunner(
        adapter,
        ScanRepository(),
        DiscoveryRepository(),
        db_path,
        gate=gate,
        query_factory=lambda tag: [
            __import__("app.discovery.types", fromlist=["DiscoveryQuery"]).DiscoveryQuery(
                "exact_tag:a", "exact_tag", text="a"
            )
        ],
        source_name="pixiv",
    )
    runner.start()
    status = _wait_terminal(runner)
    assert status["state"] == "failed"
    assert status["current_run"]["error_code"].startswith("rate_limited")
    # 429 后门被暂停：新 run 不得再次出站
    assert gate.state()["paused_reason"] == "rate_limited"
    runner.start()
    blocked = _wait_terminal(runner)
    assert blocked["state"] == "failed"
    assert blocked["current_run"]["error_code"].startswith("pixiv_operation_paused")
    assert len(adapter.calls) == 1  # 只发出过一次适配器调用


def test_runner_auth_failed_pauses_until_revalidated(db_path):
    from app.pixiv.exceptions import AuthFailed

    gate = RequestGate(min_interval_seconds=0.0)
    adapter = FakeDiscoveryAdapter({"exact_tag:a": AuthFailed("401")})
    runner = DiscoveryRunner(
        adapter,
        ScanRepository(),
        DiscoveryRepository(),
        db_path,
        gate=gate,
        query_factory=lambda tag: [
            __import__("app.discovery.types", fromlist=["DiscoveryQuery"]).DiscoveryQuery(
                "exact_tag:a", "exact_tag", text="a"
            )
        ],
        source_name="pixiv",
    )
    runner.start()
    status = _wait_terminal(runner)
    assert status["current_run"]["error_code"] == "auth_failed"
    assert gate.state()["paused_reason"] == "auth_failed"
    # 用户重新验证成功 → resume 后可再次出站
    gate.resume()
    runner.start()
    final = _wait_terminal(runner)
    assert final["state"] == "failed"  # 仍失败但确实再次调用了适配器
    assert len(adapter.calls) == 2


class BlockingContentAdapter:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def novel_text(self, novel_id: int):
        self.entered.set()
        self.release.wait(timeout=5)
        from app.pixiv.adapter import NovelText

        return NovelText(novel_id=novel_id, text="正文" * 10)


def _make_track_target(db_path, novel_id: int) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at)"
            " VALUES (?, 't', '2026-09-09T00:00:00+00:00', '2026-09-09T00:00:00+00:00')",
            (novel_id,),
        )
        conn.execute(
            "INSERT INTO target_states (novel_id, state, updated_at)"
            " VALUES (?, 'track', '2026-09-09T00:00:00+00:00')",
            (novel_id,),
        )
    finally:
        conn.close()


def test_content_analyze_conflicts_with_discovery_gate(db_path):
    _make_track_target(db_path, 1)
    gate = RequestGate(min_interval_seconds=0.0)
    adapter = BlockingContentAdapter()
    service = ContentAnalysisService(
        ContentRepository(), db_path, adapter_factory=lambda: adapter, gate=gate
    )
    results: list = []
    thread = threading.Thread(
        target=lambda: results.append(service.analyze(1, force=True))
    )
    thread.start()
    assert adapter.entered.wait(timeout=5)
    # 授权/发现占用同一门时，第二个正文请求冲突而非排队
    with pytest.raises(ContentOperationConflict):
        service.analyze(1, force=True)
    adapter.release.set()
    thread.join(timeout=5)
    assert results and results[0][1] is False


def test_content_analyze_maps_gate_cooldown(db_path):
    _make_track_target(db_path, 2)
    gate = RequestGate(min_interval_seconds=0.0)
    gate._cooldown_until = time.monotonic() + 30  # noqa: SLF001
    service = ContentAnalysisService(
        ContentRepository(),
        db_path,
        adapter_factory=lambda: BlockingContentAdapter(),
        gate=gate,
    )
    with pytest.raises(ContentRateLimited) as exc_info:
        service.analyze(2, force=True)
    assert exc_info.value.cooldown_seconds is not None


def test_scheduler_skips_when_not_verified():
    from app.scheduler.discovery import DiscoveryScheduler

    class NeverRunner:
        def __init__(self) -> None:
            self.calls = 0

        def start(self, *, tag=None):
            self.calls += 1
            return ("k", "t")

    runner = NeverRunner()
    scheduler = DiscoveryScheduler(
        runner, interval_seconds=0.05, tag="ウマ娘", can_run=lambda: False
    )
    scheduler.start()
    time.sleep(0.2)
    scheduler.stop()
    assert runner.calls == 0
    status = scheduler.status()
    assert status["skipped_reason"] == "paused_or_not_verified"


def test_validator_user_initiated_resumes_paused_gate(monkeypatch):
    """用户显式"使用已保存登录"必须能在 401 暂停后发起并恢复出站。"""
    from app.main import _default_validator

    gate = RequestGate(min_interval_seconds=0.0)
    gate.pause("auth_failed")
    calls: list[str] = []

    def fake_factory(token):
        calls.append(token)
        return object()

    monkeypatch.setattr("app.pixiv.runtime._default_api_factory", fake_factory)
    _default_validator(gate)("fake-offline-token")
    assert calls == ["fake-offline-token"]
    assert gate.state()["paused_reason"] is None


def test_validator_auth_failure_pauses_gate(monkeypatch):
    from app.main import _default_validator
    from app.pixiv.exceptions import AuthFailed

    gate = RequestGate(min_interval_seconds=0.0)

    def fake_factory(token):
        raise AuthFailed("401")

    monkeypatch.setattr("app.pixiv.runtime._default_api_factory", fake_factory)
    validate = _default_validator(gate)
    with pytest.raises(AuthFailed):
        validate("fake-offline-token")
    assert gate.state()["paused_reason"] == "auth_failed"
    # 暂停只挡住自动任务；用户再次显式验证仍可发起。
    assert gate.state()["busy"] is False
