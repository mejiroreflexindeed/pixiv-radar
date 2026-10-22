"""离线 FakeDiscoveryAdapter：按 query_key 返回预置页面或异常，记录调用。"""

from __future__ import annotations

from ..pixiv.adapter import NovelSummary
from .adapter import PixivDiscoveryAdapter
from .types import DiscoveryPage, DiscoveryQuery
from . import PER_REQUEST_LIMIT


class FakeDiscoveryAdapter:
    """测试/演示用：完全离线，不联网、不访问 Pixiv。"""

    def __init__(
        self,
        pages: dict[str, DiscoveryPage | list[NovelSummary] | Exception | None] | None = None,
    ) -> None:
        self._pages: dict[str, DiscoveryPage | list[NovelSummary] | Exception | None] = dict(
            pages or {}
        )
        self.calls: list[tuple[str, str | None]] = []

    def fetch_page(
        self, query: DiscoveryQuery, cursor: str | None
    ) -> DiscoveryPage:
        self.calls.append((query.query_key, cursor))
        value = self._pages.get(query.query_key)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return DiscoveryPage(())
        if isinstance(value, list):
            return DiscoveryPage(tuple(value[: query.limit]))
        # The fake intentionally enforces the same boundary for list and page
        # fixtures; DiscoveryService still defends against a lying adapter.
        return DiscoveryPage(tuple(value.items[: min(query.limit, PER_REQUEST_LIMIT)]), value.next_cursor)
