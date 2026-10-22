"""真实 Pixiv 通用适配器的惰性凭据边界。"""

from __future__ import annotations

from collections.abc import Callable

from ..core.credentials import CredentialStore, CredentialUnavailable
from .adapter import NovelSummary, NovelText
from .exceptions import AuthFailed, PixivAdapterError, TemporaryFailure
from .pixivpy_adapter import PixivPyAdapter, classify_pixiv_exception
from .request_gate import to_pixiv_error


def _default_api_factory(token: str):
    from pixivpy3 import AppPixivAPI

    api = AppPixivAPI(timeout=15)
    api.set_accept_language("zh-cn")
    api.auth(refresh_token=token)
    return api


class CredentialedPixivAdapter:
    """单次操作内惰性认证；构造、repr 和状态查询均不读取 token。"""

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
        self._delegate: PixivPyAdapter | None = None

    def _get_delegate(self) -> PixivPyAdapter:
        if self._delegate is not None:
            return self._delegate
        if not self._allow_real():
            raise TemporaryFailure("real_content_not_approved")
        # 先检查本代会话/冷却，再读取凭据：被暂停或已断开时连 token 都不读。
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
        self._delegate = PixivPyAdapter(api)
        return self._delegate

    def search_novels(self, word: str, **kwargs) -> list[NovelSummary]:
        delegate = self._get_delegate()
        return self._call(delegate.search_novels, word, **kwargs)

    def novel_detail(self, novel_id: int) -> NovelSummary:
        delegate = self._get_delegate()
        return self._call(delegate.novel_detail, novel_id)

    def novel_text(self, novel_id: int) -> NovelText:
        delegate = self._get_delegate()
        return self._call(delegate.novel_text, novel_id)

    def _call(self, fn, *args, **kwargs):
        """每次真实上游调用（认证/搜索/正文）单独经过门并记录完成时刻。"""
        self._before_call()
        try:
            return fn(*args, **kwargs)
        finally:
            self._after_call()

    def _guard(self) -> None:
        """读凭据前的会话/暂停/冷却检查；不记录调用时刻。"""
        if self._gate is None:
            return
        try:
            self._gate.check_ready()
        except Exception as exc:
            raise to_pixiv_error(exc) from exc

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
