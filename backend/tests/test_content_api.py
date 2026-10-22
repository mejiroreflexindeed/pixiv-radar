from fastapi.testclient import TestClient

from app.auth.service import AuthService, UnavailableAuthorizationProvider
from app.core.config import get_settings
from app.core.credentials import InMemoryCredentialStore
from app.main import create_app
from app.pixiv.adapter import NovelText
from app.storage.connection import connect, transaction


class TextAdapter:
    def __init__(self, text: str | dict[int, str]):
        self.text = text
        self.calls = 0

    def novel_text(self, novel_id: int) -> NovelText:
        self.calls += 1
        value = self.text[novel_id] if isinstance(self.text, dict) else self.text
        return NovelText(novel_id=novel_id, text=value)


def _insert_target(db, novel_id: int, state: str) -> None:
    conn = connect(db)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at) VALUES (?, ?, 'now', 'now')",
                (novel_id, f"target-{novel_id}"),
            )
            conn.execute(
                "INSERT INTO target_states (novel_id, state, updated_at) VALUES (?, ?, 'now')",
                (novel_id, state),
            )
    finally:
        conn.close()


def _app(tmp_path, monkeypatch, adapter=None):
    db = tmp_path / "state" / "content.db"
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_DISCOVERY_MODE", "demo")
    get_settings.cache_clear()
    store = InMemoryCredentialStore()
    app = create_app(
        auth_service=AuthService(store, UnavailableAuthorizationProvider()),
        content_adapter=adapter,
    )
    return app, db


def test_content_analysis_is_track_only_cached_and_never_returns_body(tmp_path, monkeypatch):
    body = ("トレーナーとウマ娘。しかし二人は走った。\n\n「また明日」\n\nその後。" * 20)
    adapter = TextAdapter(body)
    app, db = _app(tmp_path, monkeypatch, adapter)
    with TestClient(app) as client:
        _insert_target(db, 1, "track")
        _insert_target(db, 2, "broad")
        assert client.get("/api/content/status").json() == {"available": True}

        rejected = client.post("/api/content/2/analyze")
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "target_not_in_track"
        assert adapter.calls == 0

        first = client.post("/api/content/1/analyze")
        assert first.status_code == 200
        payload = first.json()
        assert payload["cached"] is False
        assert payload["novel_id"] == 1
        assert "body_text" not in payload
        assert "content_hash" not in payload
        assert adapter.calls == 1

        cached = client.post("/api/content/1/analyze")
        assert cached.json()["cached"] is True
        assert adapter.calls == 1
        loaded = client.get("/api/content/1")
        assert loaded.status_code == 200
        assert loaded.json()["reason_codes"] == payload["reason_codes"]

        forced = client.post("/api/content/1/analyze", params={"force": True})
        assert forced.status_code == 200
        assert forced.json()["cached"] is False
        assert adapter.calls == 2

    conn = connect(db)
    try:
        assert conn.execute("SELECT body_text FROM content_documents WHERE novel_id=1").fetchone()[0] == body
    finally:
        conn.close()
    get_settings.cache_clear()


def test_demo_mode_reports_unavailable_without_touching_an_adapter(tmp_path, monkeypatch):
    app, db = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _insert_target(db, 1, "track")
        assert client.get("/api/content/status").json() == {"available": False}
        missing = client.get("/api/content/1")
        assert missing.status_code == 404
        result = client.post("/api/content/1/analyze")
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "content_analysis_unavailable"
        assert client.get("/api/content/999").status_code == 404
    get_settings.cache_clear()


def test_similar_endpoint_ranks_cached_text_without_network(tmp_path, monkeypatch):
    common = "トレーナーとウマ娘は朝のコースを走った。そして二人で明日の約束をした。"
    adapter = TextAdapter({
        1: common * 20,
        2: (common + "「また一緒に走ろう」") * 20,
        3: "トレーナーが読む宇宙船の燃料ポンプと軌道計算の整備記録。" * 30,
    })
    app, db = _app(tmp_path, monkeypatch, adapter)
    with TestClient(app) as client:
        for novel_id in (1, 2, 3):
            _insert_target(db, novel_id, "track")
            assert client.post(f"/api/content/{novel_id}/analyze").status_code == 200
        assert adapter.calls == 3
        response = client.get("/api/content/1/similar", params={"limit": 2})
        assert response.status_code == 200
        payload = response.json()
        assert payload["method"] == "char-ngram-tfidf-v1"
        assert payload["note"] == "text_similarity_not_quality_or_semantic_judgment"
        assert payload["items"][0]["novel_id"] == 2
        assert payload["items"][0]["similarity"] > payload["items"][1]["similarity"]
        assert adapter.calls == 3
    get_settings.cache_clear()


class FailingTextAdapter:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def novel_text(self, novel_id: int):
        raise self.error


def test_session_invalid_and_gate_paused_have_accurate_codes(tmp_path, monkeypatch):
    from app.pixiv.exceptions import TemporaryFailure

    app, db = _app(
        tmp_path,
        monkeypatch,
        FailingTextAdapter(TemporaryFailure("pixiv_session_invalid")),
    )
    with TestClient(app) as client:
        _insert_target(db, 3, "track")
        response = client.post("/api/content/3/analyze")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "pixiv_session_invalid"

    app2, db2 = _app(
        tmp_path,
        monkeypatch,
        FailingTextAdapter(TemporaryFailure("pixiv_operation_paused:auth_failed")),
    )
    with TestClient(app2) as client:
        _insert_target(db2, 4, "track")
        response = client.post("/api/content/4/analyze")
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["code"] == "pixiv_operation_paused"
        assert detail["reason"] == "auth_failed"
    get_settings.cache_clear()
