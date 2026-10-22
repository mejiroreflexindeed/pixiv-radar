"""只读 API 测试：seed 数据后验证 /api/targets 与 /api/scan-runs（全部离线）。"""

from app.domain.broad_scan import BroadScanService
from app.pixiv.mock import MockPixivAdapter
from app.storage.connection import connect
from app.storage.migrations import apply_migrations
from app.storage.scan_repository import ScanRepository


def _seed(tmp_path):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    service = BroadScanService(
        adapter=MockPixivAdapter(), repository=ScanRepository(), db_path=db_path
    )
    return service.run("ウマ娘", limit=5)


def test_targets_returns_seeded_data(client, tmp_path):
    outcome = _seed(tmp_path)
    assert outcome.status == "succeeded"

    resp = client.get("/api/targets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert item["novel_id"] in (1001, 1002)
    assert item["title"]
    assert item["total_bookmarks"] > 0
    assert item["snapshot_at"]  # 最新快照时间

    latest = body["latest_scan"]
    assert latest["status"] == "succeeded"
    assert latest["results_written"] == 2


def test_targets_empty_state(client, tmp_path):
    resp = client.get("/api/targets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["latest_scan"] is None


def test_targets_pagination_capped(client):
    resp = client.get("/api/targets?limit=1000")
    assert resp.status_code == 422  # limit 上限 100

    resp = client.get("/api/targets?limit=0")
    assert resp.status_code == 422


def test_targets_pagination_works(client, tmp_path):
    _seed(tmp_path)
    first = client.get("/api/targets?limit=1&offset=0").json()
    second = client.get("/api/targets?limit=1&offset=1").json()
    assert first["total"] == 2
    assert len(first["items"]) == 1
    assert len(second["items"]) == 1
    assert first["items"][0]["novel_id"] != second["items"][0]["novel_id"]


def test_scan_runs_returns_recent(client, tmp_path):
    _seed(tmp_path)
    _seed(tmp_path)  # 再跑一轮
    resp = client.get("/api/scan-runs")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert all(item["status"] == "succeeded" for item in items)
    assert items[0]["id"] > items[1]["id"]  # 按新到旧


def test_scan_runs_limit_capped(client):
    resp = client.get("/api/scan-runs?limit=1000")
    assert resp.status_code == 422
