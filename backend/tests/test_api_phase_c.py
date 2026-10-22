"""WP5 只读 API 测试：detail/history/events/transitions + targets 扩展字段。"""

from types import SimpleNamespace

import pytest

from app.domain.scan_coordinator import ScanCoordinator
from app.pixiv.adapter import NovelSummary, NovelTag
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
        return list(item)

    def novel_detail(self, novel_id):
        raise NotImplementedError

    def novel_text(self, novel_id):
        raise NotImplementedError


def make_novel(novel_id, views, bookmarks):
    return NovelSummary(
        novel_id=novel_id, title=f"标题{novel_id}",
        tags=(NovelTag("ウマ娘"),),
        text_length=1000, total_bookmarks=bookmarks, total_view=views,
        total_comments=0, is_original=True,
        create_date="2026-08-01T00:00:00+00:00",
    )


def _seed(tmp_path, rounds, times):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    coordinator = ScanCoordinator(
        ScriptedAdapter(rounds), ScanRepository(), PhaseCRepository(), db_path
    )
    for i, now in enumerate(times):
        coordinator.run("ウマ娘", run_key=f"k{i}", now_iso=now, source="mock")
    return db_path


def test_targets_list_extended(client, tmp_path):
    rounds = [
        [make_novel(7001, 300, 10), make_novel(7002, 5000, 200)],
        [make_novel(7001, 500, 25), make_novel(7002, 5001, 200)],
        [make_novel(7001, 700, 40), make_novel(7002, 5002, 200)],
    ]
    _seed(tmp_path, rounds, [T0, T1, T2])

    resp = client.get("/api/targets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["track_total"] == 1

    by_id = {item["novel_id"]: item for item in body["items"]}
    assert by_id[7001]["state"] == "track"  # 三轮晋级
    assert by_id[7001]["observation_count"] == 3
    assert by_id[7001]["latest_result"] == "qualified"
    assert by_id[7001]["data_source"] == "mock"
    assert by_id[7002]["latest_result"] == "not_qualified"


def test_targets_pagination_capped(client):
    resp = client.get("/api/targets?limit=1000")
    assert resp.status_code == 422


def test_target_detail_and_404(client, tmp_path):
    rounds = [[make_novel(7001, 300, 10)]]
    _seed(tmp_path, rounds, [T0])

    resp = client.get("/api/targets/7001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["novel_id"] == 7001
    assert body["state"] == "cold_start"
    assert body["latest_result"] == "unknown"

    resp = client.get("/api/targets/999999")
    assert resp.status_code == 404


def test_target_history_ordered(client, tmp_path):
    rounds = [
        [make_novel(7001, 300, 10)],
        [make_novel(7001, 500, 25)],
        [make_novel(7001, 700, 40)],
    ]
    _seed(tmp_path, rounds, [T0, T1, T2])

    resp = client.get("/api/targets/7001/history?limit=100")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    # 按 captured_at ASC 稳定排序
    assert items[0]["captured_at"] == T0
    assert items[1]["captured_at"] == T1
    assert items[2]["captured_at"] == T2
    assert [item["result"] for item in items] == [
        "unknown", "qualified", "qualified",
    ]

    resp = client.get("/api/targets/999999/history")
    assert resp.status_code == 404

    resp = client.get("/api/targets/7001/history?limit=1000")
    assert resp.status_code == 422


def test_target_history_limit_returns_most_recent_rows(client, tmp_path):
    rounds = [
        [make_novel(7001, 300 + i * 100, 10 + i)]
        for i in range(5)
    ]
    times = [f"2026-08-30T0{i}:00:00+00:00" for i in range(5)]
    _seed(tmp_path, rounds, times)

    resp = client.get("/api/targets/7001/history?limit=2")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["captured_at"] for item in items] == times[-2:]


def test_events_list(client, tmp_path):
    rounds = [
        [make_novel(7001, 300, 10)],
        [make_novel(7001, 500, 25)],
        [make_novel(7001, 700, 40)],
    ]
    _seed(tmp_path, rounds, [T0, T1, T2])

    resp = client.get("/api/events?limit=100")
    assert resp.status_code == 200
    items = resp.json()["items"]
    types = {item["type"] for item in items}
    assert "target_entered_track" in types
    assert all("payload_json" not in item for item in items)
    assert all(isinstance(item["payload"], dict) for item in items)
    # 三连 qualified 直接 cold_start→track，不经过 cold_start_completed

    resp = client.get("/api/events?limit=1000")
    assert resp.status_code == 422


def test_state_transitions_filter(client, tmp_path):
    rounds = [
        [make_novel(7001, 300, 10)],
        [make_novel(7001, 500, 25)],
        [make_novel(7001, 700, 40)],
    ]
    _seed(tmp_path, rounds, [T0, T1, T2])

    resp = client.get("/api/state-transitions?novel_id=7001&limit=100")
    assert resp.status_code == 200
    items = resp.json()["items"]
    # 种子阶段结束时 cold_start→broad（第 3 轮前）→ 实际上第 3 轮直接 track
    # 预期只有一条 cold_start→track
    assert len(items) == 1
    assert items[0]["from_state"] == "cold_start"
    assert items[0]["to_state"] == "track"
    assert items[0]["dedupe_key"] == "7001:3:cold_start:track"
    assert "evidence_json" not in items[0]
    assert isinstance(items[0]["evidence"], dict)

    resp = client.get("/api/state-transitions?limit=1000")
    assert resp.status_code == 422


def test_track_total_is_not_limited_to_current_page(client, tmp_path):
    first = [make_novel(i, 300, 10) for i in range(1, 61)]
    second = [make_novel(i, 500, 20) for i in range(1, 61)]
    third = [make_novel(i, 700, 30) for i in range(1, 61)]
    _seed(tmp_path, [first, second, third], [T0, T1, T2])

    resp = client.get("/api/targets?limit=20")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 20
    assert body["total"] == 60
    assert body["track_total"] == 60
