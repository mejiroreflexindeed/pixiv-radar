"""SmokeScanService 离线测试：受限真实冒烟入口（全部 fake，不联网、不读真实凭据）。"""

from types import SimpleNamespace

import pytest
from pixivpy3.utils import PixivError

from app.domain.smoke_scan import SMOKE_TAG, SmokeScanService
from app.storage.connection import connect
from app.storage.migrations import apply_migrations
from app.storage.scan_repository import ScanRepository


class FakeStore:
    def __init__(self, token: str | None):
        self._token = token

    def get(self):
        return self._token

    def set(self, token: str) -> None:
        self._token = token

    def delete(self) -> None:
        self._token = None


class FailingStore:
    def get(self):
        raise RuntimeError("credential backend unavailable")


class RecordingApi:
    def __init__(self, search_result=None, search_error=None):
        self.calls = []
        self.search_result = search_result
        self.search_error = search_error

    def search_novel(self, **kwargs):
        self.calls.append(("search_novel", kwargs))
        if self.search_error:
            raise self.search_error
        return self.search_result

    def novel_detail(self, novel_id):
        self.calls.append(("novel_detail", novel_id))
        return None

    def webview_novel(self, novel_id):
        self.calls.append(("webview_novel", novel_id))
        return None


class ApiFactory:
    def __init__(self, api=None, error=None):
        self.api = api
        self.error = error
        self.called_tokens = []

    def __call__(self, token):
        self.called_tokens.append(token)
        if self.error:
            raise self.error
        return self.api


def make_novel(novel_id: int):
    return SimpleNamespace(
        id=novel_id,
        title="タイトル",
        user=SimpleNamespace(id=1, name="作者"),
        tags=[],
        text_length=100,
        total_bookmarks=1,
        total_view=2,
        total_comments=0,
    )


@pytest.fixture
def db_path(tmp_path):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return db_path


def test_missing_token_aborts_without_network(db_path):
    store = FakeStore(None)
    factory = ApiFactory(api=RecordingApi())
    service = SmokeScanService(store, factory, ScanRepository(), db_path)
    result = service.run()
    assert result.status == "aborted"
    assert factory.called_tokens == []  # 没有 token 时绝不尝试登录


def test_credential_read_failure_aborts_without_network(db_path):
    factory = ApiFactory(api=RecordingApi())
    service = SmokeScanService(FailingStore(), factory, ScanRepository(), db_path)
    result = service.run()
    assert result.status == "aborted"
    assert "凭据读取失败" in result.message
    assert factory.called_tokens == []


def test_success_uses_single_page_and_no_text(db_path):
    api = RecordingApi(
        search_result=SimpleNamespace(
            novels=[make_novel(1001), make_novel(1002)]
        )
    )
    factory = ApiFactory(api=api)
    service = SmokeScanService(
        FakeStore("tok123"), factory, ScanRepository(), db_path
    )
    result = service.run()
    assert result.status == "succeeded"

    assert factory.called_tokens == ["tok123"]
    search_calls = [call for call in api.calls if call[0] == "search_novel"]
    assert len(search_calls) == 1  # 单页：只调用一次
    assert search_calls[0][1]["word"] == SMOKE_TAG
    # 不取正文
    assert all(name != "webview_novel" for name, _ in api.calls)
    assert all(name != "novel_text" for name, _ in api.calls)

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"]
    finally:
        conn.close()
    assert n == 2


def test_rate_limited_aborts_with_no_writes(db_path):
    api = RecordingApi(
        search_result=SimpleNamespace(error=SimpleNamespace(reason="rate_limit"))
    )
    service = SmokeScanService(
        FakeStore("tok123"), ApiFactory(api=api), ScanRepository(), db_path
    )
    result = service.run()
    assert result.status == "aborted"

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"]
    finally:
        conn.close()
    assert n == 0


def test_auth_failure_aborts_no_scan_run(db_path):
    factory = ApiFactory(
        error=PixivError(
            "[ERROR] auth() failed! check refresh_token.\n"
            'HTTP 401: {"has_error": true}'
        )
    )
    service = SmokeScanService(
        FakeStore("tok123"), factory, ScanRepository(), db_path
    )
    result = service.run()
    assert result.status == "aborted"
    assert result.message

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM scan_runs").fetchone()["n"]
    finally:
        conn.close()
    assert n == 0  # 登录失败不产生任何扫描记录


def test_auth_rate_limit_preserves_retry_after(db_path):
    factory = ApiFactory(
        error=PixivError(
            "HTTP 429: rate limited",
            header={"Retry-After": "60"},
            body="rate limited",
        )
    )
    service = SmokeScanService(
        FakeStore("tok123"), factory, ScanRepository(), db_path
    )
    result = service.run()
    assert result.status == "aborted"
    assert result.cooldown_seconds == 60.0


def test_auth_unauthenticated_classified_as_auth_failed(db_path):
    """认证阶段复用统一分类：require_auth 的 "Authentication required"
    （无 HTTP 状态码）必须归类为 AuthFailed，而不是临时故障。"""
    factory = ApiFactory(
        error=PixivError(
            "Authentication required! Call login() or set_auth() first!"
        )
    )
    service = SmokeScanService(
        FakeStore("tok123"), factory, ScanRepository(), db_path
    )
    result = service.run()
    assert result.status == "aborted"
    assert "auth" in result.message.lower()
    assert result.cooldown_seconds is None

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM scan_runs").fetchone()["n"]
    finally:
        conn.close()
    assert n == 0


def test_auth_error_message_never_contains_token(db_path):
    class LeakyFactory:
        def __call__(self, token):
            raise PixivError(f"auth failed: refresh_token={token}")

    service = SmokeScanService(
        FakeStore("TOPSECRETTOKEN"), LeakyFactory(), ScanRepository(), db_path
    )
    result = service.run()
    assert result.status == "aborted"
    assert "TOPSECRETTOKEN" not in result.message
