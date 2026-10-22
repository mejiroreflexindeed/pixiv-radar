"""ScanCoordinator 测试（WP4）：租约、run_key 幂等、失败语义、事务回滚、晋级。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.timeutil import add_seconds_iso
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
    """按轮返回固定数据或抛出异常；记录调用次数。"""

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


def make_novel(novel_id, views, bookmarks, comments=0):
    return NovelSummary(
        novel_id=novel_id,
        title=f"n{novel_id}",
        tags=(NovelTag("ウマ娘"),),
        text_length=1000,
        total_bookmarks=bookmarks,
        total_view=views,
        total_comments=comments,
        is_original=True,
        create_date="2026-08-01T00:00:00+00:00",
    )


class FailingPhaseRepo(PhaseCRepository):
    """第 N 次指标写入后抛错，用于整体回滚测试。"""

    def __init__(self, fail_after_metrics: int):
        super().__init__()
        self._count = 0
        self._fail_after = fail_after_metrics

    def insert_metric(self, conn, novel_id, scan_run_id, **kwargs):
        self._count += 1
        if self._count > self._fail_after:
            raise RuntimeError("disk full")
        super().insert_metric(conn, novel_id, scan_run_id, **kwargs)


@pytest.fixture
def db_path(tmp_path):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return db_path


@pytest.fixture
def make(db_path):
    def _make(adapter, phase_repo=None):
        return ScanCoordinator(
            adapter,
            ScanRepository(),
            phase_repo or PhaseCRepository(),
            db_path,
        )

    return _make


def _count(db_path, table: str) -> int:
    conn = connect(db_path)
    try:
        return int(
            conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        )
    finally:
        conn.close()


def test_run_key_idempotent_terminal(db_path, make):
    adapter = ScriptedAdapter([[make_novel(1001, 300, 10)]])
    coordinator = make(adapter)

    first = coordinator.run("ウマ娘", run_key="k1", now_iso=T0)
    assert first.status == "succeeded"
    assert first.results_written == 1

    second = coordinator.run("ウマ娘", run_key="k1", now_iso=T1)
    assert second.run_id == first.run_id
    assert second.status == "succeeded"
    assert adapter.calls == 1  # 终态幂等：不再 fetch

    assert _count(db_path, "scan_runs") == 1
    assert _count(db_path, "metric_snapshots") == 1


def test_promotion_flow_three_rounds(db_path, make):
    rounds = [
        [make_novel(7001, 300, 10)],
        [make_novel(7001, 500, 25)],
        [make_novel(7001, 700, 40)],
    ]
    coordinator = make(ScriptedAdapter(rounds))
    times = [T0, T1, T2]

    for i, now in enumerate(times):
        outcome = coordinator.run("ウマ娘", run_key=f"k{i}", now_iso=now)
        assert outcome.status == "succeeded"

    conn = connect(db_path)
    try:
        state = conn.execute(
            "SELECT * FROM target_states WHERE novel_id=7001"
        ).fetchone()
        assert state["state"] == "track"
        assert state["observation_count"] == 3

        transition = conn.execute(
            "SELECT * FROM state_transitions WHERE novel_id=7001"
        ).fetchone()
        assert transition["from_state"] == "cold_start"
        assert transition["to_state"] == "track"
        assert transition["reason_code"] == "promotion_reached"

        event = conn.execute(
            "SELECT * FROM events WHERE type='target_entered_track'"
        ).fetchone()
        assert event is not None

        # 三轮指标确定：首轮 unknown 无门限信息，后两轮 qualified 走 fallback
        metrics = conn.execute(
            "SELECT result, threshold_source FROM target_metrics"
            " WHERE novel_id=7001 ORDER BY scan_run_id"
        ).fetchall()
        assert [m["result"] for m in metrics] == [
            "unknown", "qualified", "qualified",
        ]
        assert [m["threshold_source"] for m in metrics] == [
            None, "fallback", "fallback",
        ]
    finally:
        conn.close()


def test_demotion_flow_track_to_broad(db_path, make):
    # 种子：三轮慢增长（velocity 1/hr < fallback 5.0）建立历史
    seed_rounds = [
        [make_novel(7002, 5000, 200)],
        [make_novel(7002, 5001, 200)],
        [make_novel(7002, 5002, 200)],
    ]
    coordinator = make(ScriptedAdapter(seed_rounds))
    seed_times = [
        "2026-08-29T21:00:00+00:00",
        "2026-08-29T22:00:00+00:00",
        "2026-08-29T23:00:00+00:00",
    ]
    for i, now in enumerate(seed_times):
        coordinator.run("ウマ娘", run_key=f"seed{i}", now_iso=now)

    # 手动置为 track（模拟既有追尾目标），再三轮 not_qualified 降级
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE target_states SET state='track', promotion_streak=0,"
            " demotion_streak=0, observation_count=3 WHERE novel_id=7002"
        )
    finally:
        conn.close()

    slow_rounds = [
        [make_novel(7002, 5003, 200)],
        [make_novel(7002, 5004, 200)],
        [make_novel(7002, 5005, 200)],
    ]
    coordinator2 = make(ScriptedAdapter(slow_rounds))
    for i, now in enumerate([T0, T1, T2]):
        outcome = coordinator2.run("ウマ娘", run_key=f"slow{i}", now_iso=now)
        assert outcome.status == "succeeded"

    conn = connect(db_path)
    try:
        state = conn.execute(
            "SELECT * FROM target_states WHERE novel_id=7002"
        ).fetchone()
        assert state["state"] == "broad"
        assert state["demotion_streak"] == 0

        transition = conn.execute(
            "SELECT * FROM state_transitions WHERE novel_id=7002"
            " AND from_state='track' AND to_state='broad'"
        ).fetchone()
        assert transition is not None
        assert transition["reason_code"] == "demotion_reached"

        event = conn.execute(
            "SELECT * FROM events WHERE type='target_left_track'"
        ).fetchone()
        assert event is not None
    finally:
        conn.close()


def test_small_denominator_never_promotes(db_path, make):
    rounds = [
        [make_novel(7003, 1, 1)],
        [make_novel(7003, 2, 2)],
        [make_novel(7003, 3, 3)],
    ]
    coordinator = make(ScriptedAdapter(rounds))
    for i, now in enumerate([T0, T1, T2]):
        coordinator.run("ウマ娘", run_key=f"k{i}", now_iso=now)

    conn = connect(db_path)
    try:
        state = conn.execute(
            "SELECT * FROM target_states WHERE novel_id=7003"
        ).fetchone()
        assert state["state"] == "cold_start"
        assert state["observation_count"] == 0  # 曝光不足不计观察
        transitions = conn.execute(
            "SELECT * FROM state_transitions WHERE novel_id=7003"
        ).fetchall()
        assert len(transitions) == 0
        metrics = conn.execute(
            "SELECT result FROM target_metrics WHERE novel_id=7003"
            " ORDER BY scan_run_id"
        ).fetchall()
        assert [m["result"] for m in metrics] == ["unknown"] * 3
    finally:
        conn.close()


def test_count_regression_unknown_and_no_demotion(db_path, make):
    # 种子：两轮历史（track 目标），随后回归 → unknown → 状态保持、streak 不清零
    seed_rounds = [
        [make_novel(7004, 300, 10)],
        [make_novel(7004, 320, 12)],
    ]
    coordinator = make(ScriptedAdapter(seed_rounds))
    seed_times = [
        "2026-08-29T22:00:00+00:00",
        "2026-08-29T23:00:00+00:00",
    ]
    for i, now in enumerate(seed_times):
        coordinator.run("ウマ娘", run_key=f"seed{i}", now_iso=now)

    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE target_states SET state='track', promotion_streak=0,"
            " demotion_streak=0, observation_count=3 WHERE novel_id=7004"
        )
    finally:
        conn.close()

    rounds = [
        [make_novel(7004, 290, 9)],  # 倒退 → unknown，保持 track
        [make_novel(7004, 310, 11)],  # 恢复 → qualified，dstreak 清零
    ]
    coordinator2 = make(ScriptedAdapter(rounds))
    for i, now in enumerate([T0, T1]):
        coordinator2.run("ウマ娘", run_key=f"k{i}", now_iso=now)

    conn = connect(db_path)
    try:
        state = conn.execute(
            "SELECT * FROM target_states WHERE novel_id=7004"
        ).fetchone()
        assert state["state"] == "track"
        assert state["demotion_streak"] == 0  # unknown 不累计、qualified 清零
        transitions = conn.execute(
            "SELECT * FROM state_transitions WHERE novel_id=7004"
        ).fetchall()
        assert len(transitions) == 0

        dq = conn.execute(
            "SELECT * FROM events WHERE type='data_quality_unknown'"
            " AND novel_id=7004"
        ).fetchall()
        assert len(dq) >= 1
    finally:
        conn.close()


def test_rate_limited_abort_records_cooldown(db_path, make):
    coordinator = make(
        ScriptedAdapter([RateLimited("rate limited", cooldown_seconds=120.0)])
    )
    outcome = coordinator.run("ウマ娘", run_key="k1", now_iso=T0)
    assert outcome.status == "failed"
    assert outcome.cooldown_seconds == 120.0
    assert _count(db_path, "targets") == 0

    conn = connect(db_path)
    try:
        run = conn.execute(
            "SELECT status, error FROM scan_runs WHERE id=?", (outcome.run_id,)
        ).fetchone()
        assert run["status"] == "failed"
        assert "rate limited" in run["error"]

        tag_state = conn.execute(
            "SELECT * FROM tag_scan_state WHERE tag='ウマ娘'"
        ).fetchone()
        assert tag_state["last_error_code"] == "rate_limited"
        assert tag_state["cooldown_until"] == add_seconds_iso(T0, 120.0)

        event = conn.execute(
            "SELECT * FROM events WHERE type='scan_rate_limited'"
        ).fetchone()
        assert event is not None
    finally:
        conn.close()


def test_malformed_response_partial(db_path, make):
    coordinator = make(ScriptedAdapter([MalformedResponse("missing novels")]))
    outcome = coordinator.run("ウマ娘", run_key="k1", now_iso=T0)
    assert outcome.status == "partial"
    assert _count(db_path, "targets") == 0

    conn = connect(db_path)
    try:
        tag_state = conn.execute(
            "SELECT * FROM tag_scan_state WHERE tag='ウマ娘'"
        ).fetchone()
        assert tag_state["last_error_code"] == "malformed_response"
        event = conn.execute(
            "SELECT * FROM events WHERE type='scan_partial'"
        ).fetchone()
        assert event is not None
    finally:
        conn.close()


def test_lease_held_blocks_second_coordinator(db_path, make):
    conn = connect(db_path)
    try:
        repo = PhaseCRepository()
        assert repo.acquire(
            conn, "tag_scan:ウマ娘", "other-owner", now_iso=T0,
            expires_at=add_seconds_iso(T0, 300),
        )
    finally:
        conn.close()

    coordinator = make(ScriptedAdapter([[make_novel(1, 300, 10)]]))
    outcome = coordinator.run("ウマ娘", run_key="k1", now_iso=T0)
    assert outcome.status == "failed"
    assert outcome.error == "lease_held"
    assert _count(db_path, "scan_runs") == 0  # 未创建 run


def test_mid_item_failure_rolls_back_current_item_and_preserves_progress(
    db_path, make
):
    coordinator = make(
        ScriptedAdapter([[make_novel(1, 300, 10), make_novel(2, 500, 20)]]),
        phase_repo=FailingPhaseRepo(fail_after_metrics=1),
    )
    outcome = coordinator.run("ウマ娘", run_key="k1", now_iso=T0)
    assert outcome.status == "failed"

    # item 1 已完整提交；item 2 的 target/snapshot/metric/state 在自身事务内回滚。
    assert _count(db_path, "targets") == 1
    assert _count(db_path, "metric_snapshots") == 1
    assert _count(db_path, "target_states") == 1
    assert _count(db_path, "target_metrics") == 1
    assert _count(db_path, "scan_run_items") == 1
    assert _count(db_path, "state_transitions") == 0

    conn = connect(db_path)
    try:
        run = conn.execute(
            "SELECT status, error, results_seen, results_written"
            " FROM scan_runs WHERE id=?",
            (outcome.run_id,),
        ).fetchone()
        assert run["status"] == "failed"
        assert run["error"]
        assert run["results_seen"] == 2
        assert run["results_written"] == 1
        assert outcome.results_seen == 2
        assert outcome.results_written == 1
    finally:
        conn.close()


def test_resume_unfinished_run(db_path, make):
    # 模拟中断：只创建了 run 行（未写入任何业务数据）
    conn = connect(db_path)
    try:
        repo = PhaseCRepository()
        run_id, created = repo.create_run(
            conn, run_key="kx", source="mock", layer="broad", started_at=T0
        )
        assert created
    finally:
        conn.close()

    coordinator = make(ScriptedAdapter([[make_novel(1, 300, 10)]]))
    outcome = coordinator.run("ウマ娘", run_key="kx", now_iso=T1)
    assert outcome.status == "succeeded"
    assert outcome.run_id == run_id

    assert _count(db_path, "scan_runs") == 1
    assert _count(db_path, "metric_snapshots") == 1
    assert _count(db_path, "target_metrics") == 1


def test_resume_skips_succeeded_item_and_only_fills_missing_item(db_path, make):
    first = make(
        ScriptedAdapter(
            [[make_novel(1, 300, 10), make_novel(2, 400, 20)]]
        )
    )
    completed = first.run("ウマ娘", run_key="partial", now_iso=T0)
    assert completed.status == "succeeded"

    # 模拟进程在 item 1 已提交、item 2 尚未提交时中断。
    conn = connect(db_path)
    try:
        before = dict(
            conn.execute(
                "SELECT * FROM target_states WHERE novel_id=1"
            ).fetchone()
        )
        conn.execute(
            "DELETE FROM target_metrics WHERE novel_id=2 AND scan_run_id=?",
            (completed.run_id,),
        )
        conn.execute(
            "DELETE FROM metric_snapshots WHERE novel_id=2 AND scan_run_id=?",
            (completed.run_id,),
        )
        conn.execute("DELETE FROM target_states WHERE novel_id=2")
        conn.execute(
            "DELETE FROM scan_run_items WHERE novel_id=2 AND scan_run_id=?",
            (completed.run_id,),
        )
        conn.execute(
            "UPDATE scan_runs SET status='running', finished_at=NULL,"
            " results_seen=0, results_written=0 WHERE id=?",
            (completed.run_id,),
        )
    finally:
        conn.close()

    resumed = make(
        ScriptedAdapter(
            [[make_novel(1, 300, 10), make_novel(2, 400, 20)]]
        )
    ).run("ウマ娘", run_key="partial", now_iso=T0)
    assert resumed.status == "succeeded"
    assert resumed.results_seen == 2
    assert resumed.results_written == 2

    conn = connect(db_path)
    try:
        after = dict(
            conn.execute(
                "SELECT * FROM target_states WHERE novel_id=1"
            ).fetchone()
        )
        assert after["observation_count"] == before["observation_count"]
        assert after["promotion_streak"] == before["promotion_streak"]
        assert after["last_evaluated_run_id"] == before["last_evaluated_run_id"]
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM scan_run_items WHERE scan_run_id=?",
            (completed.run_id,),
        ).fetchone()["n"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM target_metrics WHERE scan_run_id=?",
            (completed.run_id,),
        ).fetchone()["n"] == 2
    finally:
        conn.close()


def test_writer_stops_when_lease_is_taken_over_during_fetch(db_path, make):
    class TakeoverAdapter:
        def __init__(self):
            self.taken = False

        def search_novels(self, *args, **kwargs):
            conn = connect(db_path)
            try:
                self.taken = PhaseCRepository().acquire(
                    conn,
                    "tag_scan:ウマ娘",
                    "new-owner",
                    now_iso=add_seconds_iso(T0, 301),
                    expires_at=add_seconds_iso(T0, 601),
                )
            finally:
                conn.close()
            return [make_novel(1, 300, 10)]

    adapter = TakeoverAdapter()
    outcome = make(adapter).run("ウマ娘", run_key="lease-lost", now_iso=T0)
    assert adapter.taken is True
    assert outcome.status == "failed"
    assert outcome.error == "lease_lost"
    assert _count(db_path, "targets") == 0

    conn = connect(db_path)
    try:
        run = conn.execute(
            "SELECT status, finished_at FROM scan_runs WHERE id=?",
            (outcome.run_id,),
        ).fetchone()
        assert run["status"] == "running"  # 留给新 owner 按同一 run_key 恢复
        assert run["finished_at"] is None
        assert PhaseCRepository().get_lease(
            conn, "tag_scan:ウマ娘"
        )["owner"] == "new-owner"
    finally:
        conn.close()


def test_failure_preserves_last_success_and_cursor(db_path, make):
    ok = make(ScriptedAdapter([[make_novel(1, 300, 10)]]))
    assert ok.run("ウマ娘", run_key="ok", now_iso=T0).status == "succeeded"
    conn = connect(db_path)
    try:
        PhaseCRepository().upsert_tag_state(
            conn, "ウマ娘", cursor="cursor-1", next_due=T2
        )
    finally:
        conn.close()

    failed = make(
        ScriptedAdapter([RateLimited("limited", cooldown_seconds=60)])
    ).run("ウマ娘", run_key="failed", now_iso=T1)
    assert failed.status == "failed"

    conn = connect(db_path)
    try:
        state = PhaseCRepository().get_tag_state(conn, "ウマ娘")
        assert state["last_success_at"] == T0
        assert state["cursor"] == "cursor-1"
        assert state["next_due"] == T2
        assert state["last_error_code"] == "rate_limited"
        assert state["cooldown_until"] == add_seconds_iso(T1, 60)
    finally:
        conn.close()


def test_snapshot_history_window_uses_most_recent_rows(db_path, make):
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO targets (novel_id,title,first_seen_at,last_seen_at)"
            " VALUES (1,'n',?,?)",
            (T0, T0),
        )
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(1, 203):
            stamp = (base + timedelta(hours=i)).isoformat()
            cur = conn.execute(
                "INSERT INTO scan_runs"
                " (layer,status,started_at,finished_at,run_key,source)"
                " VALUES ('broad','succeeded',?,?,?,'test')",
                (stamp, stamp, f"history-{i}"),
            )
            conn.execute(
                "INSERT INTO metric_snapshots"
                " (novel_id,scan_run_id,total_bookmarks,total_view,"
                " total_comments,captured_at) VALUES (1,?,?,?,0,?)",
                (cur.lastrowid, i, i * 10, stamp),
            )
    finally:
        conn.close()

    coordinator = make(ScriptedAdapter([[]]))
    conn = connect(db_path)
    try:
        history = coordinator._snapshot_history(
            conn, 1, exclude_run=202, limit=200
        )
    finally:
        conn.close()
    assert history[0].scan_run_id == 2
    assert history[-1].scan_run_id == 201
