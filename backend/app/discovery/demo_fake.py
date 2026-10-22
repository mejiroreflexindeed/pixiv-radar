"""Demo 专用离线 fake 发现适配器。

fixture 覆盖任务书 WP1 要求：小众高增长新作、热门噪声、冷启动、多来源重复、
空页、部分失败（一次后成功，供重试演示）。全部离线，不访问 Pixiv。

novel_id 复用 seed_e_demo 的目标（1~6）：候选因此继承 7 轮快照历史，
排序分组（小众新发现/热门参考/待观察）才真实多样。
"""

from __future__ import annotations

import threading

from ..pixiv.adapter import NovelSummary, NovelTag
from ..pixiv.exceptions import TemporaryFailure
from .adapter import PixivDiscoveryAdapter
from .types import DiscoveryPage, DiscoveryQuery


def demo_novel(novel_id: int, title: str, *, bookmarks: int, views: int, tags=("ウマ娘",)) -> NovelSummary:
    return NovelSummary(
        novel_id=novel_id,
        title=title,
        author_name=f"作者{novel_id}",
        tags=tuple(NovelTag(name) for name in tags),
        text_length=2000,
        total_bookmarks=bookmarks,
        total_view=views,
        total_comments=0,
        is_original=True,
    )


# 固定 fixture：novel_id 语义（与 seed_e_demo 的目标 1~6 对应）
#   1 小众高增长新作（多来源重复：exact_tag + novel_new + partial_tag）
#   2 热门噪声（高收藏、无增长）
#   3 冷启动（极少数据）
#   4 作者扩展
#   5 单一来源新作
#   6 角色标签新作
class DemoFakeDiscoveryAdapter:
    """每次运行返回固定 fixture；`keyword:トレーナー` 首次失败、之后成功。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keyword_attempts = 0
        self.calls: list[tuple[str, str | None]] = []

    def fetch_page(
        self, query: DiscoveryQuery, cursor: str | None
    ) -> DiscoveryPage:
        with self._lock:
            self.calls.append((query.query_key, cursor))
            key = query.query_key

            if key.startswith("exact_tag:ウマ娘"):
                return DiscoveryPage(
                    (demo_novel(1, "メジロアルダンの朝", bookmarks=40, views=700),
                     demo_novel(2, "人気シリーズ完結編", bookmarks=1200, views=50000),)
                )
            if key.startswith("novel_new"):
                return DiscoveryPage(
                    (demo_novel(1, "メジロアルダンの朝", bookmarks=40, views=700),
                     demo_novel(5, "夏合宿の新章", bookmarks=2, views=12),)
                )
            if key.startswith("partial_tag:メジロアルダン"):
                return DiscoveryPage(
                    (demo_novel(1, "メジロアルダンの朝", bookmarks=40, views=700),
                     demo_novel(6, "雨の日のアルダン", bookmarks=3, views=20),)
                )
            if key.startswith("keyword:トレーナー"):
                self._keyword_attempts += 1
                if self._keyword_attempts == 1:
                    raise TemporaryFailure("demo network blip")
                return DiscoveryPage(
                    (demo_novel(3, "トレーナー室の片隅", bookmarks=1, views=13),)
                )
            if key.startswith("novel_recommended"):
                return DiscoveryPage(
                    (demo_novel(2, "人気シリーズ完結編", bookmarks=1200, views=50000),)
                )
            if key.startswith("novel_follow"):
                return DiscoveryPage(())
            if key.startswith("author_novels"):
                return DiscoveryPage(
                    (demo_novel(4, "作者の新連載", bookmarks=8, views=60),)
                )
            if key.startswith("series_novels"):
                return DiscoveryPage(())
            # 空页：部分标签查询没有结果
            return DiscoveryPage(())
