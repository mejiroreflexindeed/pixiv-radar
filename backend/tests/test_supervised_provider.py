"""WP3 GpptSupervisedAuthorizationProvider 测试：全流程 fake worker + 内存假凭据。

不联网、不登录、不读取真实凭据；token/state/session 不得出现在
API 响应、异常、repr、日志或测试输出。
"""

from __future__ import annotations

import sys
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth.providers import supervised as supervised_module
from app.auth.providers.supervised import GpptSupervisedAuthorizationProvider
from app.auth.service import AuthOperationError, AuthService, AuthState
from app.core.credentials import InMemoryCredentialStore
from app.core.config import get_settings
from app.main import create_app

FAKE_COMMAND = [sys.executable, "-m", "app.auth.worker_entry", "--mode", "fake"]
SENTINEL = "provider-fake-refresh-token-never-return"


@pytest.fixture(autouse=True)
def force_available(monkeypatch):
    """可用性由测试显式控制：依赖/Chromium 存在 + 真实登录门禁开启。"""
    monkeypatch.setattr(supervised_module, "gppt_installed", lambda: True)
    monkeypatch.setattr(supervised_module, "chromium_available", lambda: True)
    monkeypatch.setenv("RADAR_AUTH_ALLOW_REAL", "1")


def make_provider(**kwargs):
    kwargs.setdefault("worker_command", FAKE_COMMAND)
    return GpptSupervisedAuthorizationProvider(**kwargs)


def make_service(token=None, provider=None):
    store = InMemoryCredentialStore(token)
    provider = provider or make_provider()
    service = AuthService(store, provider, session_ttl=timedelta(minutes=5))
    return service, store, provider


def _wait_for_state(service, expected: AuthState, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.status()
        if status.state is expected:
            return status
        time.sleep(0.1)
    raise AssertionError(f"state not reached: {expected!r}")


def test_unavailable_provider_fails_closed(monkeypatch):
    monkeypatch.setattr(supervised_module, "gppt_installed", lambda: False)
    service, _, _ = make_service()
    assert service.status().login_available is False
    with pytest.raises(AuthOperationError) as exc_info:
        service.start()
    assert exc_info.value.code == "login_provider_unavailable"


def test_gate_off_means_unavailable_even_with_deps(monkeypatch):
    """门禁默认关闭：即使依赖与 Chromium 都在，也绝不报告登录可用。"""
    monkeypatch.delenv("RADAR_AUTH_ALLOW_REAL", raising=False)
    service, _, provider = make_service()
    assert supervised_module.real_login_gate_enabled() is False
    status = service.status()
    assert status.login_available is False
    assert status.provider_mode == "unavailable"
    with pytest.raises(AuthOperationError) as exc_info:
        service.start()
    assert exc_info.value.code == "login_provider_unavailable"
    # 未启动任何 worker/浏览器
    assert provider._supervisor is None  # noqa: SLF001


def test_gate_on_fake_flow_is_control_flow_only():
    """门禁开启 + fake worker：控制流可启动，但没有调用真实 gppt。"""
    assert supervised_module.real_login_gate_enabled() is True
    provider = make_provider(
        request_extra={"behavior": "delayed_success", "delay": 0.2, "token": SENTINEL}
    )
    service, store, _ = make_service(provider=provider)
    result = service.start()
    assert result.status.state is AuthState.AUTHORIZING
    status = _wait_for_state(service, AuthState.CONNECTED)
    assert store.get() == SENTINEL
    assert status.login_available is True


def test_full_fake_flow_connects_with_sentinel_only_in_store():
    provider = make_provider(
        request_extra={"behavior": "delayed_success", "delay": 0.2, "token": SENTINEL}
    )
    service, store, _ = make_service(provider=provider)
    result = service.start()
    assert result.status.state is AuthState.AUTHORIZING
    assert result.launch.mode == "supervised_browser"
    assert result.launch.browser_opened is True
    serialized = str(result.as_dict())
    assert SENTINEL not in serialized

    status = _wait_for_state(service, AuthState.CONNECTED)
    assert status.can_disconnect is True
    assert store.get() == SENTINEL
    assert SENTINEL not in str(status.as_dict())
    assert SENTINEL not in repr(service)
    assert SENTINEL not in repr(provider)


def test_wrong_state_echo_rejected_by_auth_service():
    provider = make_provider(
        request_extra={"behavior": "wrong_state", "token": SENTINEL}
    )
    service, store, _ = make_service(provider=provider)
    service.start()
    status = _wait_for_state(service, AuthState.USER_ACTION_REQUIRED)
    assert status.error_code == "authorization_state_mismatch"
    assert store.get() is None  # 错误 state 绝不写入凭据


@pytest.mark.parametrize("behavior", ["state_missing", "state_empty", "wrong_state"])
def test_callback_state_missing_empty_wrong_all_rejected(behavior):
    """缺失 / 空 / 错误 state：全部由 AuthService 校验拒绝，不补造。"""
    provider = make_provider(request_extra={"behavior": behavior, "token": SENTINEL})
    service, store, _ = make_service(provider=provider)
    service.start()
    status = _wait_for_state(service, AuthState.USER_ACTION_REQUIRED)
    assert status.error_code == "authorization_state_mismatch"
    assert store.get() is None


def test_callback_state_correct_accepts():
    provider = make_provider(
        request_extra={"behavior": "delayed_success", "delay": 0.1, "token": SENTINEL}
    )
    service, store, _ = make_service(provider=provider)
    service.start()
    _wait_for_state(service, AuthState.CONNECTED)
    assert store.get() == SENTINEL


def test_failure_mapping_browser_closed_and_runtime():
    cases = [
        ("fail_browser_closed", "browser_action_required"),
        ("fail_runtime", "browser_action_required"),
    ]
    for behavior, expected_code in cases:
        provider = make_provider(request_extra={"behavior": behavior})
        service, store, _ = make_service(provider=provider)
        service.start()
        status = _wait_for_state(service, AuthState.USER_ACTION_REQUIRED)
        assert status.error_code == expected_code
        assert store.get() is None


def test_crash_maps_to_authorization_failed():
    provider = make_provider(request_extra={"behavior": "crash"})
    service, store, _ = make_service(provider=provider)
    service.start()
    status = _wait_for_state(service, AuthState.USER_ACTION_REQUIRED)
    assert status.error_code == "authorization_failed"
    assert store.get() is None


def test_cancel_flow():
    provider = make_provider(request_extra={"behavior": "spawn_child_hold"})
    service, _, _ = make_service(provider=provider)
    service.start()
    cancelled = service.cancel()
    # 无既有凭据时取消回落到 not_configured；错误码必须为 authorization_cancelled
    assert cancelled.error_code == "authorization_cancelled"
    assert cancelled.state in (
        AuthState.USER_ACTION_REQUIRED,
        AuthState.NOT_CONFIGURED,
    )


def test_timeout_maps_to_authorization_timeout():
    provider = make_provider(
        request_extra={"behavior": "timeout"},
        timeout_seconds=2,
    )
    service, _, _ = make_service(provider=provider)
    service.start()
    status = _wait_for_state(service, AuthState.USER_ACTION_REQUIRED, timeout=15)
    assert status.error_code == "authorization_timeout"


def test_canary_no_sentinel_in_logs(caplog):
    """secret canary：假 token 不得进入任何日志输出。"""
    import logging

    provider = make_provider(
        request_extra={"behavior": "delayed_success", "delay": 0.1, "token": SENTINEL}
    )
    service, store, _ = make_service(provider=provider)
    with caplog.at_level(logging.DEBUG):
        service.start()
        _wait_for_state(service, AuthState.CONNECTED)
    assert store.get() == SENTINEL
    assert SENTINEL not in caplog.text


def test_default_app_gate_off_reports_unavailable(tmp_path, monkeypatch):
    """默认启动（门禁关闭）：API 诚实报告不可用，无 worker/浏览器启动。"""
    monkeypatch.delenv("RADAR_AUTH_ALLOW_REAL", raising=False)
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "state" / "radar.db"))
    monkeypatch.setenv("RADAR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_LOG_FORMAT", "text")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        status = client.get("/api/auth/status").json()
        assert status["login_available"] is False
        assert status["provider_mode"] == "unavailable"
        resp = client.post("/api/auth/start", json={"user_initiated": True})
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "login_provider_unavailable"
    get_settings.cache_clear()


def test_api_full_flow_with_fake_worker(tmp_path, monkeypatch):
    """API 级全流程：POST start → 轮询 → connected；响应无敏感值。"""
    provider = make_provider(
        request_extra={"behavior": "delayed_success", "delay": 0.2, "token": SENTINEL}
    )
    store = InMemoryCredentialStore()
    service = AuthService(store, provider, session_ttl=timedelta(minutes=5))
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "state" / "radar.db"))
    monkeypatch.setenv("RADAR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_LOG_FORMAT", "text")
    get_settings.cache_clear()
    with TestClient(create_app(auth_service=service)) as client:
        started = client.post("/api/auth/start", json={"user_initiated": True})
        assert started.status_code == 200
        assert started.json()["status"]["state"] == "authorizing"
        assert SENTINEL not in started.text

        connected = None
        for _ in range(150):
            status = client.get("/api/auth/status").json()
            if status["state"] == "connected":
                connected = status
                break
            time.sleep(0.1)
        assert connected is not None
        assert SENTINEL not in str(connected)
        assert store.get() == SENTINEL
    get_settings.cache_clear()
