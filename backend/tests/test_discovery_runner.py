"""DiscoveryRunner 与演示 fake 适配器测试（全部离线，不访问 Pixiv）。"""

import threading
import time

import pytest

from app.discovery.demo_fake import DemoFakeDiscoveryAdapter, demo_novel
from app.discovery.runner import (
    DiscoveryBusyError,
    DiscoveryRunner,
    demo_queries,
    real_queries,
)
from app.pixiv.exceptions import TemporaryFailure
from app.storage.connection import connect
from app.storage.discovery_repository import DiscoveryRepository
from app.storage.migrations import apply_migrations
from app.storage.scan_repository import ScanRepository


@pytest.fixture
def db_path(tmp_path):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return db_path


def make_runner(db_path, adapter=None):
    return DiscoveryRunner(
        adapter or DemoFakeDiscoveryAdapter(),
        ScanRepository(),
        DiscoveryRepository(),
        db_path,
    )


def wait_terminal(runner, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.status()
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("run did not reach a terminal state")


def test_demo_queries_fixed_config():
    queries = demo_queries()
    assert len(queries) == 8
    kinds = [q.source_kind for q in queries]
    assert "exact_tag" in kinds
    assert "partial_tag" in kinds
    assert "keyword" in kinds
    assert "novel_new" in kinds
    assert all(q.limit <= 30 for q in queries)


def test_real_queries_are_tag_scoped_and_request_bounded():
    queries = real_queries(" ウマ娘 ")
    assert [q.source_kind for q in queries] == [
        "exact_tag",
        "partial_tag",
        "keyword",
    ]
    assert all(q.text == "ウマ娘" for q in queries)
    assert len(queries) == 3
    with pytest.raises(ValueError):
        real_queries("   ")


def test_runner_full_run_with_demo_fake(db_path):
    adapter = DemoFakeDiscoveryAdapter()
    runner = make_runner(db_path, adapter)
    run_key, _ = runner.start()
    status = wait_terminal(runner)
    # 首次运行：keyword 来源临时失败 → 部分成功（可重试）
    assert status["state"] == "partial"
    assert status["current_run"]["error_code"] == "temporary_failure"
    assert status["current_run"]["unique_candidates"] >= 3

    # 重试：keyword 来源已恢复 → succeeded
    run_key2, _ = runner.start()
    status2 = wait_terminal(runner)
    assert status2["state"] == "succeeded"
    assert status2["current_run"]["unique_candidates"] >= 4

    # 候选与来源留痕
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_kind FROM discovery_sightings"
        ).fetchall()
        kinds = {row["source_kind"] for row in rows}
        assert "exact_tag" in kinds
        assert "novel_new" in kinds
        # 多来源重复 novel 1 保留多条 sighting
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM discovery_sightings WHERE novel_id=1"
        ).fetchone()["n"]
        assert n >= 2
    finally:
        conn.close()


def test_runner_busy_rejects_second_start(db_path):
    runner = make_runner(db_path)
    runner.start()
    with pytest.raises(DiscoveryBusyError):
        runner.start()
    wait_terminal(runner)


def test_runner_concurrent_ui_and_scheduler_start_only_one_run(db_path, monkeypatch):
    runner = make_runner(db_path)
    both_at_recovery = threading.Barrier(2)
    release_worker = threading.Event()

    monkeypatch.setattr(runner, "_ensure_recovered", both_at_recovery.wait)
    monkeypatch.setattr(
        runner,
        "_work",
        lambda *args: release_worker.wait(2),
    )
    outcomes = []

    def attempt_start():
        try:
            outcomes.append(("started", runner.start()[0]))
        except DiscoveryBusyError:
            outcomes.append(("busy", None))

    threads = [threading.Thread(target=attempt_start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert sorted(kind for kind, _ in outcomes) == ["busy", "started"]
    assert len([key for kind, key in outcomes if kind == "started"]) == 1
    release_worker.set()
    assert runner.shutdown(timeout=2)


def test_runner_cancel_marks_cancelled(db_path):
    runner = make_runner(db_path)
    runner.start()
    assert runner.cancel() is True
    status = wait_terminal(runner)
    assert status["state"] in ("cancelled", "partial", "succeeded")
    # 取消后再次 cancel 返回 False（没有运行中的 run）
    assert runner.cancel() is False


def test_runner_recovery_marks_stale_running_runs(db_path):
    """进程重启后不留永久 running：遗留 running run 被收敛为 failed/interrupted。"""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO discovery_runs (run_key, status, started_at, planned_requests)"
            " VALUES ('stale-1', 'running', '2026-09-03T00:00:00+00:00', 8)"
        )
    finally:
        conn.close()
    # 新 runner 实例（模拟进程重启）；恢复在首次交互时惰性触发
    runner = make_runner(db_path)
    runner.status()
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, error_code, finished_at FROM discovery_runs WHERE run_key='stale-1'"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_code"] == "interrupted"
        assert row["finished_at"] is not None
    finally:
        conn.close()


def test_runs_survive_runner_restart(db_path):
    """历史 run 持久化：新 runner 实例（模拟重启）仍能通过 DB 读取。"""
    runner = make_runner(db_path)
    runner.start()
    wait_terminal(runner)
    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM discovery_runs").fetchone()["n"]
        assert n == 1
    finally:
        conn.close()
    # 第二个实例不重新执行，历史仍可读
    make_runner(db_path)
    conn = connect(db_path)
    try:
        n2 = conn.execute("SELECT COUNT(*) AS n FROM discovery_runs").fetchone()["n"]
        assert n2 == 1
    finally:
        conn.close()


def test_runner_status_never_creates_missing_db(tmp_path):
    """缺失库不空建；数据库稍后出现时，同一 runner 仍会执行恢复。"""
    missing = tmp_path / "state" / "never-created.db"
    runner = make_runner(missing)
    status = runner.status()
    assert status["state"] == "idle"
    assert not missing.exists()

    # 应用生命周期可能在首次 status 之后才迁移数据库；此前的“缺失”不能令
    # runner 永久跳过恢复。
    conn = connect(missing)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO discovery_runs (run_key, status, started_at, planned_requests)"
            " VALUES ('late-stale', 'running', '2026-09-03T00:00:00+00:00', 8)"
        )
    finally:
        conn.close()

    runner.status()
    conn = connect(missing)
    try:
        row = conn.execute(
            "SELECT status, error_code FROM discovery_runs WHERE run_key='late-stale'"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_code"] == "interrupted"
    finally:
        conn.close()


def test_demo_fake_keyword_fails_once_then_recovers():
    adapter = DemoFakeDiscoveryAdapter()
    query = demo_queries()[2]
    assert query.source_kind == "keyword"
    with pytest.raises(TemporaryFailure):
        adapter.fetch_page(query, None)
    page = adapter.fetch_page(query, None)
    assert len(page.items) == 1
    assert page.items[0].novel_id == 3


def test_demo_fake_has_required_fixture_shapes():
    adapter = DemoFakeDiscoveryAdapter()
    queries = [q for q in demo_queries() if q.source_kind != "keyword"]
    pages = {
        q.source_kind: adapter.fetch_page(q, None)
        for q in queries
    }
    # 空页存在
    assert any(len(p.items) == 0 for p in pages.values())
    # 小众高增长新作（novel 1 相对收藏高、阅读低）
    exact = next(
        q for q in queries if q.source_kind == "exact_tag"
    )
    page = adapter.fetch_page(exact, None)
    by_id = {n.novel_id: n for n in page.items}
    assert by_id[1].total_view < 1000
    assert by_id[2].total_view >= 1000  # 热门噪声
