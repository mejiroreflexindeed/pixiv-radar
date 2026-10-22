"""Pixiv 采集适配器协议与领域类型。

业务代码只依赖这里的类型化结果，不直接依赖 PixivPy 的响应对象，
便于替换采集实现与离线测试。契约与异常语义见 docs/PHASE_B_CONTRACT.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class NovelTag:
    name: str
    translated_name: str | None = None


@dataclass(frozen=True)
class NovelSummary:
    """小说搜索/详情元数据（已核实 Pixiv 小说模型可依赖的字段）。"""

    novel_id: int
    title: str
    tags: tuple[NovelTag, ...] = ()
    text_length: int = 0
    total_bookmarks: int = 0
    total_view: int = 0
    total_comments: int = 0
    is_original: bool = True
    create_date: str | None = None
    caption: str = ""
    author_id: int = 0
    author_name: str = ""
    series_id: int | None = None
    series_name: str | None = None
    x_restrict: int = 0
    visible: bool = True
    novel_ai_type: int = 0

    # Pixiv 小说模型没有可靠的 likes / updated_at，保留可空且不进入业务判断
    likes: int | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class NovelText:
    novel_id: int
    text: str


class PixivAdapter(Protocol):
    """采集适配器协议。真实实现（PixivPy）见 pixivpy_adapter.py。

    错误语义：
    - AuthFailed: 401/403、凭据无效；
    - RateLimited: 429，携带 cooldown_seconds；
    - TemporaryFailure: 网络错误 / 5xx；
    - MalformedResponse: 响应缺关键字段或结构异常；
    - NotFound: 明确的 404 / 不存在语义。

    调用约定：搜索默认单并发；真实冒烟上限 1 页、5 个结果、不请求正文。
    """

    def search_novels(
        self,
        word: str,
        *,
        limit: int = 30,
        search_target: str = "partial_match_for_tags",
        sort: str = "date_desc",
    ) -> list[NovelSummary]: ...

    def novel_detail(self, novel_id: int) -> NovelSummary: ...

    def novel_text(self, novel_id: int) -> NovelText: ...
