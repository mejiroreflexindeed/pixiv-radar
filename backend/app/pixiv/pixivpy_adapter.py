"""PixivPy 适配器实现：把 pixivpy3 响应映射为项目 dataclass，统一异常。

业务层只接收 NovelSummary / NovelText / NovelTag，不暴露 PixivPy 对象。
错误分类语义见 docs/PHASE_B_CONTRACT.md。本模块不主动登录、不重试、不限流
（单并发与冒烟上限由调用方负责）。

PixivPy 3.7.5 真实响应行为（本模块按此处理）：
- auth() 失败抛 PixivError，reason 内嵌 "HTTP {status}: {body}"，无 status_code 属性；
- 应用接口不调用 raise_for_status，429 等错误状态返回体是 JSON {"error": {...}}，
  会被 parse_json 正常解析为对象——必须先检查 error 键，否则会把限流误判为结构缺失；
- PixivPy 无内置 429/backoff 逻辑，cooldown 只从 Retry-After / error 体提取。
"""

from __future__ import annotations

import re

from ..core.logging import redact
from ..core.timeutil import normalize_utc_iso
from .adapter import NovelSummary, NovelTag, NovelText
from .exceptions import (
    AuthFailed,
    MalformedResponse,
    NotFound,
    RateLimited,
    TemporaryFailure,
)

_HTTP_STATUS_RE = re.compile(r"HTTP\s+(\d{3})")
_RATE_KEYWORDS = ("rate_limit", "rate limit", "ratelimit", "too many requests", "429")
_AUTH_KEYWORDS = ("forbidden", "unauthorized", "authentication", "invalid_grant", "invalid refresh token")
_NETWORK_KEYWORDS = ("connection", "timeout", "network", "unreachable")


def _get(obj, name: str, default=None):
    """先取属性，再尝试 dict 键；缺失返回 default。"""
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return value if value is not None else default


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _extract_status(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and value:
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and value:
            return value
    # PixivError 无状态码属性；auth() 失败时 reason 内嵌 "HTTP {status}: {body}"，
    # 部分场景 body 里也带状态码文本
    for candidate in (str(exc), str(getattr(exc, "reason", "") or "")):
        match = _HTTP_STATUS_RE.search(candidate)
        if match:
            return int(match.group(1))
    return None


def _extract_retry_after(exc: Exception) -> float | None:
    for container in (getattr(exc, "response", None), getattr(exc, "header", None)):
        if container is None:
            continue
        headers = getattr(container, "headers", None) or container
        value = headers.get("Retry-After") if hasattr(headers, "get") else None
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _cooldown_from_error_body(error) -> float | None:
    """从 app-api 429 错误体提取冷却秒数（形状未线上核实，防御式尝试）。"""
    rate_limit = _get(error, "rate_limit")
    if not rate_limit:
        return None
    for key in ("reset_after", "reset_after_seconds", "seconds", "retry_after"):
        value = _get(rate_limit, key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def classify_pixiv_exception(exc: Exception) -> Exception:
    """把 PixivPy/HTTP 异常转换为不携带响应正文的领域异常。

    认证阶段发生在 ``PixivPyAdapter`` 构造之前，因此真实冒烟入口也复用
    这个函数，避免 auth() 的 429 丢失 Retry-After。
    """
    status = _extract_status(exc)
    text = str(redact(str(exc))).lower()

    if status in (401, 403):
        return AuthFailed(f"pixiv auth failed (HTTP {status})")
    if status == 404:
        return NotFound("novel not found (HTTP 404)")
    if status == 429 or any(keyword in text for keyword in _RATE_KEYWORDS):
        return RateLimited(
            "rate limited (HTTP 429)",
            cooldown_seconds=_extract_retry_after(exc),
        )
    # AppPixivAPI.require_auth() 不附带 HTTP 状态码，只给出
    # "Authentication required"，仍应属于认证失败而非临时网络故障。
    if any(keyword in text for keyword in _AUTH_KEYWORDS):
        return AuthFailed("pixiv authentication required or credentials invalid")
    if status is not None and status >= 500:
        return TemporaryFailure(f"pixiv server error (HTTP {status})")
    if any(keyword in text for keyword in _NETWORK_KEYWORDS):
        return TemporaryFailure("pixiv network failure")

    # 不回显第三方异常正文；其中可能包含请求参数、响应体或凭据。
    return TemporaryFailure("pixiv request failed")


class PixivPyAdapter:
    """基于 pixivpy3.AppPixivAPI 的采集适配器。"""

    def __init__(self, api) -> None:
        self._api = api

    # ---- 调用与错误分类 ----

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            raise self._classify(exc) from exc

    def _classify(self, exc: Exception) -> Exception:
        return classify_pixiv_exception(exc)

    def _raise_for_error_body(self, response) -> None:
        """PixivPy 对 4xx/5xx 不抛异常，错误态以 JSON {"error": {...}} 返回。"""
        error = _get(response, "error")
        if not error:
            return
        reason = str(_get(error, "reason") or "").lower()
        message = str(_get(error, "message") or "").lower()
        user_message = str(_get(error, "user_message") or "").lower()
        combined = f"{reason} {message} {user_message}"

        if any(keyword in combined for keyword in _RATE_KEYWORDS):
            raise RateLimited(
                "rate limited (error body)",
                cooldown_seconds=_cooldown_from_error_body(error),
            )
        if any(keyword in combined for keyword in _AUTH_KEYWORDS):
            raise AuthFailed("pixiv auth failed (error body)")
        # 不把服务端 reason/message 原文带入异常；错误体可能回显请求字段。
        raise TemporaryFailure("pixiv error response")

    # ---- 协议方法 ----

    def search_novels(
        self,
        word: str,
        *,
        limit: int = 30,
        search_target: str = "partial_match_for_tags",
        sort: str = "date_desc",
    ) -> list[NovelSummary]:
        response = self._call(
            self._api.search_novel,
            word=word,
            search_target=search_target,
            sort=sort,
        )
        self._raise_for_error_body(response)
        novels = _get(response, "novels")
        if novels is None:
            raise MalformedResponse("search response missing 'novels'")
        if not novels:
            return []
        return [self._to_summary(item) for item in novels[:limit]]

    def novel_detail(self, novel_id: int) -> NovelSummary:
        response = self._call(self._api.novel_detail, novel_id)
        self._raise_for_error_body(response)
        novel = _get(response, "novel")
        if novel is None:
            raise MalformedResponse("detail response missing 'novel'")
        return self._to_summary(novel)

    def novel_text(self, novel_id: int) -> NovelText:
        response = self._call(self._api.webview_novel, novel_id)
        if response is None:
            raise MalformedResponse("text response is null")
        self._raise_for_error_body(response)
        text = _get(response, "text", _get(response, "novel_text"))
        if text is None:
            raise MalformedResponse("text response missing 'text'")
        return NovelText(novel_id=novel_id, text=str(text))

    # ---- 字段映射 ----

    def _to_summary(self, novel) -> NovelSummary:
        if novel is None:
            raise MalformedResponse("novel entry is null")

        novel_id = _get(novel, "id")
        title = _get(novel, "title")
        if novel_id is None or title is None:
            raise MalformedResponse("novel entry missing id/title")
        try:
            novel_id = int(novel_id)
            title = str(title)
        except (TypeError, ValueError) as exc:
            raise MalformedResponse("novel entry id/title invalid") from exc

        user = _get(novel, "user") or {}
        series = _get(novel, "series")
        series_id = _get(series, "id") if series else None
        series_name = _get(series, "title") if series else None

        tag_items: list[NovelTag] = []
        for tag in _get(novel, "tags") or []:
            name = _get(tag, "name")
            if name is None:
                continue
            translated = _get(tag, "translated_name")
            tag_items.append(
                NovelTag(
                    name=str(name),
                    translated_name=str(translated) if translated else None,
                )
            )

        return NovelSummary(
            novel_id=novel_id,
            title=title,
            caption=str(_get(novel, "caption") or ""),
            author_id=_coerce_int(_get(user, "id"), 0),
            author_name=str(_get(user, "name") or ""),
            series_id=_coerce_int(series_id) if series_id is not None else None,
            series_name=str(series_name) if series_name else None,
            tags=tuple(tag_items),
            text_length=_coerce_int(_get(novel, "text_length"), 0),
            total_bookmarks=_coerce_int(_get(novel, "total_bookmarks"), 0),
            total_view=_coerce_int(_get(novel, "total_view"), 0),
            total_comments=_coerce_int(_get(novel, "total_comments"), 0),
            is_original=bool(_get(novel, "is_original", True)),
            create_date=normalize_utc_iso(_get(novel, "create_date")),
            x_restrict=_coerce_int(_get(novel, "x_restrict"), 0),
            visible=bool(_get(novel, "visible", True)),
            novel_ai_type=_coerce_int(_get(novel, "novel_ai_type"), 0),
        )
