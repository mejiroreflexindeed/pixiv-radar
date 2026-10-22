"""真实 discovery 适配器的完全离线测试：只使用 fake API/凭据。"""

from types import SimpleNamespace

import pytest

from app.core.credentials import InMemoryCredentialStore
from app.discovery import make_query_key
from app.discovery.types import DiscoveryQuery
from app.pixiv.discovery_pixivpy import (
    CredentialedPixivDiscoveryAdapter,
    PixivPyDiscoveryAdapter,
)
from app.pixiv.exceptions import AuthFailed, MalformedResponse, RateLimited, TemporaryFailure


def query(kind="exact_tag", text="ウマ娘"):
    return DiscoveryQuery(
        query_key=make_query_key(kind, text),
        source_kind=kind,
        text=text,
        limit=2,
    )


def novel(novel_id: int):
    return SimpleNamespace(
        id=novel_id,
        title=f"novel-{novel_id}",
        caption="",
        user=SimpleNamespace(id=9, name="author"),
        series=None,
        tags=[SimpleNamespace(name="ウマ娘", translated_name=None)],
        text_length=1000,
        total_bookmarks=10,
        total_view=100,
        total_comments=0,
        is_original=True,
        create_date="2026-09-01T00:00:00+00:00",
        x_restrict=0,
        visible=True,
        novel_ai_type=0,
    )


class FakeApi:
    def __init__(self):
        self.calls = []
        self.response = SimpleNamespace(novels=[])

    def search_novel(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_real_adapter_maps_page_limit_and_opaque_cursor():
    api = FakeApi()
    api.response = SimpleNamespace(
        novels=[novel(1), novel(2), novel(3)],
        next_url=(
            "https://app-api.pixiv.net/v1/search/novel?offset=30"
            "&access_token=must-not-survive"
        ),
    )
    adapter = PixivPyDiscoveryAdapter(api)
    first = adapter.fetch_page(query(), None)
    assert [item.novel_id for item in first.items] == [1, 2]
    assert first.next_cursor is not None
    assert "must-not-survive" not in first.next_cursor
    assert "pixiv.net" not in first.next_cursor
    assert api.calls[0] == {
        "word": "ウマ娘",
        "search_target": "exact_match_for_tags",
        "sort": "date_desc",
    }

    api.response = SimpleNamespace(novels=[], next_url=None)
    second = adapter.fetch_page(query(), first.next_cursor)
    assert second.items == ()
    assert api.calls[1] == {
        "word": "ウマ娘",
        "search_target": "exact_match_for_tags",
        "sort": "date_desc",
        "offset": "30",
    }


def test_cursor_is_bound_to_source_kind():
    api = FakeApi()
    api.response = SimpleNamespace(
        novels=[], next_url="https://x.invalid/path?offset=30"
    )
    adapter = PixivPyDiscoveryAdapter(api)
    cursor = adapter.fetch_page(query(), None).next_cursor
    with pytest.raises(MalformedResponse, match="scope mismatch"):
        adapter.fetch_page(query("keyword", "ウマ娘"), cursor)


def test_response_shape_and_error_body_are_classified():
    api = FakeApi()
    adapter = PixivPyDiscoveryAdapter(api)
    api.response = SimpleNamespace()
    with pytest.raises(MalformedResponse):
        adapter.fetch_page(query(), None)
    api.response = SimpleNamespace(
        error=SimpleNamespace(reason="rate_limit", rate_limit={"retry_after": 20})
    )
    with pytest.raises(RateLimited):
        adapter.fetch_page(query(), None)


def test_credentialed_adapter_gate_precedes_credential_read():
    class ExplodingStore:
        def get(self):
            raise AssertionError("gate off must not read credential")

    adapter = CredentialedPixivDiscoveryAdapter(
        ExplodingStore(), allow_real=lambda: False
    )
    with pytest.raises(TemporaryFailure, match="real_discovery_not_approved"):
        adapter.fetch_page(query(), None)


def test_credentialed_adapter_missing_token_is_auth_failed():
    adapter = CredentialedPixivDiscoveryAdapter(
        InMemoryCredentialStore(), allow_real=lambda: True
    )
    with pytest.raises(AuthFailed, match="credential missing"):
        adapter.fetch_page(query(), None)


def test_credentialed_adapter_authenticates_once_per_run_without_repr_leak():
    sentinel = "refresh-token-must-never-appear"
    api = FakeApi()
    factory_calls = []

    def factory(token):
        factory_calls.append(token)
        return api

    adapter = CredentialedPixivDiscoveryAdapter(
        InMemoryCredentialStore(sentinel),
        api_factory=factory,
        allow_real=lambda: True,
    )
    adapter.fetch_page(query(), None)
    adapter.fetch_page(query("partial_tag", "ウマ娘"), None)
    assert factory_calls == [sentinel]
    assert sentinel not in repr(adapter)
