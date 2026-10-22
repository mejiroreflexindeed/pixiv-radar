import json

from app.pixiv.adapter import NovelSummary
from app.pixiv.mock import MockPixivAdapter, load_fixture_novels


def test_mock_search_returns_typed_results():
    adapter = MockPixivAdapter()
    results = adapter.search_novels("ウマ娘", limit=1)
    assert len(results) == 1
    assert isinstance(results[0], NovelSummary)
    assert results[0].novel_id == 1001


def test_mock_detail_and_text():
    adapter = MockPixivAdapter(texts={1001: "本文"})
    assert adapter.novel_detail(1001).title
    assert adapter.novel_text(1001).text == "本文"


def test_load_fixture_novels(tmp_path):
    fixture = tmp_path / "novels.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "novel_id": 1,
                    "title": "t",
                    "tags": [{"name": "ウマ娘"}],
                    "text_length": 100,
                    "total_bookmarks": 2,
                    "total_view": 3,
                    "total_comments": 0,
                }
            ]
        ),
        encoding="utf-8",
    )
    novels = load_fixture_novels(fixture)
    assert novels[0].novel_id == 1
    assert novels[0].tags[0].name == "ウマ娘"
