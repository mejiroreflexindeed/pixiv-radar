from __future__ import annotations

import sqlite3

import pytest

from app.storage.backup import BackupError, backup_database, restore_database
from app.storage.connection import connect, transaction
from app.storage.feedback_repository import FeedbackRepository
from app.storage.migrations import apply_migrations


def _make_source(path):
    conn = connect(path)
    conn.execute("PRAGMA wal_autocheckpoint=100000")
    apply_migrations(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at) VALUES (1, '备份目标', 'now', 'now')"
        )
        conn.execute("INSERT INTO target_tags (novel_id, name) VALUES (1, 'backup-tag')")
        FeedbackRepository().insert_event(
            conn, novel_id=1, event_type="good", created_at="2026-09-02T04:00:00+00:00"
        )
        conn.execute(
            "INSERT INTO content_documents VALUES (1, 'hash-1', '本地正文', 'now', NULL)"
        )
        conn.execute(
            "INSERT INTO content_features VALUES (1, 'hash-1', 'content-v1', 'zh', 4, 1, 0.0, 0, 50.0, 0, 0, '{}', '[]', 'now')"
        )
    return conn


def _event_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT novel_id, event_type, weight FROM feedback_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _content_rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT d.novel_id, d.body_text, f.analyzer_version FROM content_documents d JOIN content_features f USING (novel_id)"
        ).fetchall()
    finally:
        conn.close()


def test_backup_uses_sqlite_api_and_includes_committed_wal(tmp_path):
    source_path = tmp_path / "source" / "radar.db"
    backup_path = tmp_path / "backup.db"
    source = _make_source(source_path)
    try:
        source_hash_before = source_path.read_bytes()
        result = backup_database(source_path, backup_path, allowed_root=tmp_path)
        assert result["operation"] == "backup"
        assert result["schema_version"] == 6
        assert _event_rows(backup_path) == [(1, "good", 1.0)]
        assert _content_rows(backup_path) == [(1, "本地正文", "content-v1")]
        assert source_path.read_bytes() == source_hash_before
    finally:
        source.close()


def test_restore_requires_new_directory_and_preserves_feedback(tmp_path):
    source_path = tmp_path / "source.db"
    backup_path = tmp_path / "backup.db"
    restored_dir = tmp_path / "restored"
    source = _make_source(source_path)
    source.close()
    backup_database(source_path, backup_path, allowed_root=tmp_path)
    result = restore_database(backup_path, restored_dir, allowed_root=tmp_path)
    assert result["operation"] == "restore"
    assert _event_rows(restored_dir / "radar.db") == [(1, "good", 1.0)]
    assert _content_rows(restored_dir / "radar.db") == [(1, "本地正文", "content-v1")]
    with pytest.raises(BackupError, match="restore_directory_already_exists"):
        restore_database(backup_path, restored_dir, allowed_root=tmp_path)


def test_backup_restore_reject_overwrite_same_path_formal_and_out_of_scope(tmp_path):
    source_path = tmp_path / "source.db"
    source = _make_source(source_path)
    source.close()
    with pytest.raises(BackupError, match="same_path_not_allowed"):
        backup_database(source_path, source_path, allowed_root=tmp_path)
    existing_target = tmp_path / "existing.db"
    existing_target.write_bytes(b"occupied")
    with pytest.raises(BackupError, match="target_already_exists"):
        backup_database(source_path, existing_target, allowed_root=tmp_path)
    with pytest.raises(BackupError, match="path_out_of_scope"):
        backup_database(source_path, tmp_path.parent / "outside.db", allowed_root=tmp_path)


def test_invalid_backup_does_not_leave_restore_directory(tmp_path):
    invalid = tmp_path / "invalid.db"
    conn = sqlite3.connect(invalid)
    conn.execute("CREATE TABLE not_radar (id INTEGER)")
    conn.commit()
    conn.close()
    restored_dir = tmp_path / "failed-restore"
    with pytest.raises(BackupError, match="schema_migrations_missing"):
        restore_database(invalid, restored_dir, allowed_root=tmp_path)
    assert not restored_dir.exists()
