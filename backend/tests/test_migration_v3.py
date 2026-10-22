"""Migration v3 测试：全新库、v2 升级保数据、中途失败回滚、幂等。"""

import sqlite3

import pytest

from app.storage import migrations as migrations_module
from app.storage.connection import connect
from app.storage.migrations import Migration, apply_migrations, apply_migrations_up_to

V3_TABLES = (
    "scan_run_items",
    "scan_leases",
    "tag_scan_state",
    "target_states",
    "target_metrics",
    "state_transitions",
    "events",
)


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_fresh_db_applies_v1_v2_v3(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
        assert first == [1, 2, 3, 4, 5, 6]
        assert second == []

        tables = _table_names(conn)
        for name in V3_TABLES:
            assert name in tables

        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in rows] == [1, 2, 3, 4, 5, 6]
    finally:
        conn.close()


def test_v2_upgrade_preserves_data(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        assert apply_migrations_up_to(conn, 2) == [1, 2]

        conn.execute(
            "INSERT INTO scan_runs (layer, status, started_at, finished_at,"
            " results_seen, results_written)"
            " VALUES ('broad', 'succeeded', '2026-08-01T00:00:00+00:00',"
            " '2026-08-01T00:00:01+00:00', 1, 1)"
        )
        run_id = conn.execute("SELECT MAX(id) AS m FROM scan_runs").fetchone()["m"]
        conn.execute(
            "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at)"
            " VALUES (7001, '旧数据', '2026-08-01T00:00:00+00:00',"
            " '2026-08-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO metric_snapshots"
            " (novel_id, scan_run_id, total_bookmarks, total_view,"
            "  total_comments, captured_at)"
            " VALUES (7001, ?, 10, 300, 1, '2026-08-01T00:00:00+00:00')",
            (run_id,),
        )

        assert apply_migrations_up_to(conn, 3) == [3]

        target = conn.execute(
            "SELECT title FROM targets WHERE novel_id=7001"
        ).fetchone()
        assert target["title"] == "旧数据"
        snap = conn.execute(
            "SELECT total_bookmarks FROM metric_snapshots WHERE novel_id=7001"
        ).fetchone()
        assert snap["total_bookmarks"] == 10
        run = conn.execute("SELECT * FROM scan_runs WHERE id=?", (run_id,)).fetchone()
        assert run["status"] == "succeeded"
        assert run["run_key"] is None
        assert run["source"] == "unknown"

        tables = _table_names(conn)
        for name in V3_TABLES:
            assert name in tables
    finally:
        conn.close()


def test_v3_mid_failure_rolls_back_everything(tmp_path, monkeypatch):
    bad_v3 = Migration(
        version=3,
        name="bad_v3",
        statements=(
            "CREATE TABLE should_rollback (id INTEGER PRIMARY KEY)",
            "THIS IS NOT VALID SQL",
        ),
    )
    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        list(migrations_module.MIGRATIONS[:2]) + [bad_v3],
    )
    conn = connect(tmp_path / "radar.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn)

        tables = _table_names(conn)
        assert "should_rollback" not in tables
        assert "scan_leases" not in tables
        versions = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
        assert [row["version"] for row in versions] == [1, 2]
    finally:
        conn.close()
