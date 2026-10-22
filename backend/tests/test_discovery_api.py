"""只读 Discovery API 测试 + PixivPy 映射骨架测试（全部离线）。"""

import pytest

from app.discovery import make_query_key
from app.discovery.fake import FakeDiscoveryAdapter
from app.discovery.service import DiscoveryService
from app.discovery.types import DiscoveryQuery
from app.pixiv.adapter import NovelSummary
from app.pixiv.discovery_pixivpy import PixivPyDiscoveryQueryMapper
from app.storage.connection import connect
from app.storage.discovery_repository import DiscoveryRepository
from app.storage.migrations import apply_migrations
from app.storage.scan_repository import ScanRepository

T0 = "2026-09-01T00:00:00+00:00"


def make_novel(novel_id):
    return NovelSummary(
        novel_id=novel_id,
        title=f"n{novel_id}",
        tags=(),
        text_length=100,
        total_bookmarks=1,
        total_view=100,
        total_comments=0,
    )


def q(kind, text=None, entity_id=None):
    return DiscoveryQuery(
        query_key=make_query_key(kind, text, entity_id),
        source_kind=kind,
        text=text,
        entity_id=entity_id,
    )


def _seed(tmp_path):
    db_path = tmp_path / "state" / "radar.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    adapter = FakeDiscoveryAdapter(
        {
            "exact_tag:ウマ娘": [make_novel(1)],
            "novel_new:all": [make_novel(1), make_novel(2)],
            "author_novels:9": [make_novel(1)],
            "keyword:empty": [],
        }
    )
    service = DiscoveryService(
        adapter, ScanRepository(), DiscoveryRepository(), db_path
    )
    outcome = service.run(
        run_key="api-run",
        queries=[
            q("exact_tag", "ウマ娘"),
            q("novel_new"),
            q("author_novels", entity_id=9),
            q("keyword", "empty"),
        ],
        now_iso=T0,
    )
    return db_path, outcome


def test_runs_endpoint(client, tmp_path):
    _seed(tmp_path)
    resp = client.get("/api/discovery/runs")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["run_key"] == "api-run"
    assert items[0]["status"] == "succeeded"
    # 不返回 cursor/错误正文等敏感字段
    for forbidden in ("cursor", "error_body", "response"):
        assert forbidden not in items[0]


def test_candidates_multi_source_in_one_response(client, tmp_path):
    _, outcome = _seed(tmp_path)
    resp = client.get(
        "/api/discovery/candidates", params={"run_id": outcome.run_id}
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2  # novel 1 与 novel 2
    by_id = {i["novel_id"]: i for i in items}
    assert len(by_id[1]["sightings"]) == 3
    kinds = {s["source_kind"] for s in by_id[1]["sightings"]}
    assert kinds == {"exact_tag", "novel_new", "author_novels"}
    assert len(by_id[2]["sightings"]) == 1


def test_candidates_run_missing_404(client, tmp_path):
    _seed(tmp_path)
    resp = client.get("/api/discovery/candidates", params={"run_id": 99999})
    assert resp.status_code == 404


def test_candidates_filter_by_novel_id_for_detail_reason(client, tmp_path):
    _, outcome = _seed(tmp_path)
    hit = client.get(
        "/api/discovery/candidates",
        params={"run_id": outcome.run_id, "novel_id": 2},
    )
    assert hit.status_code == 200
    payload = hit.json()
    assert payload["total"] == 1
    assert [item["novel_id"] for item in payload["items"]] == [2]
    assert payload["items"][0]["group_reason"]

    miss = client.get(
        "/api/discovery/candidates",
        params={"run_id": outcome.run_id, "novel_id": 999},
    )
    assert miss.status_code == 200
    assert miss.json()["total"] == 0
    assert miss.json()["items"] == []


def test_candidates_pagination_and_invalid_params(client, tmp_path):
    _, outcome = _seed(tmp_path)
    resp = client.get(
        "/api/discovery/candidates",
        params={"run_id": outcome.run_id, "limit": "1", "offset": "0"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    # 越界分页 → 空列表 200
    resp = client.get(
        "/api/discovery/candidates",
        params={"run_id": outcome.run_id, "offset": "1000"},
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    # 恶意参数 → 422
    assert (
        client.get(
            "/api/discovery/candidates", params={"run_id": outcome.run_id, "limit": "1000"}
        ).status_code
        == 422
    )
    assert (
        client.get("/api/discovery/runs", params={"limit": "0"}).status_code == 422
    )


@pytest.mark.parametrize(
    "kind,text,entity_id,method,key_param",
    [
        ("exact_tag", "t", None, "search_novel", "exact_match_for_tags"),
        ("partial_tag", "t", None, "search_novel", "partial_match_for_tags"),
        ("keyword", "k", None, "search_novel", "keyword"),
        ("novel_new", None, None, "novel_new", None),
        ("novel_recommended", None, None, "novel_recommended", None),
        ("author_novels", None, 9, "user_novels", 9),
        ("series_novels", None, 7, "novel_series", 7),
        ("novel_follow", None, None, "novel_follow", None),
    ],
)
def test_pixivpy_mapper_eight_kinds(kind, text, entity_id, method, key_param):
    query = q(kind, text, entity_id)
    mapped_method, kwargs = PixivPyDiscoveryQueryMapper.map(query)
    assert mapped_method == method
    if kind in ("exact_tag", "partial_tag", "keyword"):
        assert kwargs["search_target"] == key_param
        assert kwargs["word"] == (text or "")
    elif kind in ("author_novels", "series_novels"):
        assert list(kwargs.values())[0] == key_param


def test_pixivpy_mapper_unsupported_kind_rejected():
    with pytest.raises(ValueError):
        PixivPyDiscoveryQueryMapper.map(q("evil_kind"))
