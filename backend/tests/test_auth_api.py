"""登录 API 契约测试：请求必须显式，响应绝不含 token/state/session。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth.service import (
    AuthService,
    ProviderLaunch,
    UnavailableAuthorizationProvider,
)
from app.core.credentials import InMemoryCredentialStore
from app.core.config import get_settings
from app.main import create_app

SENTINEL_TOKEN = "api-fake-refresh-token-never-return"


class FakeOAuthProvider:
    available = True
    mode = "fake_oauth"

    def __init__(self) -> None:
        self.request = None
        self.complete = None

    def start(self, request, *, on_complete, on_failure):
        del on_failure
        self.request = request
        self.complete = on_complete
        return ProviderLaunch(mode=self.mode, browser_opened=False)

    def cancel(self, session_id: str) -> None:
        del session_id


def make_client(tmp_path, monkeypatch, service: AuthService):
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "state" / "radar.db"))
    monkeypatch.setenv("RADAR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_LOG_FORMAT", "text")
    get_settings.cache_clear()
    return TestClient(create_app(auth_service=service))


def test_auth_api_full_fake_flow_is_safe(tmp_path, monkeypatch):
    store = InMemoryCredentialStore()
    provider = FakeOAuthProvider()
    service = AuthService(store, provider)

    with make_client(tmp_path, monkeypatch, service) as client:
        status = client.get("/api/auth/status")
        assert status.status_code == 200
        assert status.json()["state"] == "not_configured"

        # 缺失 user_initiated=true 时不得启动。
        assert client.post("/api/auth/start", json={}).status_code == 422
        started = client.post(
            "/api/auth/start", json={"user_initiated": True}
        )
        assert started.status_code == 200
        assert started.json()["status"]["state"] == "authorizing"
        assert provider.request is not None
        assert provider.complete is not None
        for forbidden in (
            SENTINEL_TOKEN,
            provider.request.state,
            provider.request.session_id,
        ):
            assert forbidden not in started.text

        provider.complete(provider.request.state, SENTINEL_TOKEN)
        connected = client.get("/api/auth/status")
        assert connected.json()["state"] == "connected"
        assert connected.json()["can_disconnect"] is True
        assert SENTINEL_TOKEN not in connected.text

        # 二次确认是 API 契约，不只依赖前端 confirm 对话框。
        assert client.post(
            "/api/auth/disconnect", json={"confirm": False}
        ).status_code == 422
        disconnected = client.post(
            "/api/auth/disconnect", json={"confirm": True}
        )
        assert disconnected.status_code == 200
        assert disconnected.json()["state"] == "not_configured"
        assert store.get() is None

    get_settings.cache_clear()


def test_unavailable_provider_fails_closed_with_fixed_code(tmp_path, monkeypatch):
    service = AuthService(
        InMemoryCredentialStore(), UnavailableAuthorizationProvider()
    )
    with make_client(tmp_path, monkeypatch, service) as client:
        status = client.get("/api/auth/status").json()
        assert status["login_available"] is False
        response = client.post(
            "/api/auth/start", json={"user_initiated": True}
        )
        assert response.status_code == 503
        assert response.json() == {
            "detail": {"code": "login_provider_unavailable"}
        }
    get_settings.cache_clear()


def test_openapi_has_no_password_or_token_input(tmp_path, monkeypatch):
    service = AuthService(
        InMemoryCredentialStore(), UnavailableAuthorizationProvider()
    )
    with make_client(tmp_path, monkeypatch, service) as client:
        schema = client.get("/openapi.json").text.lower()
        assert "password" not in schema
        assert "refresh_token" not in schema
        assert "access_token" not in schema
    get_settings.cache_clear()
