"""SQLite 连接与事务测试。"""

import sqlite3

import pytest

from app.storage import migrations as migrations_module
from app.storage.connection import connect, transaction
from app.storage.migrations import Migration, apply_migrations


def test_migrations_are_idempotent(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
        assert first == [1, 2, 3, 4, 5, 6]
        assert second == []
        rows = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in rows] == [1, 2, 3, 4, 5, 6]
    finally:
        conn.close()


def test_pragmas_enabled(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert journal_mode.lower() == "wal"
        assert fk == 1
    finally:
        conn.close()


def test_plain_connection_has_no_implicit_transaction(tmp_path):
    """isolation_level=None：普通连接处于自动提交，DML 不进入隐式事务。"""
    conn = connect(tmp_path / "radar.db")
    try:
        apply_migrations(conn)
        assert conn.in_transaction is False
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('k', 'v')")
        assert conn.in_transaction is False
        other = connect(tmp_path / "radar.db")
        try:
            row = other.execute(
                "SELECT value FROM app_meta WHERE key='k'"
            ).fetchone()
            assert row["value"] == "v"
        finally:
            other.close()
    finally:
        conn.close()


def test_transaction_commits(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        apply_migrations(conn)
        with transaction(conn):
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES ('a', '1')"
            )
            assert conn.in_transaction is True
        assert conn.in_transaction is False
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key='a'"
        ).fetchone()
        assert row["value"] == "1"
    finally:
        conn.close()


def test_transaction_rolls_back(tmp_path):
    conn = connect(tmp_path / "radar.db")
    try:
        apply_migrations(conn)
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute(
                    "INSERT INTO app_meta (key, value) VALUES ('a', '1')"
                )
                assert conn.in_transaction is True
                raise RuntimeError("boom")
        assert conn.in_transaction is False
        row = conn.execute("SELECT COUNT(*) AS n FROM app_meta").fetchone()
        assert row["n"] == 0
    finally:
        conn.close()


def test_migration_failure_rolls_back_all_statements(tmp_path, monkeypatch):
    """多语句迁移中途失败时，已执行的语句与版本记录一并回滚。"""
    bad = Migration(
        version=99,
        name="bad_migration",
        statements=(
            "CREATE TABLE ok_table (id INTEGER PRIMARY KEY)",
            "THIS IS NOT VALID SQL",
        ),
    )
    monkeypatch.setattr(
        migrations_module, "MIGRATIONS", list(migrations_module.MIGRATIONS) + [bad]
    )
    conn = connect(tmp_path / "radar.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn)
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='ok_table'"
        ).fetchone()
        assert table is None
        version = conn.execute(
            "SELECT version FROM schema_migrations WHERE version=99"
        ).fetchone()
        assert version is None
    finally:
        conn.close()
