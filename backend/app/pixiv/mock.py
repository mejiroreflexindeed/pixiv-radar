"""离线 Mock 适配器与 fixture 加载，供测试和骨架阶段使用，不联网。"""

from __future__ import annotations

import json
from pathlib import Path

from .adapter import NovelSummary, NovelTag, NovelText


DEFAULT_FIXTURES: list[NovelSummary] = [
    NovelSummary(
        novel_id=1001,
        title="メジロアルダンの朝",
        caption="朝練前の静かな時間。",
        author_id=10001,
        author_name="あるだん好き",
        series_id=5001,
        series_name="アルダン短編集",
        tags=(NovelTag("ウマ娘"), NovelTag("メジロアルダン")),
        text_length=12345,
        total_bookmarks=234,
        total_view=5678,
        total_comments=12,
        is_original=True,
        create_date="2026-08-01T00:00:00+00:00",
    ),
    NovelSummary(
        novel_id=1002,
        title="雨上がりのターフ",
        caption="雨の日のレースの後。",
        author_id=10002,
        author_name="ターフの住人",
        series_id=None,
        series_name=None,
        tags=(NovelTag("ウマ娘"), NovelTag("トレーナー")),
        text_length=8765,
        total_bookmarks=101,
        total_view=3210,
        total_comments=5,
        is_original=True,
        create_date="2026-08-02T00:00:00+00:00",
    ),
]


def load_fixture_novels(path: str | Path) -> list[NovelSummary]:
    """从 JSON fixture 文件加载小说摘要。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    novels: list[NovelSummary] = []
    for item in data:
        novels.append(
            NovelSummary(
                novel_id=int(item["novel_id"]),
                title=str(item["title"]),
                caption=str(item.get("caption", "")),
                author_id=int(item.get("author_id", 0)),
                author_name=str(item.get("author_name", "")),
                series_id=item.get("series_id"),
                series_name=item.get("series_name"),
                tags=tuple(
                    NovelTag(t.get("name"), t.get("translated_name"))
                    for t in item.get("tags", [])
                ),
                text_length=int(item.get("text_length", 0)),
                total_bookmarks=int(item.get("total_bookmarks", 0)),
                total_view=int(item.get("total_view", 0)),
                total_comments=int(item.get("total_comments", 0)),
                is_original=bool(item.get("is_original", True)),
                create_date=item.get("create_date"),
                x_restrict=int(item.get("x_restrict", 0)),
                visible=bool(item.get("visible", True)),
                novel_ai_type=int(item.get("novel_ai_type", 0)),
                likes=item.get("likes"),
                updated_at=item.get("updated_at"),
            )
        )
    return novels


class MockPixivAdapter:
    """完全离线的适配器实现。"""

    def __init__(
        self,
        novels: list[NovelSummary] | None = None,
        texts: dict[int, str] | None = None,
    ) -> None:
        self._novels = list(novels) if novels is not None else list(DEFAULT_FIXTURES)
        self._texts = dict(texts) if texts is not None else {}

    def search_novels(
        self,
        word: str,
        *,
        limit: int = 30,
        search_target: str = "partial_match_for_tags",
        sort: str = "date_desc",
    ) -> list[NovelSummary]:
        return list(self._novels[:limit])

    def novel_detail(self, novel_id: int) -> NovelSummary:
        for novel in self._novels:
            if novel.novel_id == novel_id:
                return novel
        raise KeyError(novel_id)

    def novel_text(self, novel_id: int) -> NovelText:
        if novel_id in self._texts:
            return NovelText(novel_id=novel_id, text=self._texts[novel_id])
        raise KeyError(novel_id)
