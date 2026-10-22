"""W02 认证边界测试：GET 零凭据读取、显式 validate、世代与失败语义。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.service import AuthOperationError, AuthService, AuthState
from app.core.config import get_settings
from app.core.credentials import InMemoryCredentialStore
from app.main import create_app
from app.pixiv.exceptions import AuthFailed, RateLimited

T0 = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)
TOKEN = "saved-refresh-token-sentinel"


class CountingStore(InMemoryCredentialStore):
    def __init__(self, token: str | None = None) -> None:
        super().__init__(token)
        self.reads = 0
        self.sets = 0
        self.deletes = 0

    def get(self):
        self.reads += 1
        return super().get()

    def set(self, token: str) -> None:
        self.sets += 1
        super().set(token)

    def delete(self) -> None:
        self.deletes += 1
        super().delete()


class UnavailableProvider:
    available = False
    mode = "unavailable"

    def start(self, request, *, on_complete, on_failure):
        raise AuthOperationError(503, "login_provider_unavailable")

    def cancel(self, session_id: str) -> None:
        del session_id


def make_service(
    token: str | None,
    *,
    allow_real: bool = True,
    validator=None,
    clock=None,
):
    store = CountingStore(token)
    service = AuthService(
        store,
        UnavailableProvider(),
        now=clock or (lambda: T0),
        allow_real=lambda: allow_real,
        validator=validator if validator is not None else (lambda token: None),
    )
    return service, store


def test_gate_closed_validate_reads_zero():
    service, store = make_service(TOKEN, allow_real=False)
    with pytest.raises(AuthOperationError) as exc_info:
        service.validate()
    assert exc_info.value.code == "login_provider_unavailable"
    assert store.reads == 0
    assert store.sets == 0


def test_validate_without_saved_login_is_controlled():
    service, store = make_service(None)
    status = service.validate()
    assert store.reads == 1
    assert status.state is AuthState.USER_ACTION_REQUIRED
    assert status.error_code == "pixiv_login_required"
    assert status.can_disconnect is False


def test_validate_auth_failed_preserves_token():
    def validator(token: str) -> None:
        raise AuthFailed("401")

    service, store = make_service(TOKEN, validator=validator)
    status = service.validate()
    assert status.state is AuthState.EXPIRED_OR_REVOKED
    assert status.error_code == "authorization_rejected"
    assert store.get() == TOKEN  # 不静默清空或替换


def test_validate_rate_limited_sets_cooldown():
    def validator(token: str) -> None:
        raise RateLimited("429", cooldown_seconds=120)

    service, store = make_service(TOKEN, validator=validator)
    status = service.validate()
    assert status.state is AuthState.RATE_LIMITED
    assert status.cooldown_until == T0 + timedelta(seconds=120)
    # 冷却期内再次 validate 直接受控拒绝，不重复出站
    with pytest.raises(AuthOperationError) as exc_info:
        service.validate()
    assert exc_info.value.status_code == 429


def test_validate_network_failure_is_temporary():
    def validator(token: str) -> None:
        raise RuntimeError("network down")

    service, store = make_service(TOKEN, validator=validator)
    status = service.validate()
    assert status.state is AuthState.TEMPORARILY_UNAVAILABLE
    assert store.get() == TOKEN


def test_late_validate_after_disconnect_cannot_reconnect():
    started = threading.Event()
    release = threading.Event()

    def validator(token: str) -> None:
        started.set()
        release.wait(timeout=5)

    service, store = make_service(TOKEN, validator=validator)
    results: list = []

    thread = threading.Thread(target=lambda: results.append(service.validate()))
    thread.start()
    assert started.wait(timeout=5)
    disconnected = service.disconnect()
    assert disconnected.state is AuthState.NOT_CONFIGURED
    release.set()
    thread.join(timeout=5)
    # 迟到的 validate 结果不得把状态改回 connected
    assert service.status().state is AuthState.NOT_CONFIGURED
    assert store.get() is None


def test_start_rejected_while_validate_in_progress():
    started = threading.Event()
    release = threading.Event()

    def validator(token: str) -> None:
        started.set()
        release.wait(timeout=5)

    service, _ = make_service(TOKEN, validator=validator)
    thread = threading.Thread(target=service.validate)
    thread.start()
    assert started.wait(timeout=5)
    with pytest.raises(AuthOperationError) as exc_info:
        service.start()
    assert exc_info.value.status_code == 409
    release.set()
    thread.join(timeout=5)


def test_api_status_get_never_reads_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "radar.db"))
    monkeypatch.setenv("RADAR_DISCOVERY_MODE", "real")
    monkeypatch.setenv("RADAR_AUTH_ALLOW_REAL", "0")
    monkeypatch.setenv("RADAR_DISCOVERY_INTERVAL_MINUTES", "0")
    get_settings.cache_clear()
    store = CountingStore(TOKEN)
    try:
        # 真实装配路径：create_app 自行构造 AuthService 与能力探测。
        application = create_app(credential_store=store)
        with TestClient(application) as client:
            for route in (
                "/api/health",
                "/api/auth/status",
                "/api/discovery/status",
                "/api/content/status",
            ):
                assert client.get(route).status_code == 200
            assert store.reads == 0
            body = client.get("/api/auth/status").json()
            assert body["state"] == "not_configured"
            assert body["validate_available"] is False
            assert body["unavailable_reason"] == "demo_mode"
    finally:
        get_settings.cache_clear()


def test_api_validate_explicit_post(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "radar.db"))
    monkeypatch.setenv("RADAR_DISCOVERY_MODE", "real")
    monkeypatch.setenv("RADAR_AUTH_ALLOW_REAL", "1")
    get_settings.cache_clear()
    store = CountingStore(TOKEN)
    try:
        service = AuthService(
            store,
            UnavailableProvider(),
            allow_real=lambda: True,
            validator=lambda token: None,
        )
        application = create_app(credential_store=store, auth_service=service)
        with TestClient(application) as client:
            assert client.post("/api/auth/validate", json={}).status_code == 422
            response = client.post(
                "/api/auth/validate", json={"user_initiated": True}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["state"] == "connected"
            assert TOKEN not in response.text
            assert store.reads == 1
    finally:
        get_settings.cache_clear()


def test_astra_readiness_probe_is_green(tmp_path, monkeypatch):
    """独立探针等价覆盖：GET 状态链零凭据读取。"""
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "radar.db"))
    monkeypatch.setenv("RADAR_DISCOVERY_MODE", "real")
    monkeypatch.setenv("RADAR_AUTH_ALLOW_REAL", "0")
    get_settings.cache_clear()
    store = CountingStore(None)
    try:
        application = create_app(
            credential_store=store,
            auth_service=AuthService(
                store,
                UnavailableProvider(),
                allow_real=lambda: False,
            ),
        )
        with TestClient(application) as client:
            assert store.reads == 0
            for route in (
                "/api/health",
                "/api/auth/status",
                "/api/discovery/status",
                "/api/content/status",
            ):
                assert client.get(route).status_code == 200
            assert store.reads == 0
    finally:
        get_settings.cache_clear()
