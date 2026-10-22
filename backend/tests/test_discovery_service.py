"""DiscoveryService 测试：预算/去重留痕/错误语义/run_key 重放（全部离线）。"""

import sqlite3

import pytest

from app.discovery import MAX_UNIQUE_CANDIDATES, make_query_key
from app.discovery.fake import FakeDiscoveryAdapter
from app.discovery.service import DiscoveryService
from app.discovery.types import DiscoveryPage, DiscoveryQuery
from app.pixiv.adapter import NovelSummary
from app.pixiv.exceptions import AuthFailed, MalformedResponse, RateLimited, TemporaryFailure
from app.storage.connection import connect
from app.storage.discovery_repository import DiscoveryRepository
from app.storage.migrations import apply_migrations
from app.storage.scan_repository import ScanRepository

T0 = "2026-09-01T00:00:00+00:00"


def make_novel(novel_id, **overrides):
    data = dict(
        novel_id=novel_id,
        title=f"n{novel_id}",
        tags=(),
        text_length=100,
        total_bookmarks=1,
        total_view=100,
        total_comments=0,
    )
    data.update(overrides)
    return NovelSummary(**data)


def q(kind, text=None, entity_id=None):
    return DiscoveryQuery(
        query_key=make_query_key(kind, text, entity_id),
        source_kind=kind,
        text=text,
        entity_id=entity_id,
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


def make_service(db_path, adapter):
    return DiscoveryService(
        adapter, ScanRepository(), DiscoveryRepository(), db_path
    )


def test_three_sources_one_candidate_three_sightings(db_path):
    adapter = FakeDiscoveryAdapter(
        {
            "exact_tag:ウマ娘": [make_novel(1, title="t1")],
            "novel_new:all": [make_novel(1, title="t2")],
            "author_novels:9": [make_novel(1, title="t3")],
        }
    )
    service = make_service(db_path, adapter)
    outcome = service.run(
        run_key="r1",
        queries=[q("exact_tag", "ウマ娘"), q("novel_new"), q("author_novels", entity_id=9)],
        now_iso=T0,
    )
    assert outcome.status == "succeeded"
    assert outcome.unique_candidates == 1
    conn = connect(db_path)
    try:
        sightings = conn.execute(
            "SELECT source_kind FROM discovery_sightings ORDER BY source_kind"
        ).fetchall()
        assert [s["source_kind"] for s in sightings] == [
            "author_novels", "exact_tag", "novel_new",
        ]
        target = conn.execute("SELECT title FROM targets WHERE novel_id=1").fetchone()
        assert target["title"] == "t1"  # 来源优先级：exact_tag
    finally:
        conn.close()


def test_run_key_replay_does_not_call_adapter(db_path):
    adapter = FakeDiscoveryAdapter({"exact_tag:ウマ娘": [make_novel(1)]})
    service = make_service(db_path, adapter)
    first = service.run(
        run_key="replay", queries=[q("exact_tag", "ウマ娘")], now_iso=T0
    )
    calls_after_first = len(adapter.calls)
    second = service.run(
        run_key="replay", queries=[q("exact_tag", "ウマ娘")], now_iso=T0
    )
    assert second.run_id == first.run_id
    assert second.status == "succeeded"
    assert len(adapter.calls) == calls_after_first  # 未再次调用 adapter
    conn = connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM discovery_sightings"
        ).fetchone()["n"]
        assert n == 1  # 不重复 sightings
    finally:
        conn.close()


def test_rate_limited_zero_success_is_failed(db_path):
    adapter = FakeDiscoveryAdapter(
        {
            "exact_tag:a": RateLimited("rate limited", cooldown_seconds=60),
            "exact_tag:b": [make_novel(2)],
        }
    )
    service = make_service(db_path, adapter)
    outcome = service.run(
        run_key="rl0", queries=[q("exact_tag", "a"), q("exact_tag", "b")], now_iso=T0
    )
    assert outcome.status == "failed"
    # 429 保留有效 cooldown（rate_limited 或 rate_limited:<秒>）
    assert outcome.error_code is not None
    assert outcome.error_code.startswith("rate_limited")
    assert outcome.executed_requests == 1  # 429 请求也算实际尝试
    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"]
        assert n == 0  # 零成功不落库
        paired = conn.execute("SELECT status FROM scan_runs").fetchone()
        assert paired["status"] == "failed"
    finally:
        conn.close()


def test_rate_limited_after_success_is_partial(db_path):
    adapter = FakeDiscoveryAdapter(
        {
            "exact_tag:a": [make_novel(1)],
            "exact_tag:b": RateLimited("rate limited"),
        }
    )
    service = make_service(db_path, adapter)
    outcome = service.run(
        run_key="rl1", queries=[q("exact_tag", "a"), q("exact_tag", "b")], now_iso=T0
    )
    assert outcome.status == "partial"
    assert outcome.executed_requests == 2  # 成功请求 + 429 请求
    assert outcome.unique_candidates == 1
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT status FROM scan_runs").fetchone()["status"] == "partial"
    finally:
        conn.close()


def test_temporary_failure_continues_other_sources(db_path):
    adapter = FakeDiscoveryAdapter(
        {
            "exact_tag:a": TemporaryFailure("network"),
            "exact_tag:b": [make_novel(2)],
        }
    )
    service = make_service(db_path, adapter)
    outcome = service.run(
        run_key="tf", queries=[q("exact_tag", "a"), q("exact_tag", "b")], now_iso=T0
    )
    assert outcome.status == "partial"
    assert outcome.unique_candidates == 1  # 独立来源继续


def test_all_temporary_failures_are_failed_not_partial(db_path):
    adapter = FakeDiscoveryAdapter(
        {
            "exact_tag:a": TemporaryFailure("network"),
            "exact_tag:b": TemporaryFailure("network"),
        }
    )
    outcome = make_service(db_path, adapter).run(
        run_key="all-tf",
        queries=[q("exact_tag", "a"), q("exact_tag", "b")],
        now_iso=T0,
    )
    assert outcome.status == "failed"
    assert outcome.error_code == "temporary_failure"
    assert outcome.unique_candidates == 0


def test_malformed_continues_and_empty_is_zero_result(db_path):
    adapter = FakeDiscoveryAdapter(
        {
            "exact_tag:a": MalformedResponse("bad"),
            "exact_tag:empty": [],  # 空列表 = 成功零结果
        }
    )
    service = make_service(db_path, adapter)
    outcome = service.run(
        run_key="mf", queries=[q("exact_tag", "a"), q("exact_tag", "empty")], now_iso=T0
    )
    assert outcome.status == "partial"
    assert outcome.executed_requests == 2  # 畸形响应请求也算实际尝试
    assert outcome.unique_candidates == 0
    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"]
        assert n == 0  # 空结果不生成目标/删除/负样本
    finally:
        conn.close()


def test_unique_candidates_capped_at_200(db_path):
    pages = {
        f"exact_tag:t{i}": [make_novel(j) for j in range(1, 31)]
        for i in range(8)
    }
    adapter = FakeDiscoveryAdapter(pages)
    service = make_service(db_path, adapter)
    outcome = service.run(
        run_key="cap",
        queries=[q("exact_tag", f"t{i}") for i in range(8)],
        now_iso=T0,
    )
    assert outcome.executed_requests == 8
    # 30 unique per page × 8 pages；不同页面 novel_id 重叠（1..30 相同集合）
    assert outcome.unique_candidates == 30
    assert outcome.unique_candidates <= MAX_UNIQUE_CANDIDATES


def test_auth_failed_stops(db_path):
    adapter = FakeDiscoveryAdapter(
        {
            "exact_tag:a": [make_novel(1)],
            "novel_new:all": AuthFailed("401"),
        }
    )
    service = make_service(db_path, adapter)
    outcome = service.run(
        run_key="auth",
        queries=[
            q("exact_tag", "a"),
            q("novel_new"),
            q("author_novels", entity_id=5),
        ],
        now_iso=T0,
    )
    assert outcome.status == "partial"
    assert outcome.error_code == "auth_failed"
    # 执行顺序按预算分组：core(exact_tag) → extension(author_novels) →
    # explore(novel_new)；novel_new 命中 AuthFailed 立即停止
    assert outcome.executed_requests == 3  # 包含触发 AuthFailed 的请求
    assert len(adapter.calls) == 3


def test_no_text_calls_anywhere(db_path):
    """Search-1 全链路结构上不触碰 novel_text/novel_detail：计数恒为 0。"""

    class RecordingAdapter(FakeDiscoveryAdapter):
        def __init__(self):
            super().__init__({"exact_tag:a": [make_novel(1)]})
            self.novel_text_calls = 0
            self.novel_detail_calls = 0

        def novel_text(self, novel_id):  # 若被调用即失败可见
            self.novel_text_calls += 1
            raise AssertionError("novel_text must never be called")

        def novel_detail(self, novel_id):
            self.novel_detail_calls += 1
            raise AssertionError("novel_detail must never be called")

    adapter = RecordingAdapter()
    service = make_service(db_path, adapter)
    outcome = service.run(run_key="notext", queries=[q("exact_tag", "a")], now_iso=T0)
    assert outcome.status == "succeeded"
    assert adapter.novel_text_calls == 0
    assert adapter.novel_detail_calls == 0


def test_page_object_cannot_bypass_per_request_limit(db_path):
    class LyingAdapter(FakeDiscoveryAdapter):
        def fetch_page(self, query, cursor):
            self.calls.append((query.query_key, cursor))
            return DiscoveryPage(tuple(make_novel(i) for i in range(100)))

    adapter = LyingAdapter()
    service = make_service(db_path, adapter)
    outcome = service.run(
        run_key="lying-page",
        queries=[DiscoveryQuery("exact_tag:one", "exact_tag", limit=7)],
        now_iso=T0,
    )
    assert outcome.results_seen == 7
    assert outcome.unique_candidates == 7
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"] == 7
    finally:
        conn.close()


def test_candidate_cap_uses_source_priority_not_novel_id(db_path):
    # Eight distinct pages produce 240 distinct IDs.  The final page has the
    # strongest source key and therefore its IDs must survive the 200 cap even
    # though their numeric IDs are larger.
    pages = {}
    queries = []
    for page_no in range(8):
        kind = "exact_tag" if page_no == 0 else "keyword"
        key = make_query_key(kind, f"page-{page_no}")
        queries.append(DiscoveryQuery(key, kind, text=f"page-{page_no}"))
        start = 211 if page_no == 0 else (page_no - 1) * 30 + 1
        pages[key] = [make_novel(i) for i in range(start, start + 30)]
    adapter = FakeDiscoveryAdapter(pages)
    outcome = make_service(db_path, adapter).run(
        run_key="cap-priority", queries=queries, now_iso=T0
    )
    assert outcome.executed_requests == 8
    assert outcome.results_seen == 240
    assert outcome.unique_candidates == 200
    conn = connect(db_path)
    try:
        ids = [row["novel_id"] for row in conn.execute("SELECT novel_id FROM targets")]
        assert 240 in ids
        assert 1 in ids  # numeric ID must not be the selection priority
    finally:
        conn.close()


def test_invalid_query_is_rejected_before_run_or_adapter(db_path):
    adapter = FakeDiscoveryAdapter()
    service = make_service(db_path, adapter)
    invalid = DiscoveryQuery("exact_tag:foo bar", "exact_tag", text="foo bar")
    with pytest.raises(ValueError):
        service.run(run_key="invalid-before-run", queries=[invalid], now_iso=T0)
    assert adapter.calls == []
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM discovery_runs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_persistence_failure_rolls_back_and_finishes_run(db_path):
    class FailingDiscoveryRepository(DiscoveryRepository):
        def insert_sighting(self, *args, **kwargs):
            raise sqlite3.IntegrityError("injected")

    adapter = FakeDiscoveryAdapter({"exact_tag:bad": [make_novel(99)]})
    service = DiscoveryService(
        adapter,
        ScanRepository(),
        FailingDiscoveryRepository(),
        db_path,
    )
    outcome = service.run(
        run_key="persist-fail", queries=[q("exact_tag", "bad")], now_iso=T0
    )
    assert outcome.status == "failed"
    assert outcome.error_code == "persistence_failed"
    conn = connect(db_path)
    try:
        run = conn.execute("SELECT * FROM discovery_runs").fetchone()
        assert run["status"] == "failed"
        assert run["finished_at"] == T0
        assert conn.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM scan_runs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_conflicting_query_key_is_rejected_before_run_or_adapter(db_path):
    adapter = FakeDiscoveryAdapter()
    service = make_service(db_path, adapter)
    conflicting = [
        DiscoveryQuery("exact_tag:stable", "exact_tag", text="stable"),
        DiscoveryQuery("exact_tag:stable", "exact_tag", text="different"),
    ]
    with pytest.raises(ValueError):
        service.run(run_key="conflict", queries=conflicting, now_iso=T0)
    assert adapter.calls == []


def test_repeated_discovery_feeds_existing_state_machine_without_extra_requests(db_path):
    points = [
        ("2026-09-01T00:00:00+00:00", 300, 10),
        ("2026-09-01T01:00:00+00:00", 320, 12),
        ("2026-09-01T02:00:00+00:00", 340, 14),
    ]
    total_calls = 0
    for index, (captured_at, views, bookmarks) in enumerate(points, start=1):
        adapter = FakeDiscoveryAdapter(
            {"exact_tag:ウマ娘": [make_novel(1, total_view=views, total_bookmarks=bookmarks)]}
        )
        outcome = make_service(db_path, adapter).run(
            run_key=f"state-{index}",
            queries=[q("exact_tag", "ウマ娘")],
            now_iso=captured_at,
        )
        assert outcome.status == "succeeded"
        total_calls += len(adapter.calls)

    assert total_calls == 3  # 每轮仍只有原本的一次发现请求
    conn = connect(db_path)
    try:
        state = dict(conn.execute("SELECT * FROM target_states WHERE novel_id=1").fetchone())
        assert state["state"] == "track"
        assert state["observation_count"] == 3
        assert conn.execute("SELECT COUNT(*) FROM target_metrics WHERE novel_id=1").fetchone()[0] == 3
        transition = conn.execute(
            "SELECT from_state, to_state FROM state_transitions WHERE novel_id=1"
        ).fetchone()
        assert dict(transition) == {"from_state": "cold_start", "to_state": "track"}
    finally:
        conn.close()
