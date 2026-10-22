"""阶段 C 持久化仓库测试：run_key 幂等、run item、租约竞争、标签状态、
指标/转换/事件幂等（全部离线，时间显式注入）。"""

import pytest

from app.core.timeutil import add_seconds_iso
from app.storage.connection import connect
from app.storage.migrations import apply_migrations
from app.storage.phase_c_repository import PhaseCRepository

T0 = "2026-08-30T00:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "radar.db")
    try:
        apply_migrations(c)
        yield c
    finally:
        c.close()


@pytest.fixture
def repo():
    return PhaseCRepository()


def _insert_target(conn, novel_id: int) -> None:
    conn.execute(
        "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at)"
        " VALUES (?, 't', ?, ?)",
        (novel_id, T0, T0),
    )


# ---- run_key 幂等 ----


def test_create_run_duplicate_key_returns_existing(conn, repo):
    run_id, created = repo.create_run(
        conn, run_key="key-1", source="mock", layer="broad", started_at=T0
    )
    assert created is True
    again, created_again = repo.create_run(
        conn, run_key="key-1", source="mock", layer="broad", started_at=T0
    )
    assert again == run_id
    assert created_again is False

    n = conn.execute("SELECT COUNT(*) AS n FROM scan_runs").fetchone()["n"]
    assert n == 1


def test_create_run_different_keys_new_runs(conn, repo):
    a, _ = repo.create_run(
        conn, run_key="key-a", source="mock", layer="broad", started_at=T0
    )
    b, _ = repo.create_run(
        conn, run_key="key-b", source="mock", layer="broad", started_at=T0
    )
    assert a != b


def test_create_run_null_key_always_new(conn, repo):
    a, _ = repo.create_run(
        conn, run_key=None, source="mock", layer="broad", started_at=T0
    )
    b, _ = repo.create_run(
        conn, run_key=None, source="mock", layer="broad", started_at=T0
    )
    assert a != b


# ---- run item ----


def test_run_item_upsert_and_list(conn, repo):
    run_id, _ = repo.create_run(
        conn, run_key="k", source="mock", layer="broad", started_at=T0
    )
    repo.upsert_run_item(
        conn, run_id, 1001, status="succeeded", attempt=1, completed_at=T0
    )
    repo.upsert_run_item(conn, run_id, 1002, status="pending")
    # 重复 upsert 不产生第二行
    repo.upsert_run_item(conn, run_id, 1001, status="succeeded", attempt=1)

    items = repo.list_run_items(conn, run_id)
    assert len(items) == 2
    by_id = {item["novel_id"]: item for item in items}
    assert by_id[1001]["status"] == "succeeded"
    assert by_id[1002]["status"] == "pending"


def test_run_item_invalid_status_rejected(conn, repo):
    run_id, _ = repo.create_run(
        conn, run_key="k", source="mock", layer="broad", started_at=T0
    )
    with pytest.raises(ValueError):
        repo.upsert_run_item(conn, run_id, 1, status="weird")


# ---- 租约 ----


def test_lease_acquire_renew_release(conn, repo):
    assert repo.acquire(
        conn, "tag:ウマ娘", "owner-A", now_iso=T0,
        expires_at=add_seconds_iso(T0, 300),
    )

    # 未过期不可抢占
    assert repo.acquire(
        conn, "tag:ウマ娘", "owner-B", now_iso=add_seconds_iso(T0, 100),
        expires_at=add_seconds_iso(add_seconds_iso(T0, 100), 300),
    ) is False

    # owner 匹配且未过期可续租，过期时间延长
    later = add_seconds_iso(T0, 150)
    assert repo.renew(
        conn, "tag:ウマ娘", "owner-A", now_iso=later,
        expires_at=add_seconds_iso(later, 300),
    ) is True
    lease = repo.get_lease(conn, "tag:ウマ娘")
    assert lease["expires_at"] == add_seconds_iso(later, 300)

    # owner 不匹配不能续租
    assert repo.renew(
        conn, "tag:ウマ娘", "owner-B", now_iso=later,
        expires_at=add_seconds_iso(later, 300),
    ) is False

    # owner 匹配释放成功
    assert repo.release(conn, "tag:ウマ娘", "owner-A") is True
    assert repo.get_lease(conn, "tag:ウマ娘") is None


def test_lease_expired_takeover_and_old_owner_protection(conn, repo):
    assert repo.acquire(
        conn, "tag:ウマ娘", "owner-A", now_iso=T0,
        expires_at=add_seconds_iso(T0, 300),
    )
    # 过期后 B 可接管
    takeover_time = add_seconds_iso(T0, 301)
    assert repo.acquire(
        conn, "tag:ウマ娘", "owner-B", now_iso=takeover_time,
        expires_at=add_seconds_iso(takeover_time, 300),
    ) is True
    lease = repo.get_lease(conn, "tag:ウマ娘")
    assert lease["owner"] == "owner-B"

    # 旧 owner 不能释放新租约
    assert repo.release(conn, "tag:ウマ娘", "owner-A") is False
    # 过期后旧 owner 也不能续租（owner 已变）
    assert repo.renew(
        conn, "tag:ウマ娘", "owner-A", now_iso=takeover_time,
        expires_at=add_seconds_iso(takeover_time, 300),
    ) is False


# ---- 标签扫描状态 ----


def test_tag_scan_state_upsert(conn, repo):
    repo.upsert_tag_state(
        conn,
        "ウマ娘",
        cursor="cursor-1",
        next_due=add_seconds_iso(T0, 3600),
        last_success_at=T0,
        cooldown_until=None,
    )
    state = repo.get_tag_state(conn, "ウマ娘")
    assert state["last_success_at"] == T0
    assert state["cooldown_until"] is None

    repo.upsert_tag_state(
        conn, "ウマ娘",
        last_error_code="rate_limited",
        cooldown_until=add_seconds_iso(T0, 120),
    )
    state = repo.get_tag_state(conn, "ウマ娘")
    assert state["last_error_code"] == "rate_limited"
    assert state["cooldown_until"] == add_seconds_iso(T0, 120)
    assert state["cursor"] == "cursor-1"
    assert state["next_due"] == add_seconds_iso(T0, 3600)
    assert state["last_success_at"] == T0


# ---- 目标状态 ----


def test_target_state_upsert_and_validation(conn, repo):
    _insert_target(conn, 1001)
    repo.upsert_target_state(
        conn, 1001, state="cold_start", observation_count=0,
        promotion_streak=0, demotion_streak=0,
        last_evaluated_run_id=None, updated_at=T0,
    )
    state = repo.get_target_state(conn, 1001)
    assert state["state"] == "cold_start"

    with pytest.raises(ValueError):
        repo.upsert_target_state(
            conn, 1001, state="invalid_state", observation_count=0,
            promotion_streak=0, demotion_streak=0,
            last_evaluated_run_id=None, updated_at=T0,
        )


# ---- 指标 ----


def test_metric_insert_unique_update(conn, repo):
    _insert_target(conn, 1001)
    run_id, _ = repo.create_run(
        conn, run_key="k", source="mock", layer="broad", started_at=T0
    )
    repo.insert_metric(
        conn, 1001, run_id, exposure_eligible=True,
        smoothed_bookmark_rate=0.01, view_velocity=10.0,
        bookmark_velocity=1.0, ewma_view_velocity=10.0,
        threshold=5.0, threshold_source="fallback", sample_count=3,
        result="qualified", reason_codes=[], created_at=T0,
    )
    # 重复插入更新而非新增
    repo.insert_metric(
        conn, 1001, run_id, exposure_eligible=True,
        smoothed_bookmark_rate=0.02, view_velocity=20.0,
        bookmark_velocity=2.0, ewma_view_velocity=13.5,
        threshold=5.0, threshold_source="fallback", sample_count=3,
        result="qualified", reason_codes=[], created_at=T0,
    )
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM target_metrics WHERE novel_id=1001"
    ).fetchone()["n"]
    assert n == 1
    latest = repo.get_latest_metric(conn, 1001)
    assert latest["ewma_view_velocity"] == 13.5


def test_metric_reason_codes_json(conn, repo):
    _insert_target(conn, 1001)
    run_id, _ = repo.create_run(
        conn, run_key="k", source="mock", layer="broad", started_at=T0
    )
    repo.insert_metric(
        conn, 1001, run_id, exposure_eligible=False,
        smoothed_bookmark_rate=None, view_velocity=None,
        bookmark_velocity=None, ewma_view_velocity=None,
        threshold=None, threshold_source=None, sample_count=None,
        result="unknown", reason_codes=["insufficient_exposure"], created_at=T0,
    )
    import json

    row = conn.execute(
        "SELECT reason_codes FROM target_metrics WHERE novel_id=1001"
    ).fetchone()
    assert json.loads(row["reason_codes"]) == ["insufficient_exposure"]


# ---- 转换与事件幂等 ----


def test_transition_dedupe(conn, repo):
    _insert_target(conn, 1001)
    run_id, _ = repo.create_run(
        conn, run_key="k", source="mock", layer="broad", started_at=T0
    )
    assert repo.insert_transition(
        conn, novel_id=1001, from_state="cold_start", to_state="track",
        reason_code="promotion_reached", evidence={"streak": 2},
        scan_run_id=run_id, created_at=T0,
    ) is True
    assert repo.insert_transition(
        conn, novel_id=1001, from_state="cold_start", to_state="track",
        reason_code="promotion_reached", evidence={"streak": 2},
        scan_run_id=run_id, created_at=T0,
    ) is False
    rows = repo.list_transitions(conn, novel_id=1001, limit=10)
    assert len(rows) == 1


def test_event_dedupe_and_list(conn, repo):
    run_id, _ = repo.create_run(
        conn, run_key="k", source="mock", layer="broad", started_at=T0
    )
    assert repo.insert_event(
        conn, event_key="target_entered_track:1001:1", type_="target_entered_track",
        severity="info", payload={}, novel_id=1001, scan_run_id=run_id,
        created_at=T0,
    ) is True
    assert repo.insert_event(
        conn, event_key="target_entered_track:1001:1", type_="target_entered_track",
        severity="info", payload={}, novel_id=1001, scan_run_id=run_id,
        created_at=T0,
    ) is False
    events = repo.list_events(conn, limit=10, offset=0)
    assert len(events) == 1
    assert events[0]["type"] == "target_entered_track"
