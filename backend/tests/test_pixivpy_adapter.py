"""PixivPyAdapter 离线测试：完全使用 fake pixivpy API 响应，不联网、不登录。"""

from types import SimpleNamespace

import pytest
from pixivpy3 import AppPixivAPI
from pixivpy3.utils import PixivError

from app.pixiv.adapter import NovelSummary
from app.pixiv.exceptions import (
    AuthFailed,
    MalformedResponse,
    NotFound,
    RateLimited,
    TemporaryFailure,
)
from app.pixiv.pixivpy_adapter import PixivPyAdapter


class FakeHttpError(Exception):
    def __init__(self, status_code: int, headers: dict | None = None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code, headers=headers or {}
        )


class FakeApi:
    def __init__(self):
        self.calls = []
        self.search_result = None
        self.search_error = None
        self.detail_result = None
        self.detail_error = None
        self.text_result = None

    def search_novel(self, **kwargs):
        self.calls.append(("search_novel", kwargs))
        if self.search_error is not None:
            raise self.search_error
        return self.search_result

    def novel_detail(self, novel_id):
        self.calls.append(("novel_detail", novel_id))
        if self.detail_error is not None:
            raise self.detail_error
        return self.detail_result

    def webview_novel(self, novel_id):
        self.calls.append(("webview_novel", novel_id))
        return self.text_result


def make_novel(**overrides):
    data = dict(
        id=1001,
        title="メジロアルダンの朝",
        caption="朝練前の静かな時間。",
        user=SimpleNamespace(id=9001, name="あるだん好き"),
        series=SimpleNamespace(id=5001, title="アルダン短編集"),
        tags=[SimpleNamespace(name="ウマ娘", translated_name="赛马娘")],
        text_length=12345,
        total_bookmarks=234,
        total_view=5678,
        total_comments=12,
        is_original=True,
        create_date="2026-08-01T00:00:00+00:00",
        x_restrict=0,
        visible=True,
        novel_ai_type=0,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


# ---- 字段映射 ----


def test_search_maps_all_fields():
    api = FakeApi()
    api.search_result = SimpleNamespace(novels=[make_novel()])
    adapter = PixivPyAdapter(api)

    results = adapter.search_novels("ウマ娘", limit=5)
    assert len(results) == 1
    novel = results[0]
    assert isinstance(novel, NovelSummary)
    assert novel.novel_id == 1001
    assert novel.title == "メジロアルダンの朝"
    assert novel.caption == "朝練前の静かな時間。"
    assert novel.author_id == 9001
    assert novel.author_name == "あるだん好き"
    assert novel.series_id == 5001
    assert novel.series_name == "アルダン短編集"
    assert novel.tags[0].name == "ウマ娘"
    assert novel.tags[0].translated_name == "赛马娘"
    assert novel.text_length == 12345
    assert novel.total_bookmarks == 234
    assert novel.total_view == 5678
    assert novel.total_comments == 12
    assert novel.is_original is True
    assert novel.create_date == "2026-08-01T00:00:00+00:00"
    assert novel.x_restrict == 0
    assert novel.visible is True
    assert novel.novel_ai_type == 0


def test_search_passes_query_params():
    api = FakeApi()
    api.search_result = SimpleNamespace(novels=[])
    adapter = PixivPyAdapter(api)
    adapter.search_novels(
        "ウマ娘",
        limit=5,
        search_target="exact_match_for_tags",
        sort="date_asc",
    )
    assert api.calls[0] == (
        "search_novel",
        {"word": "ウマ娘", "search_target": "exact_match_for_tags", "sort": "date_asc"},
    )


def test_search_empty_list_returns_empty():
    api = FakeApi()
    api.search_result = SimpleNamespace(novels=[])
    adapter = PixivPyAdapter(api)
    assert adapter.search_novels("ウマ娘") == []


def test_search_missing_novels_is_malformed():
    api = FakeApi()
    api.search_result = SimpleNamespace()
    adapter = PixivPyAdapter(api)
    with pytest.raises(MalformedResponse):
        adapter.search_novels("ウマ娘")


def test_search_novel_missing_id_is_malformed():
    api = FakeApi()
    api.search_result = SimpleNamespace(novels=[make_novel(id=None)])
    adapter = PixivPyAdapter(api)
    with pytest.raises(MalformedResponse):
        adapter.search_novels("ウマ娘")


def test_search_never_calls_text_interfaces():
    api = FakeApi()
    api.search_result = SimpleNamespace(novels=[make_novel()])
    adapter = PixivPyAdapter(api)
    adapter.search_novels("ウマ娘")
    called = {name for name, _ in api.calls}
    assert called == {"search_novel"}


# ---- 错误分类 ----


def test_auth_failed():
    api = FakeApi()
    api.search_error = FakeHttpError(401)
    adapter = PixivPyAdapter(api)
    with pytest.raises(AuthFailed):
        adapter.search_novels("ウマ娘")


def test_rate_limited_with_cooldown():
    api = FakeApi()
    api.search_error = FakeHttpError(429, headers={"Retry-After": "60"})
    adapter = PixivPyAdapter(api)
    with pytest.raises(RateLimited) as exc_info:
        adapter.search_novels("ウマ娘")
    assert exc_info.value.cooldown_seconds == 60.0


def test_rate_limited_string_based():
    api = FakeApi()
    api.search_error = Exception("too many requests")
    adapter = PixivPyAdapter(api)
    with pytest.raises(RateLimited):
        adapter.search_novels("ウマ娘")


def test_server_error_is_temporary():
    api = FakeApi()
    api.search_error = FakeHttpError(500)
    adapter = PixivPyAdapter(api)
    with pytest.raises(TemporaryFailure):
        adapter.search_novels("ウマ娘")


def test_network_error_is_temporary():
    api = FakeApi()
    api.search_error = ConnectionError("connection refused")
    adapter = PixivPyAdapter(api)
    with pytest.raises(TemporaryFailure):
        adapter.search_novels("ウマ娘")


def test_not_found():
    api = FakeApi()
    api.detail_error = FakeHttpError(404)
    adapter = PixivPyAdapter(api)
    with pytest.raises(NotFound):
        adapter.novel_detail(999999)


# ---- detail / text ----


def test_detail_maps_novel():
    api = FakeApi()
    api.detail_result = SimpleNamespace(novel=make_novel())
    adapter = PixivPyAdapter(api)
    assert adapter.novel_detail(1001).novel_id == 1001


def test_detail_missing_novel_is_malformed():
    api = FakeApi()
    api.detail_result = SimpleNamespace()
    adapter = PixivPyAdapter(api)
    with pytest.raises(MalformedResponse):
        adapter.novel_detail(1001)


def test_text_maps_novel():
    api = FakeApi()
    api.text_result = SimpleNamespace(text="本文")
    adapter = PixivPyAdapter(api)
    assert adapter.novel_text(1001).text == "本文"


def test_text_missing_is_malformed():
    api = FakeApi()
    api.text_result = SimpleNamespace()
    adapter = PixivPyAdapter(api)
    with pytest.raises(MalformedResponse):
        adapter.novel_text(1001)


# ---- PixivPy 3.7.5 真实行为：PixivError（无 status_code 属性，reason 内嵌 HTTP 码） ----


def test_pixiv_error_auth_401_is_auth_failed():
    api = FakeApi()
    api.search_error = PixivError(
        "[ERROR] auth() failed! check refresh_token.\nHTTP 401: {\"has_error\": true}",
        header={},
        body='{"has_error": true}',
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(AuthFailed):
        adapter.search_novels("ウマ娘")


def test_real_app_api_without_auth_is_auth_failed_without_network():
    adapter = PixivPyAdapter(AppPixivAPI())
    with pytest.raises(AuthFailed):
        adapter.search_novels("ウマ娘", limit=1)


def test_pixiv_error_auth_429_with_retry_after_is_rate_limited():
    api = FakeApi()
    api.search_error = PixivError(
        "[ERROR] auth() failed! check refresh_token.\nHTTP 429: rate limited",
        header={"Retry-After": "60"},
        body="rate limited",
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(RateLimited) as exc_info:
        adapter.search_novels("ウマ娘")
    assert exc_info.value.cooldown_seconds == 60.0


def test_pixiv_error_wrapped_network_error_is_temporary():
    api = FakeApi()
    api.search_error = PixivError(
        "requests GET https://app-api.pixiv.net/v1/search/novel error: "
        "ConnectionError('Connection refused')"
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(TemporaryFailure):
        adapter.search_novels("ウマ娘")


def test_pixiv_error_parse_json_failure_is_temporary():
    api = FakeApi()
    api.search_error = PixivError(
        "parse_json() error: Expecting value: line 1 column 1 (char 0)",
        header={},
        body="<html>challenge</html>",
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(TemporaryFailure):
        adapter.search_novels("ウマ娘")


# ---- PixivPy 真实行为：4xx/5xx 以 JSON error 体返回，不抛异常 ----


def test_search_error_body_rate_limit_is_rate_limited():
    api = FakeApi()
    api.search_result = SimpleNamespace(
        error=SimpleNamespace(reason="rate_limit", message="Rate Limit")
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(RateLimited):
        adapter.search_novels("ウマ娘")


def test_search_error_body_rate_limit_with_body_cooldown():
    api = FakeApi()
    api.search_result = SimpleNamespace(
        error=SimpleNamespace(
            reason="rate_limit",
            message="Rate Limit",
            rate_limit=SimpleNamespace(reset_after=30),
        )
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(RateLimited) as exc_info:
        adapter.search_novels("ウマ娘")
    assert exc_info.value.cooldown_seconds == 30.0


def test_search_error_body_forbidden_is_auth_failed():
    api = FakeApi()
    api.search_result = SimpleNamespace(
        error=SimpleNamespace(reason="forbidden", message="Authentication required")
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(AuthFailed):
        adapter.search_novels("ウマ娘")


def test_error_body_exception_never_echoes_token():
    api = FakeApi()
    api.search_result = SimpleNamespace(
        error=SimpleNamespace(
            reason="refresh_token=BODY_SECRET_SENTINEL",
            message="Authentication required",
        )
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(AuthFailed) as exc_info:
        adapter.search_novels("ウマ娘")
    assert "BODY_SECRET_SENTINEL" not in str(exc_info.value)


def test_search_error_body_unknown_is_temporary():
    api = FakeApi()
    api.search_result = SimpleNamespace(
        error=SimpleNamespace(reason="internal_server_error")
    )
    adapter = PixivPyAdapter(api)
    with pytest.raises(TemporaryFailure):
        adapter.search_novels("ウマ娘")


def test_detail_error_body_is_checked():
    api = FakeApi()
    api.detail_result = SimpleNamespace(error=SimpleNamespace(reason="rate_limit"))
    adapter = PixivPyAdapter(api)
    with pytest.raises(RateLimited):
        adapter.novel_detail(1001)


# ---- create_date 归一为 UTC ----


def test_create_date_normalized_to_utc():
    api = FakeApi()
    api.search_result = SimpleNamespace(
        novels=[make_novel(create_date="2026-08-01T00:00:00+09:00")]
    )
    adapter = PixivPyAdapter(api)
    novel = adapter.search_novels("ウマ娘")[0]
    assert novel.create_date == "2026-07-31T15:00:00+00:00"


def test_create_date_unparseable_becomes_none():
    api = FakeApi()
    api.search_result = SimpleNamespace(
        novels=[make_novel(create_date="not-a-date")]
    )
    adapter = PixivPyAdapter(api)
    novel = adapter.search_novels("ウマ娘")[0]
    assert novel.create_date is None
