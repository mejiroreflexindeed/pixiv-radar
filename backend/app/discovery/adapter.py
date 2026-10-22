"""PixivDiscoveryAdapter 协议（Search-1）。

独立于现有 search_novels 路径：发现层按"来源类型 + query_key 取一页候选 +
项目自己的不透明 cursor"，不破坏 BroadScan/ScanCoordinator 的既有语义。
业务层看不到 PixivPy 对象、next_url 或任意第三方响应。
"""

from __future__ import annotations

from typing import Protocol

from .types import DiscoveryPage, DiscoveryQuery


class PixivDiscoveryAdapter(Protocol):
    """八类来源的单页候选接口。"""

    def fetch_page(
        self, query: DiscoveryQuery, cursor: str | None
    ) -> DiscoveryPage: ...
