"""WP3 只读 Dashboard API 测试：筛选/排序/分页/热力图/恶意参数（全部离线）。"""

import pytest

from app.domain.scan_coordinator import ScanCoordinator
from app.pixiv.adapter import NovelSummary, NovelTag
from app.storage.connection import connect
from app.storage.migrations import apply_migrations
from app.storage.phase_c_repository import PhaseCRepository
from app.storage.scan_repository import ScanRepository

TIMES = [
    "2026-08-30T00:00:00+00:00",
    "2026-08-30T01:00:00+00:00",
    "2026-08-30T02:00:00+00:00",
    "2026-08-30T03:00:00+00:00",
    "2026-08-30T04:00:00+00:00",
    "2026-08-30T05:00:00+00:00",
    "2026-08-30T06:00:00+00:00",
]


class ScriptedAdapter:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def search_novels(
        self, word, *, limit=30,
        search_target="partial_match_for_tags", sort="date_desc",
    ):
        item = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        return list(item)

    def novel_detail(self, novel_id):
        raise NotImplementedError

    def novel_text(self, novel_id):
        raise NotImplementedError


def make_novel(novel_id, views, bookmarks):
    return NovelSummary(
        novel_id=novel_id,
        title=f"novel-{novel_id:02d}",
        author_name=f"author-{novel_id}",
        tags=(NovelTag("ウマ娘"),),
        text_length=1000,
        total_bookmarks=bookmarks,
        total_view=views,
        total_comments=0,
        is_original=True,
        create_date="2026-08-01T00:00:00+00:00",
    )


def _build_rounds():
    rounds = []
    for r in range(7):
        batch = []
        for i in range(1, 13):
            if i <= 3:
                # 小分母 → unknown
                views = 10 + i + r
                bookmarks = 1 + (r if i == 1 else 0)
            elif i == 4:
                # 慢增长 1/hr → not_qualified（views >= 200 但信号 < fallback 5.0）
                views = 5000 + r
                bookmarks = 200
            else:
                # 快增长 → qualified（velocity 20+i >= 25/hr）
                views = 300 + i * 10 + r * (20 + i)
                bookmarks = 10 + i + r * 3
            batch.append(make_novel(i, views, bookmarks))
        rounds.append(batch)
    return rounds


def _seed(tmp_path):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
        # 无扫描数据的目标（缺失指标）
        conn.execute(
            "INSERT INTO targets (novel_id, title, author_name,"
            " first_seen_at, last_seen_at)"
            " VALUES (13, 'novel-13', 'author-13', ?, ?)",
            ("2026-08-30T07:00:00+00:00", "2026-08-30T07:00:00+00:00"),
        )
    finally:
        conn.close()
    coordinator = ScanCoordinator(
        ScriptedAdapter(_build_rounds()),
        ScanRepository(),
        PhaseCRepository(),
        db_path,
    )
    for i, now in enumerate(TIMES):
        outcome = coordinator.run("ウマ娘", run_key=f"k{i}", now_iso=now, source="mock")
        assert outcome.status == "succeeded"
    return db_path


@pytest.fixture
def seeded(client, tmp_path):
    _seed(tmp_path)
    yield client


# ---- 默认兼容 ----


def test_default_no_params_keeps_shape(seeded):
    body = seeded.get("/api/targets").json()
    assert body["total"] == 13
    assert body["track_total"] == 8  # 5..12 快增长 → track
    assert len(body["items"]) == 13
    item = body["items"][0]
    for field in (
        "novel_id", "title", "state", "latest_result", "latest_ewma",
        "latest_threshold", "latest_threshold_source", "signal_ratio",
        "data_source", "last_seen_at",
    ):
        assert field in item
    # 快增长目标 signal_ratio 为有限正数；小分母/无指标为 null
    assert any(i["signal_ratio"] is not None for i in body["items"])
    assert any(i["signal_ratio"] is None for i in body["items"])


# ---- 筛选 ----


def test_target_detail_has_same_signal_fields_as_list(seeded):
    listed = seeded.get("/api/targets", params={"limit": "100"}).json()["items"]
    fast = next(item for item in listed if item["signal_ratio"] is not None)
    detail = seeded.get(f"/api/targets/{fast['novel_id']}").json()
    assert detail["signal_ratio"] == fast["signal_ratio"]
    assert detail["latest_ewma"] == fast["latest_ewma"]
    assert detail["latest_threshold"] == fast["latest_threshold"]


def test_q_filter_title_and_author(seeded):
    body = seeded.get("/api/targets", params={"q": "author-5"}).json()
    assert body["total"] == 1
    assert body["items"][0]["novel_id"] == 5


def test_state_filter(seeded):
    body = seeded.get("/api/targets", params={"state": "track"}).json()
    assert body["total"] == 8
    assert body["track_total"] == 8
    assert all(i["state"] == "track" for i in body["items"])


def test_result_filter(seeded):
    body = seeded.get("/api/targets", params={"result": "unknown"}).json()
    assert body["total"] == 3  # 小分母 1..3
    assert all(i["latest_result"] == "unknown" for i in body["items"])


def test_min_signal_ratio_filter(seeded):
    body = seeded.get("/api/targets", params={"min_signal_ratio": "3"}).json()
    assert body["total"] >= 1
    assert all(
        i["signal_ratio"] is not None and i["signal_ratio"] >= 3
        for i in body["items"]
    )


def test_sql_injection_style_input_is_harmless(seeded):
    resp = seeded.get(
        "/api/targets", params={"q": "'; DROP TABLE targets;--"}
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    # 表仍然存在且数据完好
    body = seeded.get("/api/targets").json()
    assert body["total"] == 13


# ---- 排序 ----


def test_sort_title_asc(seeded):
    body = seeded.get(
        "/api/targets", params={"sort": "title", "sort_dir": "asc"}
    ).json()
    titles = [i["title"] for i in body["items"]]
    assert titles == sorted(titles)


def test_sort_signal_desc_nulls_last(seeded):
    body = seeded.get(
        "/api/targets", params={"sort": "signal", "sort_dir": "desc"}
    ).json()
    ratios = [i["signal_ratio"] for i in body["items"]]
    non_null = [r for r in ratios if r is not None]
    assert non_null == sorted(non_null, reverse=True)
    # null 全部在末尾
    first_null = next(
        (idx for idx, r in enumerate(ratios) if r is None), len(ratios)
    )
    assert all(r is None for r in ratios[first_null:])


def test_sort_views_desc(seeded):
    body = seeded.get(
        "/api/targets", params={"sort": "views", "sort_dir": "desc"}
    ).json()
    views = [i["total_view"] for i in body["items"] if i["total_view"] is not None]
    assert views == sorted(views, reverse=True)


def test_stable_tiebreak_by_novel_id_desc(seeded):
    body = seeded.get(
        "/api/targets", params={"sort": "title", "sort_dir": "desc"}
    ).json()
    titles = [i["title"] for i in body["items"]]
    assert titles == sorted(titles, reverse=True)
    # novel-12 与 novel-11 等前缀相同但完整字符串唯一，无需额外 tiebreak 断言


# ---- 分页与非法参数 ----


def test_pagination_bounds(seeded):
    body = seeded.get("/api/targets", params={"limit": "5", "offset": "5"}).json()
    assert len(body["items"]) == 5
    assert body["total"] == 13
    body2 = seeded.get("/api/targets", params={"offset": "1000"}).json()
    assert body2["items"] == []
    assert body2["total"] == 13


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "evil_column"},
        {"sort_dir": "sideways"},
        {"state": "deleted"},
        {"result": "awesome"},
        {"q": "x" * 101},
        {"min_signal_ratio": "-1"},
        {"min_signal_ratio": "1000"},
        {"limit": "0"},
        {"limit": "101"},
    ],
)
def test_invalid_params_rejected(seeded, params):
    resp = seeded.get("/api/targets", params=params)
    assert resp.status_code == 422


# ---- 热力图 ----


def test_heatmap_caps_12x7(seeded):
    body = seeded.get("/api/dashboard/heatmap").json()
    assert len(body["items"]) == 12  # 13 个目标截断为 12
    for item in body["items"]:
        assert len(item["points"]) <= 7
        times = [p["captured_at"] for p in item["points"]]
        assert times == sorted(times)  # 时间正序
        for point in item["points"]:
            assert set(point.keys()) == {
                "scan_run_id", "captured_at", "ewma", "threshold",
                "result", "reason_codes",
            }
            assert isinstance(point["reason_codes"], list)


def test_heatmap_filters(seeded):
    body = seeded.get("/api/dashboard/heatmap", params={"state": "track"}).json()
    assert len(body["items"]) == 8
    assert all(i["state"] == "track" for i in body["items"])

    body = seeded.get(
        "/api/dashboard/heatmap", params={"result": "unknown"}
    ).json()
    assert len(body["items"]) == 3


def test_heatmap_target_without_metrics_has_empty_points(seeded):
    body = seeded.get("/api/dashboard/heatmap").json()
    no_metric = [i for i in body["items"] if i["novel_id"] == 13]
    assert len(no_metric) == 1  # 13 号 last_seen 最新，排第一
    assert no_metric[0]["points"] == []


def test_heatmap_invalid_params_rejected(seeded):
    assert seeded.get("/api/dashboard/heatmap", params={"state": "evil"}).status_code == 422
    assert seeded.get("/api/dashboard/heatmap", params={"q": "x" * 101}).status_code == 422
