"""PixivPy 真实发现适配器。

业务层只看到项目自己的 ``DiscoveryPage`` 和不透明 cursor；Pixiv 的
``next_url``、原始响应对象和异常正文不会离开本模块。真正的凭据读取、认证与
网络调用保持惰性：只有显式 real 模式的一次发现运行才会触发。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from ..core.credentials import CredentialStore, CredentialUnavailable
from ..discovery.types import DiscoveryPage, DiscoveryQuery
from .exceptions import (
    AuthFailed,
    MalformedResponse,
    PixivAdapterError,
    TemporaryFailure,
)
from .pixivpy_adapter import PixivPyAdapter, classify_pixiv_exception
from .request_gate import to_pixiv_error

_SEARCH_TARGET = {
    "exact_tag": "exact_match_for_tags",
    "partial_tag": "partial_match_for_tags",
    "keyword": "keyword",
}

# 仅保留端点公开签名中的分页字段。不得持久化 next_url、host 或任意参数。
_CURSOR_FIELDS: dict[str, tuple[str, ...]] = {
    "exact_tag": ("offset",),
    "partial_tag": ("offset",),
    "keyword": ("offset",),
    "novel_new": ("max_novel_id",),
    "novel_recommended": (
        "offset",
        "already_recommended",
        "max_bookmark_id_for_recommend",
    ),
    "author_novels": ("offset",),
    "series_novels": ("last_order",),
    "novel_follow": ("offset", "restrict"),
}
_NUMERIC_CURSOR_FIELDS = {
    "offset",
    "max_novel_id",
    "max_bookmark_id_for_recommend",
    "last_order",
}
_CURSOR_MAX = 500


def _get(obj, name: str, default=None):
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return default if value is None else value


def _encode_cursor(source_kind: str, next_url: str | None) -> str | None:
    if not next_url:
        return None
    parsed = parse_qs(urlparse(str(next_url)).query)
    params: dict[str, str] = {}
    for key in _CURSOR_FIELDS[source_kind]:
        values = parsed.get(key)
        if not values:
            continue
        value = str(values[-1])
        if not value or len(value) > 160:
            raise MalformedResponse("discovery cursor value invalid")
        if key in _NUMERIC_CURSOR_FIELDS and not value.isdigit():
            raise MalformedResponse("discovery cursor number invalid")
        if key == "restrict" and value not in {"public", "private", "all"}:
            raise MalformedResponse("discovery cursor restrict invalid")
        params[key] = value
    if not params:
        return None
    payload = json.dumps(
        {"v": 1, "source_kind": source_kind, "params": params},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    cursor = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    if len(cursor) > _CURSOR_MAX:
        raise MalformedResponse("discovery cursor too long")
    return cursor


def _decode_cursor(query: DiscoveryQuery, cursor: str | None) -> dict[str, str]:
    if cursor is None:
        return {}
    if not isinstance(cursor, str) or not cursor or len(cursor) > _CURSOR_MAX:
        raise MalformedResponse("discovery cursor invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("ascii"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise MalformedResponse("discovery cursor invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("source_kind") != query.source_kind
        or not isinstance(payload.get("params"), dict)
    ):
        raise MalformedResponse("discovery cursor scope mismatch")
    allowed = set(_CURSOR_FIELDS[query.source_kind])
    params: dict[str, str] = {}
    for key, raw in payload["params"].items():
        if key not in allowed or not isinstance(raw, str) or not raw or len(raw) > 160:
            raise MalformedResponse("discovery cursor field invalid")
        if key in _NUMERIC_CURSOR_FIELDS and not raw.isdigit():
            raise MalformedResponse("discovery cursor number invalid")
        if key == "restrict" and raw not in {"public", "private", "all"}:
            raise MalformedResponse("discovery cursor restrict invalid")
        params[key] = raw
    return params


class PixivPyDiscoveryQueryMapper:
    """DiscoveryQuery → (PixivPy 方法名, 参数) 的纯映射。"""

    @staticmethod
    def map(query: DiscoveryQuery) -> tuple[str, dict]:
        kind = query.source_kind
        if kind in _SEARCH_TARGET:
            return (
                "search_novel",
                {
                    "word": query.text or "",
                    "search_target": query.search_target or _SEARCH_TARGET[kind],
                    "sort": "date_desc",
                },
            )
        if kind == "novel_new":
            return ("novel_new", {})
        if kind == "novel_recommended":
            return ("novel_recommended", {})
        if kind == "author_novels":
            return ("user_novels", {"user_id": query.entity_id})
        if kind == "series_novels":
            return ("novel_series", {"series_id": query.entity_id})
        if kind == "novel_follow":
            return ("novel_follow", {})
        raise ValueError(f"unsupported source_kind: {kind!r}")


class PixivPyDiscoveryAdapter:
    """已认证 PixivPy API 的一页发现适配器。"""

    source_name = "pixiv"

    def __init__(self, api) -> None:
        self._api = api
        self._mapper = PixivPyAdapter(api)

    def fetch_page(
        self, query: DiscoveryQuery, cursor: str | None
    ) -> DiscoveryPage:
        method_name, params = PixivPyDiscoveryQueryMapper.map(query)
        method = getattr(self._api, method_name, None)
        if not callable(method):
            raise TemporaryFailure("discovery_source_unavailable")
        params.update(_decode_cursor(query, cursor))
        response = self._mapper._call(method, **params)
        self._mapper._raise_for_error_body(response)
        novels = _get(response, "novels")
        if novels is None:
            raise MalformedResponse("discovery response missing novels")
        if not isinstance(novels, (list, tuple)):
            raise MalformedResponse("discovery novels invalid")
        items = tuple(
            self._mapper._to_summary(item) for item in novels[: query.limit]
        )
        next_cursor = _encode_cursor(
            query.source_kind, _get(response, "next_url")
        )
        return DiscoveryPage(items=items, next_cursor=next_cursor)


def _default_api_factory(token: str):
    from pixivpy3 import AppPixivAPI

    api = AppPixivAPI(timeout=15)
    api.set_accept_language("zh-cn")
    api.auth(refresh_token=token)
    return api


class CredentialedPixivDiscoveryAdapter:
    """一次 discovery run 内惰性认证并复用连接的真实适配器。"""

    source_name = "pixiv"

    def __init__(
        self,
        credential_store: CredentialStore,
        *,
        api_factory: Callable[[str], object] | None = None,
        allow_real: Callable[[], bool] | None = None,
        gate=None,
    ) -> None:
        self._store = credential_store
        self._api_factory = api_factory or _default_api_factory
        self._allow_real = allow_real or (lambda: False)
        self._gate = gate
        self._delegate: PixivPyDiscoveryAdapter | None = None

    def _get_delegate(self) -> PixivPyDiscoveryAdapter:
        if self._delegate is not None:
            return self._delegate
        if not self._allow_real():
            raise TemporaryFailure("real_discovery_not_approved")
        # 先检查本代会话/冷却，再读取凭据：暂停或已断开时连 token 都不读。
        self._guard()
        try:
            token = self._store.get()
        except CredentialUnavailable as exc:
            raise TemporaryFailure("credential_store_unavailable") from exc
        except Exception as exc:
            raise TemporaryFailure("credential_store_unavailable") from exc
        if not token:
            raise AuthFailed("pixiv credential missing")
        self._before_call()
        try:
            api = self._api_factory(token)
        except PixivAdapterError:
            raise
        except Exception as exc:
            raise classify_pixiv_exception(exc) from exc
        finally:
            self._after_call()
        self._delegate = PixivPyDiscoveryAdapter(api)
        return self._delegate

    def fetch_page(
        self, query: DiscoveryQuery, cursor: str | None
    ) -> DiscoveryPage:
        delegate = self._get_delegate()
        return self._call(delegate.fetch_page, query, cursor)

    def _guard(self) -> None:
        if self._gate is None:
            return
        try:
            self._gate.check_ready()
        except Exception as exc:
            raise to_pixiv_error(exc) from exc

    def _call(self, fn, *args, **kwargs):
        """认证与每一页搜索分别经过门；多页搜索也按完成时刻保持间隔。"""
        self._before_call()
        try:
            return fn(*args, **kwargs)
        finally:
            self._after_call()

    def _before_call(self) -> None:
        if self._gate is None:
            return
        try:
            self._gate.before_call()
        except Exception as exc:
            raise to_pixiv_error(exc) from exc

    def _after_call(self) -> None:
        if self._gate is None:
            return
        self._gate.after_call()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(authenticated={self._delegate is not None})"
