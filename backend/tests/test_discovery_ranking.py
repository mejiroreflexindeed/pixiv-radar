"""可解释三段式排序测试（WP2 必测场景）。"""

from app.discovery.ranking import GROUP_LABELS, rank_candidates


def base(
    novel_id=1,
    *,
    title="t",
    bookmarks=10,
    views=300,
    visible=True,
    snapshots=None,
    source_kinds=("exact_tag",),
    feedback=None,
):
    return {
        "novel_id": novel_id,
        "title": title,
        "author_name": "a",
        "bookmarks": bookmarks,
        "views": views,
        "visible": visible,
        "snapshots": list(snapshots or []),
        "source_kinds": list(source_kinds),
        "feedback": feedback,
    }


def snap(views, bookmarks, captured_at, run=0):
    return {
        "views": views,
        "bookmarks": bookmarks,
        "captured_at": captured_at,
        "scan_run_id": run,
    }


def growing_snapshots():
    return [
        snap(50, 6, "2026-09-01T00:00:00+00:00", 1),
        snap(100, 8, "2026-09-01T01:00:00+00:00", 2),
        snap(150, 10, "2026-09-01T02:00:00+00:00", 3),
    ]


def test_small_gem_vs_popular_reference_explainable():
    small = base(
        7001, title="小众作", bookmarks=10, views=150,
        snapshots=growing_snapshots(), source_kinds=("exact_tag", "novel_new"),
    )
    popular = base(
        7002, title="热门作", bookmarks=1000, views=10000,
        snapshots=[
            snap(10000, 1000, "2026-09-01T00:00:00+00:00", 1),
            snap(10000, 1000, "2026-09-01T01:00:00+00:00", 2),
            snap(10000, 1000, "2026-09-01T02:00:00+00:00", 3),
        ],
    )
    ranked = rank_candidates([popular, small])
    by_id = {r.novel_id: r for r in ranked}
    assert by_id[7001].group == "small_gem"
    assert by_id[7002].group == "popular_reference"
    # 分组可解释
    assert "小众新发现" in by_id[7001].group_reason
    assert "热度不是质量" in by_id[7002].group_reason
    # 小众作排序在热门参考之前（分组顺序）
    assert ranked[0].novel_id == 7001


def test_single_snapshot_goes_observe():
    candidate = base(
        7003, snapshots=[snap(10, 1, "2026-09-01T00:00:00+00:00", 1)]
    )
    ranked = rank_candidates([candidate])
    assert ranked[0].group == "observe"
    assert "1/3" in ranked[0].group_reason


def test_three_snapshots_compare_after_observation():
    one = base(7001, snapshots=[snap(10, 1, "2026-09-01T00:00:00+00:00")])
    three = base(
        7002,
        bookmarks=20,
        snapshots=[
            snap(50, 10, "2026-09-01T00:00:00+00:00", 1),
            snap(120, 15, "2026-09-01T01:00:00+00:00", 2),
            snap(200, 20, "2026-09-01T02:00:00+00:00", 3),
        ],
    )
    ranked = rank_candidates([three, one])
    by_id = {r.novel_id: r for r in ranked}
    assert by_id[7001].group == "observe"
    assert by_id[7002].group in ("small_gem", "stable_track")


def test_feedback_changes_explanation_and_order():
    good = base(
        7001, bookmarks=20, snapshots=growing_snapshots(), feedback="good"
    )
    exclude = base(
        7002, bookmarks=20, snapshots=growing_snapshots(), feedback="exclude"
    )
    none = base(
        7003, bookmarks=20, snapshots=growing_snapshots(), feedback=None
    )
    ranked = rank_candidates([none, exclude, good])
    by_id = {r.novel_id: r for r in ranked}
    assert any("好文" in e for e in by_id[7001].explanations)
    assert any("排除" in e for e in by_id[7002].explanations)
    # good 排最前、exclude 排最后（同组内反馈修正）
    assert ranked[0].novel_id == 7001
    assert ranked[-1].novel_id == 7002


def test_input_order_independence():
    a = base(7001, bookmarks=20, snapshots=growing_snapshots())
    b = base(7002, bookmarks=5, snapshots=growing_snapshots())
    first = rank_candidates([a, b])
    second = rank_candidates([b, a])
    assert first == second
    assert first[0].novel_id == 7001


def test_hard_filters_with_reasons():
    cases = [
        (base(0), "invalid_id"),
        (base(1, title="   "), "empty_title"),
        (base(1, visible=False), "invisible"),
        (base(1, bookmarks=-1), "invalid_counts"),
    ]
    for candidate, expected in cases:
        ranked = rank_candidates([candidate])
        assert ranked[0].group == "filtered"
        assert ranked[0].filtered_reason == expected


def test_group_labels_complete():
    for key in ("small_gem", "stable_track", "popular_reference", "observe", "filtered"):
        assert key in GROUP_LABELS
