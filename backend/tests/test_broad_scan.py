"""BroadScanService 最小持久化扫描链路测试（全部离线）。"""

import pytest

from app.domain.broad_scan import BroadScanService
from app.pixiv.adapter import NovelSummary, NovelTag
from app.pixiv.exceptions import MalformedResponse, RateLimited
from app.pixiv.mock import MockPixivAdapter
from app.storage.connection import connect
from app.storage.migrations import apply_migrations
from app.storage.scan_repository import ScanRepository


class ErrorAdapter:
    """构造指定异常的适配器，用于失败语义测试。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def search_novels(self, *args, **kwargs):
        raise self._exc

    def novel_detail(self, novel_id):
        raise self._exc

    def novel_text(self, novel_id):
        raise self._exc


class FailingRepository(ScanRepository):
    """在第 N 次快照写入后抛错，用于整体回滚测试。"""

    def __init__(self, fail_after: int):
        super().__init__()
        self._count = 0
        self._fail_after = fail_after

    def insert_snapshot(self, conn, novel_id, run_id, novel, *, captured_at):
        self._count += 1
        if self._count > self._fail_after:
            raise RuntimeError("disk full")
        super().insert_snapshot(
            conn, novel_id, run_id, novel, captured_at=captured_at
        )


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "state" / "radar.db")
    try:
        apply_migrations(conn)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state" / "radar.db"


@pytest.fixture
def service_factory(db_path):
    def make(adapter=None, repository=None):
        return BroadScanService(
            adapter=adapter if adapter is not None else MockPixivAdapter(),
            repository=repository if repository is not None else ScanRepository(),
            db_path=db_path,
        )

    return make


def test_run_twice_is_idempotent(db, service_factory):
    repo = ScanRepository()
    service = service_factory(repository=repo)

    first = service.run("ウマ娘", limit=5)
    second = service.run("ウマ娘", limit=5)

    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert first.results_written == 2  # DEFAULT_FIXTURES 两条
    assert second.results_written == 2

    # targets 不重复
    assert repo.count_targets(db) == 2

    # 快照按 run 分开，无重复 (novel_id, run_id)
    snaps = db.execute(
        "SELECT scan_run_id, novel_id FROM metric_snapshots"
    ).fetchall()
    assert len(snaps) == 4
    pairs = {(row["scan_run_id"], row["novel_id"]) for row in snaps}
    assert len(pairs) == 4

    # 两次 run 各自有终态
    runs = repo.list_scan_runs(db, limit=10)
    assert len(runs) == 2
    assert all(run["status"] == "succeeded" for run in runs)


def test_first_seen_preserved_across_runs(db, service_factory):
    repo = ScanRepository()
    service = service_factory(repository=repo)
    service.run("ウマ娘", limit=5)
    row1 = db.execute(
        "SELECT first_seen_at, last_seen_at FROM targets WHERE novel_id=1001"
    ).fetchone()
    service.run("ウマ娘", limit=5)
    row2 = db.execute(
        "SELECT first_seen_at, last_seen_at FROM targets WHERE novel_id=1001"
    ).fetchone()
    assert row1["first_seen_at"] == row2["first_seen_at"]
    assert row2["last_seen_at"] >= row1["last_seen_at"]


def test_empty_result_is_success_with_zero(db, service_factory):
    repo = ScanRepository()
    service = service_factory(
        adapter=MockPixivAdapter(novels=[]), repository=repo
    )
    outcome = service.run("ウマ娘", limit=5)
    assert outcome.status == "succeeded"
    assert outcome.results_seen == 0
    assert outcome.results_written == 0
    assert repo.count_targets(db) == 0


def test_malformed_response_is_partial(db, service_factory):
    repo = ScanRepository()
    service = service_factory(
        adapter=ErrorAdapter(MalformedResponse("missing novels")),
        repository=repo,
    )
    outcome = service.run("ウマ娘", limit=5)
    assert outcome.status == "partial"
    assert repo.count_targets(db) == 0


def test_rate_limited_is_failed_and_preserves_targets(db, service_factory):
    repo = ScanRepository()
    ok = service_factory(repository=repo)
    ok.run("ウマ娘", limit=1)

    failing = service_factory(
        adapter=ErrorAdapter(RateLimited("rate limited")), repository=repo
    )
    outcome = failing.run("ウマ娘", limit=1)
    assert outcome.status == "failed"

    # 已有目标仍在，快照没有新增，不制造“已删除”
    assert repo.count_targets(db) == 1
    snaps = db.execute(
        "SELECT COUNT(*) AS n FROM metric_snapshots"
    ).fetchone()
    assert snaps["n"] == 1


def test_mid_write_failure_rolls_back_all(db, service_factory):
    repo = FailingRepository(fail_after=1)  # 第二条快照失败
    service = service_factory(repository=repo)
    outcome = service.run("ウマ娘", limit=5)
    assert outcome.status == "failed"

    # 整体回滚：无 targets、无 tags、无快照
    assert db.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"] == 0
    assert (
        db.execute("SELECT COUNT(*) AS n FROM target_tags").fetchone()["n"] == 0
    )
    assert (
        db.execute("SELECT COUNT(*) AS n FROM metric_snapshots").fetchone()["n"]
        == 0
    )

    # run 有失败终态
    run = repo.get_latest_scan_run(db)
    assert run["status"] == "failed"
    assert run["error"]


def test_times_are_iso8601_utc(db, service_factory):
    service = service_factory()
    outcome = service.run("ウマ娘", limit=5)
    assert outcome.started_at.endswith("+00:00")
    assert outcome.finished_at.endswith("+00:00")

    run = db.execute(
        "SELECT started_at, finished_at FROM scan_runs WHERE id=?",
        (outcome.run_id,),
    ).fetchone()
    assert run["started_at"].endswith("+00:00")
    assert run["finished_at"].endswith("+00:00")

    snap = db.execute(
        "SELECT captured_at FROM metric_snapshots LIMIT 1"
    ).fetchone()
    assert snap["captured_at"].endswith("+00:00")

    target = db.execute("SELECT first_seen_at FROM targets LIMIT 1").fetchone()
    assert target["first_seen_at"].endswith("+00:00")


class TokenLeakError(Exception):
    """异常文本携带凭据，验证落库前脱敏。"""

    def __str__(self) -> str:
        return "auth failed: refresh_token=LEAKEDSECRET123 password=LEAKEDPASS456"


def test_failed_run_error_never_contains_token(db, service_factory):
    repo = ScanRepository()
    service = service_factory(adapter=ErrorAdapter(TokenLeakError()), repository=repo)
    outcome = service.run("ウマ娘", limit=5)
    assert outcome.status == "failed"
    assert "LEAKEDSECRET123" not in (outcome.error or "")
    assert "LEAKEDPASS456" not in (outcome.error or "")

    run = repo.get_latest_scan_run(db)
    assert "LEAKEDSECRET123" not in (run["error"] or "")
    assert "LEAKEDPASS456" not in (run["error"] or "")
    assert "***" in (run["error"] or "")


def test_rate_limited_records_cooldown(db, service_factory):
    repo = ScanRepository()
    service = service_factory(
        adapter=ErrorAdapter(
            RateLimited("rate limited", cooldown_seconds=120.0)
        ),
        repository=repo,
    )
    outcome = service.run("ウマ娘", limit=5)
    assert outcome.status == "failed"
    assert outcome.cooldown_seconds == 120.0


def test_unparseable_create_date_not_stored_as_utc(db, service_factory):
    """不可解析的 create_date 在入库层必须是 NULL，不得原样写入 UTC 字段。"""
    novels = [
        NovelSummary(
            novel_id=3001,
            title="bad-date",
            tags=(NovelTag("ウマ娘"),),
            text_length=100,
            total_bookmarks=1,
            total_view=2,
            total_comments=0,
            create_date="not-a-date",
        ),
    ]
    service = service_factory(adapter=MockPixivAdapter(novels=novels))
    outcome = service.run("ウマ娘", limit=5)
    assert outcome.status == "succeeded"

    row = db.execute(
        "SELECT create_date FROM targets WHERE novel_id=3001"
    ).fetchone()
    assert row is not None
    assert row["create_date"] is None
