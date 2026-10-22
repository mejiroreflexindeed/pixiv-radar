"""去重与元数据合并测试（契约 §8：纯函数、保留来源、顺序无关）。"""

from app.discovery.merge import merge_batches
from app.discovery.types import DiscoveryBatch, DiscoveryPage, DiscoveryQuery
from app.pixiv.adapter import NovelSummary, NovelTag


def make_novel(novel_id, **overrides):
    data = dict(
        novel_id=novel_id,
        title=f"n{novel_id}",
        caption="",
        author_id=0,
        author_name="",
        series_id=None,
        series_name=None,
        text_length=100,
        total_bookmarks=1,
        total_view=100,
        total_comments=0,
        is_original=True,
        create_date=None,
        x_restrict=0,
        visible=True,
        novel_ai_type=0,
        tags=(),
    )
    data.update(overrides)
    return NovelSummary(**data)


def q(key, kind, text=None):
    return DiscoveryQuery(query_key=key, source_kind=kind, text=text)


def test_three_sources_one_candidate_keeps_all_sightings():
    batches = [
        DiscoveryBatch(
            q("exact_tag:t", "exact_tag", "t"),
            DiscoveryPage((make_novel(1, title="t1"),)),
        ),
        DiscoveryBatch(
            q("novel_new:all", "novel_new"),
            DiscoveryPage((make_novel(1, title="t2"),)),
        ),
        DiscoveryBatch(
            q("author_novels:9", "author_novels"),
            DiscoveryPage((make_novel(1, title="t3"),)),
        ),
    ]
    result = merge_batches(batches, discovered_at="2026-09-01T00:00:00+00:00")
    assert len(result) == 1
    candidate = result[0]
    assert candidate.novel.novel_id == 1
    assert len(candidate.sightings) == 3
    kinds = {s.source_kind for s in candidate.sightings}
    assert kinds == {"exact_tag", "novel_new", "author_novels"}
    # 文本按来源优先级取首个非空：exact_tag 优先
    assert candidate.novel.title == "t1"


def test_same_source_query_duplicate_keeps_best_rank():
    batches = [
        DiscoveryBatch(
            q("exact_tag:t", "exact_tag", "t"),
            DiscoveryPage(
                (make_novel(1), make_novel(2), make_novel(1))  # novel 1 出现两次
            ),
        ),
    ]
    result = merge_batches(batches, discovered_at="2026-09-01T00:00:00+00:00")
    sightings = result[0].sightings
    assert len(sightings) == 1
    assert sightings[0].source_rank == 1  # 最靠前 rank


def test_input_order_independence():
    base = [
        DiscoveryBatch(
            q("exact_tag:t", "exact_tag", "t"),
            DiscoveryPage((make_novel(1, title="a", total_bookmarks=5),)),
        ),
        DiscoveryBatch(
            q("keyword:k", "keyword", "k"),
            DiscoveryPage((make_novel(1, title="b", total_bookmarks=9),)),
        ),
    ]
    a = merge_batches(list(base), discovered_at="2026-09-01T00:00:00+00:00")
    b = merge_batches(list(reversed(base)), discovered_at="2026-09-01T00:00:00+00:00")
    assert a == b
    # 计数取最大值；标题按优先级（exact_tag > keyword）
    assert a[0].novel.total_bookmarks == 9
    assert a[0].novel.title == "a"


def test_tags_stable_union_sorted():
    n1 = make_novel(1, tags=(NovelTag("b"), NovelTag("a")))
    n2 = make_novel(1, tags=(NovelTag("c"), NovelTag("a")))
    result = merge_batches(
        [
            DiscoveryBatch(q("exact_tag:t", "exact_tag", "t"), DiscoveryPage((n1,))),
            DiscoveryBatch(q("keyword:k", "keyword", "k"), DiscoveryPage((n2,))),
        ],
        discovered_at="2026-09-01T00:00:00+00:00",
    )
    names = [t.name for t in result[0].novel.tags]
    assert names == ["a", "b", "c"]  # 排序保证与输入顺序无关


def test_visibility_not_relaxed():
    n1 = make_novel(1, visible=True)
    n2 = make_novel(1, visible=False)
    result = merge_batches(
        [
            DiscoveryBatch(q("exact_tag:t", "exact_tag", "t"), DiscoveryPage((n1,))),
            DiscoveryBatch(q("keyword:k", "keyword", "k"), DiscoveryPage((n2,))),
        ],
        discovered_at="2026-09-01T00:00:00+00:00",
    )
    assert result[0].novel.visible is False


def test_negative_counts_ignored():
    n1 = make_novel(1, total_bookmarks=10)
    n2 = make_novel(1, total_bookmarks=-5)
    result = merge_batches(
        [
            DiscoveryBatch(q("exact_tag:t", "exact_tag", "t"), DiscoveryPage((n1,))),
            DiscoveryBatch(q("keyword:k", "keyword", "k"), DiscoveryPage((n2,))),
        ],
        discovered_at="2026-09-01T00:00:00+00:00",
    )
    assert result[0].novel.total_bookmarks == 10


def test_false_zero_and_entity_pairs_are_preserved_deterministically():
    high = make_novel(
        1,
        is_original=False,
        novel_ai_type=0,
        author_id=42,
        author_name="",
        series_id=7,
        series_name="",
        tags=(NovelTag("tag", "高优先翻译"),),
    )
    low = make_novel(
        1,
        is_original=True,
        novel_ai_type=2,
        author_id=42,
        author_name="author-42",
        series_id=7,
        series_name="series-7",
        tags=(NovelTag("tag", "低优先翻译"),),
    )
    batches = [
        DiscoveryBatch(q("exact_tag:x", "exact_tag", "x"), DiscoveryPage((high,))),
        DiscoveryBatch(q("keyword:y", "keyword", "y"), DiscoveryPage((low,))),
    ]
    a = merge_batches(batches, discovered_at="2026-09-01T00:00:00+00:00")
    b = merge_batches(list(reversed(batches)), discovered_at="2026-09-01T00:00:00+00:00")
    assert a == b
    novel = a[0].novel
    assert novel.is_original is False
    assert novel.novel_ai_type == 0
    assert (novel.author_id, novel.author_name) == (42, "author-42")
    assert (novel.series_id, novel.series_name) == (7, "series-7")
    assert novel.tags == (NovelTag("tag", "高优先翻译"),)


def test_same_source_conflicts_use_query_key_and_tags_translation_tie_break():
    left = make_novel(1, title="z-title", tags=(NovelTag("t", "z"),))
    right = make_novel(1, title="a-title", tags=(NovelTag("t", "a"),))
    batches = [
        DiscoveryBatch(q("exact_tag:z", "exact_tag", "z"), DiscoveryPage((left,))),
        DiscoveryBatch(q("exact_tag:a", "exact_tag", "a"), DiscoveryPage((right,))),
    ]
    a = merge_batches(batches, discovered_at="2026-09-01T00:00:00+00:00")
    b = merge_batches(list(reversed(batches)), discovered_at="2026-09-01T00:00:00+00:00")
    assert a == b
    assert a[0].novel.title == "a-title"
    assert a[0].novel.tags == (NovelTag("t", "a"),)
