"""Login-1 状态机测试：纯内存假 OAuth，不联网、不访问系统凭据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.auth.service import (
    AuthOperationError,
    AuthService,
    AuthState,
    ProviderFailure,
    ProviderLaunch,
)
from app.core.credentials import InMemoryCredentialStore

SENTINEL_TOKEN = "fake-refresh-sentinel-never-return"
T0 = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self) -> None:
        self.value = T0

    def __call__(self) -> datetime:
        return self.value


class FakeOAuthProvider:
    available = True
    mode = "fake_oauth"

    def __init__(self) -> None:
        self.request = None
        self.on_complete = None
        self.on_failure = None
        self.cancelled: list[str] = []

    def start(self, request, *, on_complete, on_failure):
        self.request = request
        self.on_complete = on_complete
        self.on_failure = on_failure
        return ProviderLaunch(mode=self.mode, browser_opened=False)

    def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    def succeed(
        self,
        *,
        token: str = SENTINEL_TOKEN,
        callback_state: str | None = None,
    ) -> None:
        assert self.request is not None
        assert self.on_complete is not None
        self.on_complete(callback_state or self.request.state, token)

    def fail(self, failure: ProviderFailure) -> None:
        assert self.request is not None
        assert self.on_failure is not None
        self.on_failure(self.request.state, failure)


def make_service(
    token: str | None = None,
    *,
    clock=None,
    allow_real: bool = True,
    validator=None,
    capability=None,
):
    store = CountingStore(token)
    provider = FakeOAuthProvider()
    service = AuthService(
        store,
        provider,
        session_ttl=timedelta(minutes=5),
        now=clock or (lambda: T0),
        allow_real=lambda: allow_real,
        validator=validator if validator is not None else (lambda token: None),
        capability=capability,
    )
    return service, store, provider


class CountingStore(InMemoryCredentialStore):
    """记录读取次数；验证 GET 边界不读凭据。"""

    def __init__(self, token: str | None = None) -> None:
        super().__init__(token)
        self.reads = 0

    def get(self) -> str | None:
        self.reads += 1
        return super().get()


def test_initial_status_does_not_claim_connected():
    service, _, _ = make_service()
    status = service.status()
    assert status.state is AuthState.NOT_CONFIGURED
    assert status.login_available is True
    assert status.can_disconnect is False


def test_status_never_reads_credential_and_validate_is_explicit():
    service, store, _ = make_service(SENTINEL_TOKEN)
    status = service.status()
    # GET 只读进程内状态：不读取凭据、不伪报 connected。
    assert status.state is AuthState.NOT_CONFIGURED
    assert status.can_disconnect is False
    assert store.reads == 0
    assert SENTINEL_TOKEN not in str(status.as_dict())
    assert SENTINEL_TOKEN not in repr(service)

    # 显式 validate 才读取并验证已保存登录。
    validated = service.validate()
    assert store.reads == 1
    assert validated.state is AuthState.CONNECTED
    assert validated.can_disconnect is True
    assert SENTINEL_TOKEN not in str(validated.as_dict())


def test_success_is_state_checked_single_use_and_safe_to_serialize():
    service, store, provider = make_service()
    result = service.start()
    assert result.status.state is AuthState.AUTHORIZING
    assert result.launch.mode == "fake_oauth"
    assert "session_id" not in result.as_dict()
    assert provider.request is not None
    assert provider.request.state not in str(result.as_dict())
    assert provider.request.session_id not in str(result.as_dict())
    assert provider.request.state not in repr(provider.request)
    assert provider.request.session_id not in repr(provider.request)

    provider.succeed()
    status = service.status()
    assert status.state is AuthState.CONNECTED
    assert status.can_disconnect is True
    assert store.get() == SENTINEL_TOKEN
    assert SENTINEL_TOKEN not in str(status.as_dict())

    # 同一个提供者回调再次到达时被忽略。
    provider.succeed(token="second-token-must-be-ignored")
    assert store.get() == SENTINEL_TOKEN


def test_state_mismatch_consumes_session_and_never_writes_token():
    service, store, provider = make_service()
    service.start()
    provider.succeed(callback_state="wrong-state")
    status = service.status()
    assert status.state is AuthState.USER_ACTION_REQUIRED
    assert status.error_code == "authorization_state_mismatch"
    assert store.get() is None

    provider.succeed()
    assert store.get() is None


def test_timeout_consumes_session_and_cancels_provider():
    clock = MutableClock()
    service, store, provider = make_service(clock=clock)
    service.start()
    clock.value = T0 + timedelta(minutes=5)
    status = service.status()
    assert status.state is AuthState.USER_ACTION_REQUIRED
    assert status.error_code == "authorization_timeout"
    assert len(provider.cancelled) == 1
    provider.succeed()
    assert store.get() is None


def test_cancel_and_disconnect_are_scoped():
    service, store, provider = make_service(SENTINEL_TOKEN)
    # GET 不读凭据：未验证前不宣称可断开。
    assert service.status().can_disconnect is False
    assert service.validate().can_disconnect is True
    service.start()
    cancelled = service.cancel()
    assert cancelled.state is AuthState.USER_ACTION_REQUIRED
    assert cancelled.error_code == "authorization_cancelled"
    assert store.get() == SENTINEL_TOKEN
    assert len(provider.cancelled) == 1

    disconnected = service.disconnect()
    assert disconnected.state is AuthState.NOT_CONFIGURED
    assert disconnected.can_disconnect is False
    assert store.get() is None


def test_rate_limit_failure_blocks_retry_until_cooldown():
    clock = MutableClock()
    service, _, provider = make_service(clock=clock)
    service.start()
    provider.fail(
        ProviderFailure(
            AuthState.RATE_LIMITED,
            "authorization_rate_limited",
            cooldown_until=T0 + timedelta(minutes=2),
        )
    )
    status = service.status()
    assert status.state is AuthState.RATE_LIMITED
    assert status.cooldown_until == T0 + timedelta(minutes=2)
    with pytest.raises(AuthOperationError) as exc_info:
        service.start()
    assert exc_info.value.status_code == 429


def test_unapproved_failure_code_is_normalized():
    service, _, provider = make_service()
    service.start()
    provider.fail(
        ProviderFailure(
            AuthState.TEMPORARILY_UNAVAILABLE,
            "upstream-secret-detail-must-not-pass",
        )
    )
    status = service.status()
    assert status.state is AuthState.USER_ACTION_REQUIRED
    assert status.error_code == "authorization_failed"
    assert "upstream-secret" not in str(status.as_dict())


def test_provider_metadata_is_allowlisted_before_api_serialization():
    class UnsafeMetadataProvider(FakeOAuthProvider):
        mode = "mode-contains-sensitive-provider-detail"

    provider = UnsafeMetadataProvider()
    service = AuthService(InMemoryCredentialStore(), provider)
    status = service.status()
    assert status.login_available is False
    assert status.provider_mode == "unavailable"
    assert "sensitive-provider-detail" not in str(status.as_dict())
    with pytest.raises(AuthOperationError) as exc_info:
        service.start()
    assert exc_info.value.code == "login_provider_unavailable"
