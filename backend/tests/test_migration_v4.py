"""Migration v4 测试：fresh→v4、v3→v4 数据保留、幂等、中途失败回滚、约束与索引。"""

import sqlite3

import pytest

from app.storage import migrations as migrations_module
from app.storage.connection import connect
from app.storage.migrations import Migration, apply_migrations, apply_migrations_up_to

V4_TABLES = ("discovery_runs", "discovery_sightings", "discovery_query_state")


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


def _index_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {row["name"] for row in rows}


def _seed_v3_data(conn) -> int:
    """写入阶段 C 的 v3 数据，返回 run_id。"""
    conn.execute(
        "INSERT INTO scan_runs (layer, status, source, run_key, started_at,"
        " finished_at, results_seen, results_written)"
        " VALUES ('broad','succeeded','mock','v3-run',"
        " '2026-08-01T00:00:00+00:00','2026-08-01T00:00:01+00:00',1,1)"
    )
    run_id = conn.execute("SELECT MAX(id) AS m FROM scan_runs").fetchone()["m"]
    conn.execute(
        "INSERT INTO targets (novel_id, title, caption, author_id, author_name,"
        " first_seen_at, last_seen_at)"
        " VALUES (9001, '旧数据', 'c', 1, 'a',"
        " '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO metric_snapshots (novel_id, scan_run_id, total_bookmarks,"
        " total_view, total_comments, captured_at)"
        " VALUES (9001, ?, 10, 300, 1, '2026-08-01T00:00:00+00:00')",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO target_metrics (novel_id, scan_run_id, exposure_eligible,"
        " result, reason_codes, created_at)"
        " VALUES (9001, ?, 1, 'unknown', '[]', '2026-08-01T00:00:00+00:00')",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO target_states (novel_id, state, observation_count,"
        " promotion_streak, demotion_streak, updated_at)"
        " VALUES (9001, 'cold_start', 1, 0, 0, '2026-08-01T00:00:00+00:00')"
    )
    return run_id


def test_fresh_db_applies_v1_to_v4(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
        assert first == [1, 2, 3, 4, 5, 6]
        assert second == []
        tables = _table_names(conn)
        for name in V4_TABLES:
            assert name in tables
        versions = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6]
    finally:
        conn.close()


def test_v3_to_v4_preserves_data(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        assert apply_migrations_up_to(conn, 3) == [1, 2, 3]
        run_id = _seed_v3_data(conn)

        assert apply_migrations_up_to(conn, 4) == [4]

        # 旧数据逐表保留
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM scan_runs WHERE id=?", (run_id,)
        ).fetchone()["n"] == 1
        target = conn.execute(
            "SELECT title, caption FROM targets WHERE novel_id=9001"
        ).fetchone()
        assert target["title"] == "旧数据"
        assert target["caption"] == "c"
        assert conn.execute(
            "SELECT total_bookmarks FROM metric_snapshots WHERE novel_id=9001"
        ).fetchone()["total_bookmarks"] == 10
        assert conn.execute(
            "SELECT result FROM target_metrics WHERE novel_id=9001"
        ).fetchone()["result"] == "unknown"
        assert conn.execute(
            "SELECT state FROM target_states WHERE novel_id=9001"
        ).fetchone()["state"] == "cold_start"
        for name in V4_TABLES:
            assert name in _table_names(conn)
    finally:
        conn.close()


def test_v4_constraints_and_indexes(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        apply_migrations(conn)
        indexes = _index_names(conn)
        assert "idx_sightings_novel_time" in indexes
        assert "idx_sightings_source_time" in indexes

        conn.execute(
            "INSERT INTO discovery_runs (run_key, status, started_at)"
            " VALUES ('k1', 'running', '2026-09-01T00:00:00+00:00')"
        )
        run_id = conn.execute("SELECT MAX(id) AS m FROM discovery_runs").fetchone()["m"]

        # status CHECK：非法状态被拒绝
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_runs (run_key, status, started_at)"
                " VALUES ('k2', 'weird', '2026-09-01T00:00:00+00:00')"
            )

        # 外键 + 唯一 + rank CHECK
        conn.execute(
            "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at)"
            " VALUES (9101, 't', '2026-09-01T00:00:00+00:00',"
            " '2026-09-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO discovery_sightings (discovery_run_id, novel_id,"
            " source_kind, query_key, source_rank, discovered_at)"
            " VALUES (?, 9101, 'exact_tag', 'exact_tag:tagA', 1,"
            " '2026-09-01T00:00:00+00:00')",
            (run_id,),
        )
        # 同 (run, novel, source_kind, query_key) 重复 → 唯一约束拒绝
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_sightings (discovery_run_id, novel_id,"
                " source_kind, query_key, source_rank, discovered_at)"
                " VALUES (?, 9101, 'exact_tag', 'exact_tag:tagA', 2,"
                " '2026-09-01T00:00:01+00:00')",
                (run_id,),
            )
        # rank 下限 CHECK
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_sightings (discovery_run_id, novel_id,"
                " source_kind, query_key, source_rank, discovered_at)"
                " VALUES (?, 9101, 'partial_tag', 'partial_tag:tagA', 0,"
                " '2026-09-01T00:00:01+00:00')",
                (run_id,),
            )
        # 外键：sighting 引用不存在的 run → 拒绝
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_sightings (discovery_run_id, novel_id,"
                " source_kind, query_key, source_rank, discovered_at)"
                " VALUES (99999, 9101, 'exact_tag', 'exact_tag:tagX', 1,"
                " '2026-09-01T00:00:01+00:00')"
            )
    finally:
        conn.close()


def test_v4_mid_failure_rolls_back(tmp_path, monkeypatch):
    bad_v4 = Migration(
        version=4,
        name="bad_v4",
        statements=(
            "CREATE TABLE should_rollback_v4 (id INTEGER PRIMARY KEY)",
            "THIS IS NOT VALID SQL",
        ),
    )
    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        list(migrations_module.MIGRATIONS[:3]) + [bad_v4],
    )
    conn = connect(tmp_path / "radar.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn)
        tables = _table_names(conn)
        assert "should_rollback_v4" not in tables
        assert "discovery_runs" not in tables
        versions = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
        assert [row["version"] for row in versions] == [1, 2, 3]
    finally:
        conn.close()
