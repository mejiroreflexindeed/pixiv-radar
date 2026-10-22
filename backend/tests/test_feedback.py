"""阶段 F 反馈闭环：不可变事件、撤销、权重和标签聚合。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3

from app.core.config import get_settings
from app.pixiv.adapter import NovelSummary, NovelTag
from app.storage.connection import connect, transaction
from app.storage import migrations
from app.storage.feedback_repository import FeedbackError, FeedbackRepository
from app.storage.migrations import Migration, apply_migrations, apply_migrations_up_to
from app.storage.scan_repository import ScanRepository


def _seed_target(
    novel_id: int,
    *,
    tags: tuple[NovelTag, ...] = (NovelTag("ウマ娘", "赛马娘"),),
) -> None:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        with transaction(conn):
            ScanRepository().upsert_target(
                conn,
                NovelSummary(
                    novel_id=novel_id,
                    title=f"标题{novel_id}",
                    tags=tags,
                    author_name=f"作者{novel_id}",
                ),
                now_iso="2026-09-02T04:00:00+00:00",
            )
            ScanRepository().replace_target_tags(conn, novel_id, tags)
    finally:
        conn.close()


def test_good_exclude_undo_is_append_only_and_replayable(client):
    _seed_target(1)

    good = client.post("/api/feedback", json={"novel_id": 1, "event_type": "good"})
    assert good.status_code == 200
    good_event = good.json()["event"]
    assert good_event["event_type"] == "good"
    assert good_event["weight"] == 1.0
    assert good_event["scoring_version"] == "feedback-v1"

    first = client.get("/api/feedback/targets/1")
    assert first.status_code == 200
    assert first.json()["summary"]["effective_feedback"] == "good"
    assert first.json()["summary"]["explicit_score"] == 1.0

    undo = client.post(
        "/api/feedback",
        json={
            "novel_id": 1,
            "event_type": "undo",
            "reverts_event_id": good_event["id"],
        },
    )
    assert undo.status_code == 200
    summary = undo.json()["summary"]
    assert summary["effective_feedback"] is None
    assert summary["explicit_score"] == 0.0
    assert summary["undo_count"] == 1
    assert summary["reverted_event_count"] == 1

    events = client.get("/api/feedback/targets/1").json()["events"]
    by_id = {item["id"]: item for item in events}
    assert by_id[good_event["id"]]["is_reverted"] is True
    assert all("refresh_token" not in item for item in events)

    exclude = client.post(
        "/api/feedback", json={"novel_id": 1, "event_type": "exclude"}
    )
    assert exclude.status_code == 200
    assert exclude.json()["summary"]["effective_feedback"] == "exclude"
    assert exclude.json()["summary"]["explicit_score"] == -1.0

    duplicate_undo = client.post(
        "/api/feedback",
        json={
            "novel_id": 1,
            "event_type": "undo",
            "reverts_event_id": good_event["id"],
        },
    )
    assert duplicate_undo.status_code == 409
    assert duplicate_undo.json() == {
        "detail": {"code": "feedback_event_already_reverted"}
    }


def test_exposure_and_weak_signals_are_explicit_and_bounded(client):
    _seed_target(2)
    assert client.post(
        "/api/feedback", json={"novel_id": 2, "event_type": "exposure"}
    ).status_code == 200
    assert client.post(
        "/api/feedback", json={"novel_id": 2, "event_type": "click"}
    ).status_code == 200
    summary = client.get("/api/feedback/targets/2").json()["summary"]
    assert summary["exposure_count"] == 1
    assert summary["click_count"] == 1
    assert summary["explicit_score"] == 0.0
    assert summary["weak_signal_score"] == 0.05
    assert summary["score"] == 0.05


def test_feedback_rejects_unknown_target_and_bad_undo(client):
    missing = client.post(
        "/api/feedback", json={"novel_id": 999, "event_type": "good"}
    )
    assert missing.status_code == 404
    assert missing.json() == {"detail": {"code": "target_not_found"}}

    _seed_target(3)
    bad_undo = client.post(
        "/api/feedback", json={"novel_id": 3, "event_type": "undo"}
    )
    assert bad_undo.status_code == 422
    assert bad_undo.json() == {"detail": {"code": "undo_target_required"}}

    invalid = client.post(
        "/api/feedback", json={"novel_id": 3, "event_type": "nope"}
    )
    assert invalid.status_code == 422

    strict_id = client.post(
        "/api/feedback", json={"novel_id": True, "event_type": "good"}
    )
    assert strict_id.status_code == 422


def test_preference_tags_replay_active_events_deterministically(client):
    _seed_target(10, tags=(NovelTag("alpha", "阿尔法"), NovelTag("shared", "共享")))
    _seed_target(11, tags=(NovelTag("beta", "贝塔"), NovelTag("shared", "共享")))
    first = client.post(
        "/api/feedback", json={"novel_id": 10, "event_type": "good"}
    )
    second = client.post(
        "/api/feedback", json={"novel_id": 11, "event_type": "exclude"}
    )
    assert first.status_code == 200 and second.status_code == 200

    tags = client.get("/api/feedback/tags?limit=100")
    assert tags.status_code == 200
    items = tags.json()["items"]
    assert [item["tag"] for item in items] == ["alpha", "shared", "beta"]
    assert items[0]["positive_count"] == 1
    assert items[1]["negative_count"] == 1
    assert items[1]["score"] == 0.0
    assert items[2]["score"] == -1.0
    assert all("payload_json" not in item for item in items)

    undo = client.post(
        "/api/feedback",
        json={
            "novel_id": 10,
            "event_type": "undo",
            "reverts_event_id": first.json()["event"]["id"],
        },
    )
    assert undo.status_code == 200
    items = client.get("/api/feedback/tags?limit=100").json()["items"]
    assert [item["tag"] for item in items] == ["beta", "shared"]


def test_feedback_chain_restores_previous_explicit_state_and_rejects_cross_target_undo(client):
    _seed_target(20)
    _seed_target(21)
    good = client.post("/api/feedback", json={"novel_id": 20, "event_type": "good"}).json()["event"]
    exclude = client.post("/api/feedback", json={"novel_id": 20, "event_type": "exclude"}).json()["event"]
    restored = client.post(
        "/api/feedback",
        json={"novel_id": 20, "event_type": "undo", "reverts_event_id": exclude["id"]},
    )
    assert restored.status_code == 200
    assert restored.json()["summary"]["effective_feedback"] == "good"
    assert restored.json()["summary"]["explicit_score"] == 1.0

    cross_target = client.post(
        "/api/feedback",
        json={"novel_id": 21, "event_type": "undo", "reverts_event_id": good["id"]},
    )
    assert cross_target.status_code == 404
    assert cross_target.json() == {"detail": {"code": "feedback_event_not_found"}}


def test_latest_duplicate_good_undo_reactivates_earlier_good_and_weak_is_not_reversible(client):
    _seed_target(22)
    first = client.post("/api/feedback", json={"novel_id": 22, "event_type": "good"}).json()["event"]
    latest = client.post("/api/feedback", json={"novel_id": 22, "event_type": "good"}).json()["event"]
    undone = client.post(
        "/api/feedback",
        json={"novel_id": 22, "event_type": "undo", "reverts_event_id": latest["id"]},
    )
    assert undone.status_code == 200
    assert undone.json()["summary"]["active_feedback_event_id"] == first["id"]
    click = client.post("/api/feedback", json={"novel_id": 22, "event_type": "click"}).json()["event"]
    weak_undo = client.post(
        "/api/feedback",
        json={"novel_id": 22, "event_type": "undo", "reverts_event_id": click["id"]},
    )
    assert weak_undo.status_code == 422
    assert weak_undo.json() == {"detail": {"code": "feedback_event_not_reversible"}}


def test_feedback_audit_pagination_has_no_duplicate_boundary(client):
    _seed_target(23)
    for _ in range(6):
        assert client.post(
            "/api/feedback", json={"novel_id": 23, "event_type": "exposure"}
        ).status_code == 200
    first = client.get("/api/feedback/targets/23?limit=5&offset=0").json()["events"]
    second = client.get("/api/feedback/targets/23?limit=5&offset=5").json()["events"]
    assert len(first) == 5 and len(second) == 1
    assert {item["id"] for item in first}.isdisjoint({item["id"] for item in second})


def test_concurrent_undo_is_serialized_to_one_success(tmp_path):
    db_path = tmp_path / "state" / "concurrent.db"
    conn = connect(db_path)
    try:
        apply_migrations(conn)
        with transaction(conn):
            conn.execute(
                "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at) VALUES (30, '并发', 'now', 'now')"
            )
            good = FeedbackRepository().insert_event(
                conn, novel_id=30, event_type="good", created_at="2026-09-02T04:00:00+00:00"
            )
    finally:
        conn.close()

    def undo_once() -> str:
        worker_conn = connect(db_path)
        try:
            with transaction(worker_conn):
                FeedbackRepository().insert_event(
                    worker_conn,
                    novel_id=30,
                    event_type="undo",
                    reverts_event_id=good["id"],
                    created_at="2026-09-02T04:00:01+00:00",
                )
            return "ok"
        except FeedbackError as exc:
            return exc.code
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: undo_once(), range(2)))
    assert sorted(results) == ["feedback_event_already_reverted", "ok"]


def _table_digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    payload = repr([tuple(row) for row in rows]).encode()
    return len(rows), hashlib.sha256(payload).hexdigest()


def test_v4_upgrade_only_adds_v5_and_preserves_existing_tables(tmp_path):
    conn = connect(tmp_path / "state" / "v4.db")
    try:
        assert apply_migrations_up_to(conn, 4) == [1, 2, 3, 4]
        with transaction(conn):
            conn.execute(
                "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at) VALUES (40, 'v4', 'now', 'now')"
            )
            conn.execute("INSERT INTO target_tags (novel_id, name) VALUES (40, 'tag')")
            conn.execute("INSERT INTO scan_runs (layer, status, started_at) VALUES ('broad', 'succeeded', 'now')")
            conn.execute(
                "INSERT INTO metric_snapshots (novel_id, scan_run_id, total_bookmarks, total_view, total_comments, captured_at) VALUES (40, 1, 1, 2, 3, 'now')"
            )
            conn.execute(
                "INSERT INTO discovery_runs (run_key, status, started_at) VALUES ('v4-run', 'succeeded', 'now')"
            )
            conn.execute(
                "INSERT INTO discovery_sightings (discovery_run_id, novel_id, source_kind, query_key, source_rank, discovered_at) VALUES (1, 40, 'exact_tag', 'tag', 1, 'now')"
            )
        before = {table: _table_digest(conn, table) for table in (
            "targets", "target_tags", "scan_runs", "metric_snapshots", "discovery_runs", "discovery_sightings"
        )}
        assert apply_migrations(conn) == [5, 6]
        after = {table: _table_digest(conn, table) for table in before}
        assert after == before
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='feedback_events'").fetchone()
    finally:
        conn.close()


def test_failed_v5_migration_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    conn = connect(tmp_path / "state" / "retry.db")
    try:
        apply_migrations_up_to(conn, 4)
        broken = Migration(
            version=5,
            name="broken_feedback",
            statements=(
                "CREATE TABLE feedback_events (id INTEGER PRIMARY KEY)",
                "THIS IS NOT SQL",
            ),
        )
        monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS[:4], broken])
        try:
            apply_migrations(conn)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("broken migration unexpectedly succeeded")
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 4
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='feedback_events'").fetchone() is None
        monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS[:4], Migration(
            version=5,
            name="create_feedback_events_retry",
            statements=("CREATE TABLE feedback_events (id INTEGER PRIMARY KEY)",),
        )])
        assert apply_migrations(conn) == [5]
    finally:
        conn.close()
