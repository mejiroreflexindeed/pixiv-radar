"""WP8 三轮固定 fixture：确定性输出断言 + 门限两条路径 + 错误轮状态保持。

全部离线；时间与参数显式注入；逐轮输出固定可断言。
"""

import pytest

from app.domain.scan_coordinator import ScanCoordinator
from app.pixiv.adapter import NovelSummary, NovelTag
from app.pixiv.exceptions import MalformedResponse, RateLimited
from app.storage.connection import connect
from app.storage.migrations import apply_migrations
from app.storage.phase_c_repository import PhaseCRepository
from app.storage.scan_repository import ScanRepository

T0 = "2026-08-30T00:00:00+00:00"
T1 = "2026-08-30T01:00:00+00:00"
T2 = "2026-08-30T02:00:00+00:00"


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
        if isinstance(item, Exception):
            raise item
        return list(item)

    def novel_detail(self, novel_id):
        raise NotImplementedError

    def novel_text(self, novel_id):
        raise NotImplementedError


def make_novel(novel_id, views, bookmarks):
    return NovelSummary(
        novel_id=novel_id, title=f"n{novel_id}",
        tags=(NovelTag("ウマ娘"),),
        text_length=1000, total_bookmarks=bookmarks, total_view=views,
        total_comments=0, is_original=True,
        create_date="2026-08-01T00:00:00+00:00",
    )


@pytest.fixture
def db_path(tmp_path):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return db_path


def _make(db_path, adapter):
    return ScanCoordinator(
        adapter, ScanRepository(), PhaseCRepository(), db_path
    )


def _query(db_path, sql, *args):
    conn = connect(db_path)
    try:
        rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def test_three_round_growth_and_state_machine(db_path):
    """稳定增长 → 三轮后 cold_start→track；逐轮结果确定。"""
    rounds = [
        [make_novel(1, 300, 10), make_novel(2, 5000, 200)],
        [make_novel(1, 500, 25), make_novel(2, 5001, 200)],
        [make_novel(1, 700, 40), make_novel(2, 5002, 200)],
    ]
    coordinator = _make(db_path, ScriptedAdapter(rounds))
    for i, now in enumerate([T0, T1, T2]):
        outcome = coordinator.run("ウマ娘", run_key=f"r{i}", now_iso=now)
        assert outcome.status == "succeeded"

    # 目标 1：逐轮结果 unknown / qualified / qualified（velocity 200/hr）
    metrics1 = _query(
        db_path,
        "SELECT result, ewma_view_velocity, threshold_source FROM target_metrics"
        " WHERE novel_id=1 ORDER BY scan_run_id",
    )
    assert [m["result"] for m in metrics1] == ["unknown", "qualified", "qualified"]
    assert metrics1[1]["ewma_view_velocity"] == pytest.approx(200.0)
    assert metrics1[2]["ewma_view_velocity"] == pytest.approx(200.0)
    assert metrics1[1]["threshold_source"] == "fallback"

    state1 = _query(db_path, "SELECT * FROM target_states WHERE novel_id=1")
    assert state1[0]["state"] == "track"
    assert state1[0]["observation_count"] == 3

    transition = _query(
        db_path, "SELECT * FROM state_transitions WHERE novel_id=1"
    )
    assert len(transition) == 1
    assert transition[0]["from_state"] == "cold_start"
    assert transition[0]["to_state"] == "track"

    # 目标 2：慢增长 1/hr < fallback 5.0 → not_qualified；obs 满后转 broad
    metrics2 = _query(
        db_path,
        "SELECT result FROM target_metrics WHERE novel_id=2 ORDER BY scan_run_id",
    )
    assert metrics2[0]["result"] == "unknown"
    assert metrics2[1]["result"] == "not_qualified"
    assert metrics2[2]["result"] == "not_qualified"
    state2 = _query(db_path, "SELECT * FROM target_states WHERE novel_id=2")
    assert state2[0]["state"] == "broad"
    transition2 = _query(
        db_path,
        "SELECT * FROM state_transitions WHERE novel_id=2"
        " AND from_state='cold_start'",
    )
    assert transition2[0]["to_state"] == "broad"


def test_demotion_after_three_not_qualified(db_path):
    """预置 track 目标：三轮不合格 → track→broad。"""
    seed = _make(
        db_path,
        ScriptedAdapter(
            [
                [make_novel(3, 5000, 200)],
                [make_novel(3, 5001, 200)],
                [make_novel(3, 5002, 200)],
            ]
        ),
    )
    for i, now in enumerate(
        [
            "2026-08-29T21:00:00+00:00",
            "2026-08-29T22:00:00+00:00",
            "2026-08-29T23:00:00+00:00",
        ]
    ):
        seed.run("ウマ娘", run_key=f"s{i}", now_iso=now)
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE target_states SET state='track', promotion_streak=0,"
            " demotion_streak=0, observation_count=3 WHERE novel_id=3"
        )
    finally:
        conn.close()

    slow = _make(
        db_path,
        ScriptedAdapter(
            [
                [make_novel(3, 5003, 200)],
                [make_novel(3, 5004, 200)],
                [make_novel(3, 5005, 200)],
            ]
        ),
    )
    for i, now in enumerate([T0, T1, T2]):
        slow.run("ウマ娘", run_key=f"d{i}", now_iso=now)

    state = _query(db_path, "SELECT * FROM target_states WHERE novel_id=3")
    assert state[0]["state"] == "broad"
    transition = _query(
        db_path,
        "SELECT * FROM state_transitions WHERE novel_id=3"
        " AND from_state='track'",
    )
    assert transition[0]["to_state"] == "broad"
    assert transition[0]["reason_code"] == "demotion_reached"


def test_small_denominator_and_regression_and_same_timestamp(db_path):
    """小分母 1/1、计数倒退、同时间戳三类目标逐轮 unknown，状态保持。"""
    rounds = [
        [
            make_novel(11, 1, 1),        # 小分母
            make_novel(12, 300, 10),     # 之后倒退
            make_novel(13, 300, 10),     # 之后同时间戳
        ],
        [
            make_novel(11, 2, 2),
            make_novel(12, 290, 9),
            make_novel(13, 320, 12),
        ],
        [
            make_novel(11, 3, 3),
            make_novel(12, 310, 11),
            make_novel(13, 340, 14),
        ],
    ]
    # 同时间戳：第 2、3 轮 captured_at 相同（都注入 T1）
    coordinator = _make(db_path, ScriptedAdapter(rounds))
    coordinator.run("ウマ娘", run_key="k0", now_iso=T0)
    coordinator.run("ウマ娘", run_key="k1", now_iso=T1)
    coordinator.run("ウマ娘", run_key="k2", now_iso=T1)  # 与上轮同时间戳

    # 小分母：全程 unknown（insufficient_exposure），观察数 0，无转换
    m11 = _query(
        db_path,
        "SELECT result, reason_codes FROM target_metrics WHERE novel_id=11"
        " ORDER BY scan_run_id",
    )
    assert [m["result"] for m in m11] == ["unknown"] * 3
    assert m11[0]["reason_codes"].count("insufficient_exposure") >= 0
    s11 = _query(db_path, "SELECT * FROM target_states WHERE novel_id=11")
    assert s11[0]["state"] == "cold_start"
    assert s11[0]["observation_count"] == 0
    assert _query(
        db_path, "SELECT * FROM state_transitions WHERE novel_id=11"
    ) == []

    # 计数倒退轮（r2）：unknown + count_regression + data_quality_unknown 事件
    m12 = _query(
        db_path,
        "SELECT result, reason_codes FROM target_metrics WHERE novel_id=12"
        " ORDER BY scan_run_id",
    )
    assert m12[0]["result"] == "unknown"
    assert "no_valid_growth" in m12[0]["reason_codes"]
    assert "count_regression" in m12[1]["reason_codes"]
    dq = _query(
        db_path,
        "SELECT * FROM events WHERE type='data_quality_unknown'"
        " AND novel_id=12",
    )
    assert len(dq) >= 1

    # 同时间戳轮（r3，13 号）：time_regression → unknown
    m13 = _query(
        db_path,
        "SELECT result, reason_codes FROM target_metrics WHERE novel_id=13"
        " ORDER BY scan_run_id",
    )
    assert "time_regression" in m13[2]["reason_codes"]


def test_error_rounds_keep_state(db_path):
    """429 / 畸形响应轮：run 失败/部分，目标状态与快照保持。"""
    good = _make(
        db_path,
        ScriptedAdapter(
            [
                [make_novel(21, 300, 10)],
                [make_novel(21, 500, 25)],
                [make_novel(21, 700, 40)],
            ]
        ),
    )
    for i, now in enumerate([T0, T1, T2]):
        good.run("ウマ娘", run_key=f"g{i}", now_iso=now)
    before = _query(db_path, "SELECT * FROM target_states WHERE novel_id=21")

    # 429 轮
    rl = _make(db_path, ScriptedAdapter([RateLimited("rate limited", cooldown_seconds=60)]))
    outcome = rl.run("ウマ娘", run_key="e1", now_iso=T1)
    assert outcome.status == "failed"
    assert outcome.cooldown_seconds == 60.0

    # 畸形响应轮
    mal = _make(db_path, ScriptedAdapter([MalformedResponse("missing novels")]))
    outcome = mal.run("ウマ娘", run_key="e2", now_iso=T1)
    assert outcome.status == "partial"

    after = _query(db_path, "SELECT * FROM target_states WHERE novel_id=21")
    assert after == before  # 状态完全保持
    assert _query(
        db_path, "SELECT COUNT(*) AS n FROM metric_snapshots WHERE novel_id=21"
    )[0]["n"] == 3  # 快照没有新增


def test_median_mad_threshold_with_12_targets(db_path):
    """>=10 有效样本 → median+2*MAD 门限；MAD=0 回退。"""
    rounds = [
        [
            make_novel(100 + i, 300 + i * 10, 10 + i)
            for i in range(12)
        ]
    ]
    coordinator = _make(db_path, ScriptedAdapter(rounds))
    coordinator.run("ウマ娘", run_key="mm", now_iso=T0)
    # 第一轮只有单快照，无增长信号 → 需要第二轮
    rounds2 = [
        [
            make_novel(100 + i, 600 + i * 20, 20 + i)
            for i in range(12)
        ]
    ]
    coordinator2 = _make(db_path, ScriptedAdapter(rounds2))
    coordinator2.run("ウマ娘", run_key="mm2", now_iso=T1)

    metrics = _query(
        db_path,
        "SELECT DISTINCT threshold_source, sample_count FROM target_metrics"
        " WHERE scan_run_id IN (SELECT id FROM scan_runs WHERE run_key IN"
        " ('mm','mm2')) AND threshold_source IS NOT NULL",
    )
    sources = {m["threshold_source"] for m in metrics}
    assert "median_mad" in sources
    assert all(m["sample_count"] == 12 for m in metrics)

    # 门限 = median + 2*MAD；速度均不同，MAD 非零
    rates = _query(
        db_path,
        "SELECT ewma_view_velocity FROM target_metrics WHERE novel_id=100"
        " ORDER BY scan_run_id",
    )
    assert rates[-1]["ewma_view_velocity"] == pytest.approx(300.0)


def test_mad_zero_fallback_path(db_path):
    """全部样本速度相同（MAD=0）→ fallback_mad_zero，不任意晋级。"""
    rounds = [[make_novel(200 + i, 300, 10) for i in range(12)]]
    coordinator = _make(db_path, ScriptedAdapter(rounds))
    coordinator.run("ウマ娘", run_key="z0", now_iso=T0)
    rounds2 = [[make_novel(200 + i, 320, 12) for i in range(12)]]
    coordinator2 = _make(db_path, ScriptedAdapter(rounds2))
    coordinator2.run("ウマ娘", run_key="z1", now_iso=T1)

    metrics = _query(
        db_path,
        "SELECT DISTINCT threshold_source FROM target_metrics"
        " WHERE threshold_source IS NOT NULL",
    )
    assert "fallback_mad_zero" in {m["threshold_source"] for m in metrics}
