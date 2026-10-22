"""B1/B2：授权/发现/正文/断开共享出站生命周期与冷却归一化的项目回归。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.auth.service import (
    AuthOperationError,
    AuthService,
    ProviderLaunch,
)
from app.content.service import ContentAnalysisService
from app.core.credentials import InMemoryCredentialStore
from app.main import _default_validator
from app.pixiv.exceptions import AuthFailed, RateLimited
from app.pixiv.request_gate import (
    RequestGate,
    normalize_cooldown_seconds,
)
from app.storage.connection import connect
from app.storage.content_repository import ContentRepository
from app.storage.migrations import apply_migrations


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "state" / "radar.db"
    conn = connect(path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return path


class FakeProvider:
    available = True
    mode = "fake_oauth"

    def __init__(self) -> None:
        self.starts: list[tuple] = []

    def start(self, request, *, on_complete, on_failure):
        self.starts.append((request, on_complete, on_failure))
        return ProviderLaunch(mode="fake_oauth", browser_opened=True)

    def cancel(self, session_id: str) -> None:
        del session_id


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class DateTimeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def make_service(*, gate=None, now=None, token=None):
    provider = FakeProvider()
    service = AuthService(
        InMemoryCredentialStore(token),
        provider,
        allow_real=lambda: True,
        gate=gate,
        now=now,
    )
    return service, provider


def test_gate_busy_blocks_login_start_without_starting_provider():
    gate = RequestGate(min_interval_seconds=0.0)
    service, provider = make_service(gate=gate)
    with gate.operation():
        with pytest.raises(AuthOperationError) as exc_info:
            service.start()
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "pixiv_operation_in_progress"
        assert provider.starts == []
    service.start()
    assert gate.state()["busy"] is True


def test_cancel_and_timeout_release_authorization_lease():
    gate = RequestGate(min_interval_seconds=0.0)
    service, _ = make_service(gate=gate)
    service.start()
    assert gate.state()["busy"] is True
    service.cancel()
    assert gate.state()["busy"] is False

    clock = DateTimeClock()
    service2, _ = make_service(gate=gate, now=clock)
    service2.start()
    assert gate.state()["busy"] is True
    clock.value = clock.value + timedelta(minutes=6)
    status = service2.status()
    assert status.state.value == "user_action_required"
    assert gate.state()["busy"] is False


def test_validate_conflicts_with_busy_gate():
    gate = RequestGate(min_interval_seconds=0.0)
    service, _ = make_service(
        gate=gate, token="saved-token", now=None
    )
    service._validator = _default_validator(gate)  # noqa: SLF001
    with gate.operation():
        with pytest.raises(AuthOperationError) as exc_info:
            service.validate()
        assert exc_info.value.status_code == 409


def test_fresh_login_resumes_auth_pause():
    gate = RequestGate(min_interval_seconds=0.0)
    service, provider = make_service(gate=gate)
    gate.pause("auth_failed")
    service.start()
    request, on_complete, _ = provider.starts[0]
    on_complete(request.state, "refresh-token-1")
    assert service.status().state.value == "connected"
    assert gate.state()["paused_reason"] is None


def test_login_start_is_refused_during_active_cooldown():
    gate = RequestGate(min_interval_seconds=0.0)
    service, provider = make_service(gate=gate)
    gate.pause("rate_limited", cooldown_seconds=60)
    with pytest.raises(AuthOperationError) as exc_info:
        service.start()
    assert exc_info.value.status_code == 429
    assert provider.starts == []


def test_fresh_login_after_cooldown_lapse_resumes_pause_keeps_nothing():
    clock = FakeClock()
    gate = RequestGate(min_interval_seconds=0.0, clock=clock)
    service, provider = make_service(gate=gate)
    gate.pause("rate_limited", cooldown_seconds=60)
    clock.value += 61.0
    service.start()
    request, on_complete, _ = provider.starts[0]
    on_complete(request.state, "refresh-token-2")
    assert service.status().state.value == "connected"
    assert gate.state()["paused_reason"] is None
    assert gate.state()["cooldown_remaining_seconds"] == 0.0


def test_auth_status_reports_shared_gate_cooldown():
    gate = RequestGate(min_interval_seconds=0.0)
    service, _ = make_service(gate=gate)
    gate.pause("rate_limited", cooldown_seconds=45)
    status = service.status()
    assert status.state.value == "rate_limited"
    assert status.cooldown_until is not None
    payload = status.as_dict()
    assert payload["cooldown_until"] is not None
    assert payload["error_code"] == "authorization_rate_limited"

    # auth_failed 没有冷却：不合成限流状态，仍由门守卫拒绝出站。
    gate2 = RequestGate(min_interval_seconds=0.0)
    service2, _ = make_service(gate=gate2)
    gate2.pause("auth_failed")
    assert service2.status().state.value != "rate_limited"


def test_refused_validate_resets_operation_state():
    clock = FakeClock()
    gate = RequestGate(min_interval_seconds=0.0, clock=clock)
    service, _ = make_service(gate=gate, now=None, token="saved-token")
    gate.pause("rate_limited", cooldown_seconds=60)
    with pytest.raises(AuthOperationError) as exc_info:
        service.validate()
    assert exc_info.value.status_code == 429
    assert service._operation_in_progress is False  # noqa: SLF001
    clock.value += 61.0
    status = service.validate()
    assert status.state.value == "connected"
    assert service.outbound_session_valid() is True


def test_normalize_cooldown_values():
    assert normalize_cooldown_seconds(0) == 0.0
    assert normalize_cooldown_seconds(60) == 60.0
    assert normalize_cooldown_seconds(None) == 900.0
    assert normalize_cooldown_seconds(-5) == 900.0
    assert normalize_cooldown_seconds(float("nan")) == 900.0
    assert normalize_cooldown_seconds(float("inf")) == 900.0
    assert normalize_cooldown_seconds(1e12) == 900.0
    assert normalize_cooldown_seconds("120") == 900.0


def test_pause_normalizes_and_resume_preserves_cooldown():
    clock = FakeClock()
    gate = RequestGate(min_interval_seconds=0.0, clock=clock)
    gate.pause("rate_limited")
    assert gate.state()["cooldown_remaining_seconds"] == 900.0
    gate.resume()
    assert gate.state()["paused_reason"] is None
    assert gate.state()["cooldown_remaining_seconds"] == 900.0
    clock.value += 900.0
    assert gate.state()["cooldown_remaining_seconds"] == 0.0


def _make_track_target(db_path, novel_id: int) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at)"
            " VALUES (?, 't', '2026-09-12T00:00:00+00:00',"
            " '2026-09-12T00:00:00+00:00')",
            (novel_id,),
        )
        conn.execute(
            "INSERT INTO target_states (novel_id, state, updated_at)"
            " VALUES (?, 'track', '2026-09-12T00:00:00+00:00')",
            (novel_id,),
        )
    finally:
        conn.close()


class _FailingTextAdapter:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def novel_text(self, novel_id: int):
        raise self._error


def test_content_auth_failure_pauses_shared_gate(db_path):
    _make_track_target(db_path, 21)
    gate = RequestGate(min_interval_seconds=0.0)
    service = ContentAnalysisService(
        ContentRepository(),
        db_path,
        adapter_factory=lambda: _FailingTextAdapter(AuthFailed("401")),
        gate=gate,
    )
    with pytest.raises(AuthFailed):
        service.analyze(21, force=True)
    assert gate.state()["paused_reason"] == "auth_failed"


def test_content_rate_limit_pauses_gate_with_cooldown(db_path):
    _make_track_target(db_path, 22)
    gate = RequestGate(min_interval_seconds=0.0)
    service = ContentAnalysisService(
        ContentRepository(),
        db_path,
        adapter_factory=lambda: _FailingTextAdapter(
            RateLimited("429", cooldown_seconds=45)
        ),
        gate=gate,
    )
    with pytest.raises(RateLimited):
        service.analyze(22, force=True)
    assert gate.state()["paused_reason"] == "rate_limited"
    assert gate.state()["cooldown_remaining_seconds"] == pytest.approx(
        45.0, abs=1.0
    )


def test_content_failure_commits_while_gate_still_held(db_path):
    """R11：正文 429 必须在仍持有本次门时提交，不能等释放后才 pause。"""
    _make_track_target(db_path, 41)
    gate = RequestGate(min_interval_seconds=0.0)
    observed: dict[str, object] = {}

    class _Adapter:
        def novel_text(self, novel_id: int):
            observed["busy_at_failure"] = gate.state()["busy"]
            raise RateLimited("429", cooldown_seconds=30)

    service = ContentAnalysisService(
        ContentRepository(),
        db_path,
        adapter_factory=lambda: _Adapter(),
        gate=gate,
    )
    with pytest.raises(RateLimited):
        service.analyze(41, force=True)
    assert observed["busy_at_failure"] is True
    assert gate.state()["paused_reason"] == "rate_limited"


def test_authorization_commits_epoch_before_releasing_gate():
    """R12：授权成功先提交新会话 epoch，再释放授权门。"""
    gate = RequestGate(min_interval_seconds=0.0)
    service, provider = make_service(gate=gate)
    service.start()
    epoch_before = gate.session_epoch()
    observed: dict[str, int] = {}
    original_release = gate.release

    def release_and_record():
        observed["epoch_at_release"] = gate.session_epoch()
        original_release()

    gate.release = release_and_record  # type: ignore[method-assign]
    request, on_complete, _ = provider.starts[0]
    on_complete(request.state, "refresh-token")
    assert service.status().state.value == "connected"
    assert observed["epoch_at_release"] == epoch_before + 1


def test_validation_aborts_when_session_changed_during_interval_wait():
    """R13：等待最小间隔期间断开，调用前复核世代，断开后新增认证 HTTP=0。"""
    calls: list[str] = []
    gate = RequestGate(min_interval_seconds=60.0)
    service, _ = make_service(gate=gate, token="saved-token")
    service._validator = lambda token: calls.append(token)  # noqa: SLF001
    gate._last_call_at = gate._clock()  # noqa: SLF001 制造最小间隔等待

    def wait_then_disconnect(seconds, cancel):
        service._generation += 1  # noqa: SLF001 等待期间断开/重新授权
        return False

    gate._wait = wait_then_disconnect  # type: ignore[method-assign]
    status = service.validate()
    assert calls == []
    assert status.state.value == "not_configured"


class CountingStore(InMemoryCredentialStore):
    def __init__(self, token="saved-token") -> None:
        super().__init__(token)
        self.reads = 0

    def get(self):
        self.reads += 1
        return super().get()


def test_adapters_check_gate_before_reading_credentials():
    from app.discovery.types import DiscoveryQuery
    from app.pixiv.discovery_pixivpy import CredentialedPixivDiscoveryAdapter
    from app.pixiv.exceptions import TemporaryFailure
    from app.pixiv.runtime import CredentialedPixivAdapter

    factory_calls: list[str] = []

    def factory(token: str):
        factory_calls.append(token)
        return object()

    gate = RequestGate(min_interval_seconds=0.0)
    gate.set_session_guard(lambda: False)

    discovery_store = CountingStore()
    discovery = CredentialedPixivDiscoveryAdapter(
        discovery_store,
        api_factory=factory,
        allow_real=lambda: True,
        gate=gate,
    )
    query = DiscoveryQuery("exact_tag:a", "exact_tag", text="a")
    with gate.operation():
        with pytest.raises(TemporaryFailure):
            discovery.fetch_page(query, None)
    assert discovery_store.reads == 0
    assert factory_calls == []

    content_store = CountingStore()
    content = CredentialedPixivAdapter(
        content_store,
        api_factory=factory,
        allow_real=lambda: True,
        gate=gate,
    )
    with gate.operation():
        with pytest.raises(TemporaryFailure):
            content.novel_text(1)
    assert content_store.reads == 0
    assert factory_calls == []


def test_cooldown_blocks_before_credential_read():
    from app.discovery.types import DiscoveryQuery
    from app.pixiv.discovery_pixivpy import CredentialedPixivDiscoveryAdapter
    from app.pixiv.exceptions import RateLimited as AdapterRateLimited

    gate = RequestGate(min_interval_seconds=0.0)
    store = CountingStore()
    adapter = CredentialedPixivDiscoveryAdapter(
        store, api_factory=lambda token: object(), allow_real=lambda: True, gate=gate
    )
    query = DiscoveryQuery("exact_tag:a", "exact_tag", text="a")
    with gate.operation():
        # 冷却在操作持有期间出现（例如另一路径刚收到 429）：读凭据前必须被拦。
        gate._cooldown_until = gate._clock() + 60  # noqa: SLF001
        with pytest.raises(AdapterRateLimited):
            adapter.fetch_page(query, None)
    assert store.reads == 0


def test_wrong_state_and_provider_failure_release_lease():
    from app.auth.service import AuthState, ProviderFailure

    gate = RequestGate(min_interval_seconds=0.0)
    service, provider = make_service(gate=gate)
    service.start()
    request, on_complete, _ = provider.starts[0]
    on_complete("wrong-state", "refresh-token")
    assert gate.state()["busy"] is False
    assert service.status().state.value == "user_action_required"

    service2, provider2 = make_service(gate=gate)
    service2.start()
    request2, _, on_failure = provider2.starts[0]
    on_failure(
        request2.state,
        ProviderFailure(
            state=AuthState.USER_ACTION_REQUIRED,
            error_code="login_provider_failed",
        ),
    )
    assert gate.state()["busy"] is False


class _SessionBumpingFailAdapter:
    """模拟：请求进行中会话被重建，随后旧请求才返回 401/429。"""

    def __init__(self, gate: RequestGate, error: Exception) -> None:
        self._gate = gate
        self._error = error

    def novel_text(self, novel_id: int):
        self._gate.begin_session()  # 旧请求执行期间发生断开/新登录
        raise self._error


def test_old_content_failures_cannot_pause_a_new_session(db_path):
    for novel_id, error in (
        (31, AuthFailed("401")),
        (32, RateLimited("429", cooldown_seconds=60)),
    ):
        _make_track_target(db_path, novel_id)
        gate = RequestGate(min_interval_seconds=0.0)
        service = ContentAnalysisService(
            ContentRepository(),
            db_path,
            adapter_factory=lambda error=error: _SessionBumpingFailAdapter(
                gate, error
            ),
            gate=gate,
        )
        with pytest.raises((AuthFailed, RateLimited)):
            service.analyze(novel_id, force=True)
        state = gate.state()
        assert state["paused_reason"] is None
        assert state["cooldown_remaining_seconds"] is None


def test_old_validator_success_cannot_resume_a_new_session():
    from app.main import _default_validator

    gate = RequestGate(min_interval_seconds=0.0)

    def factory(_token: str):
        gate.begin_session()  # 旧验证执行期间会话已被重建
        return object()

    import app.pixiv.runtime as runtime_module

    original = runtime_module._default_api_factory
    runtime_module._default_api_factory = factory
    try:
        gate.pause("auth_failed")
        _default_validator(gate)("stale-token")
    finally:
        runtime_module._default_api_factory = original
    # 旧成功不得清除新会话的暂停状态
    assert gate.state()["paused_reason"] == "auth_failed"


def test_old_discovery_failure_cannot_pause_a_new_session(db_path):
    import time as _time

    from app.discovery.runner import DiscoveryRunner
    from app.discovery.types import DiscoveryQuery
    from app.storage.discovery_repository import DiscoveryRepository
    from app.storage.scan_repository import ScanRepository

    gate = RequestGate(min_interval_seconds=0.0)

    class _ExplodingAdapter:
        def fetch_page(self, query, cursor):
            gate.begin_session()  # 旧 run 执行期间会话已被重建
            raise AuthFailed("401")

    runner = DiscoveryRunner(
        _ExplodingAdapter(),
        ScanRepository(),
        DiscoveryRepository(),
        db_path,
        gate=gate,
        query_factory=lambda tag: [
            DiscoveryQuery("exact_tag:a", "exact_tag", text="a")
        ],
    )
    runner.start(tag="x")
    deadline = _time.monotonic() + 5
    while _time.monotonic() < deadline and runner.status()["state"] == "running":
        _time.sleep(0.01)
    assert runner.status()["state"] != "running"
    assert gate.state()["paused_reason"] is None
