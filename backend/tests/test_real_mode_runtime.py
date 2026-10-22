"""真实发现模式的离线运行时门禁测试；不读真实凭据、不联网。"""

import time

from fastapi.testclient import TestClient

from app.auth.service import AuthService, UnavailableAuthorizationProvider
from app.core.config import get_settings
from app.core.credentials import InMemoryCredentialStore
from app.main import create_app
from app.pixiv.request_gate import RequestGate
from app.storage.connection import connect


class CountingStore(InMemoryCredentialStore):
    def __init__(self):
        super().__init__(None)
        self.reads = 0

    def get(self):
        self.reads += 1
        return super().get()


def test_real_mode_is_lazy_and_gate_closed_without_credential_read(
    tmp_path, monkeypatch
):
    db = tmp_path / "real-mode" / "radar.db"
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_DISCOVERY_MODE", "real")
    monkeypatch.setenv("RADAR_AUTH_ALLOW_REAL", "0")
    get_settings.cache_clear()
    store = CountingStore()
    app = create_app(
        auth_service=AuthService(store, UnavailableAuthorizationProvider()),
        credential_store=store,
        request_gate=RequestGate(min_interval_seconds=0.0),
    )
    with TestClient(app) as client:
        status = client.get("/api/discovery/status").json()
        assert status["state"] == "idle"
        assert status["source"] == "pixiv"
        assert store.reads == 0

        started = client.post(
            "/api/discovery/start", params={"tag": "ウマ娘"}
        )
        assert started.status_code == 200
        assert started.json()["source"] == "pixiv"
        assert started.json()["run_key"].startswith("pixiv-")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get("/api/discovery/status").json()
            if current["state"] != "running":
                break
            time.sleep(0.02)
        assert current["state"] == "failed"
        assert current["current_run"]["error_code"] == "temporary_failure"
        assert store.reads == 0

    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT source, status FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert dict(row) == {"source": "pixiv", "status": "failed"}
    finally:
        conn.close()
    get_settings.cache_clear()
