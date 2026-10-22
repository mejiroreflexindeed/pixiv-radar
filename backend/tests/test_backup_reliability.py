"""Regression tests for snapshot sidecars and separate cleanup diagnostics.

Every path belongs to pytest's fresh fixture. No real database is opened.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from app.storage import backup as module
from app.storage.backup import BackupError, backup_database, restore_database
from app.storage.connection import connect, transaction
from app.storage.feedback_repository import FeedbackRepository
from app.storage.migrations import apply_migrations


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source" / "source.db"
    conn = connect(path)
    conn.execute("PRAGMA wal_autocheckpoint=0")
    apply_migrations(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO targets(novel_id,title,first_seen_at,last_seen_at) "
            "VALUES(1,'fixture','now','now')"
        )
        FeedbackRepository().insert_event(
            conn, novel_id=1, event_type="good", created_at="2026-09-02T00:00:00+00:00"
        )
    try:
        yield path, conn
    finally:
        conn.close()


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_published_snapshot_is_single_file_and_keeps_source_wal(tmp_path, source, operation):
    source_path, source_conn = source
    wal = Path(str(source_path) + "-wal")
    assert wal.stat().st_size > 0
    original = source_path.read_bytes(), wal.read_bytes()
    source_names = {p.name for p in source_path.parent.iterdir()}
    target = tmp_path / ("output.db" if operation == "backup" else "restored")
    function = backup_database if operation == "backup" else restore_database
    result = function(source_path, target, allowed_root=tmp_path)
    final = Path(result["target"])
    conn = sqlite3.connect(final)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT novel_id,event_type FROM feedback_events").fetchall() == [(1, "good")]
    finally:
        conn.close()
    assert not list(tmp_path.glob(".*tmp-*"))
    assert all(not Path(str(final) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    if operation == "restore":
        assert {p.name for p in target.iterdir()} == {"radar.db"}
    assert source_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert (source_path.read_bytes(), wal.read_bytes()) == original
    assert {p.name for p in source_path.parent.iterdir()} == source_names


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_late_collision_cleans_only_owned_snapshot_and_sidecars(tmp_path, source, monkeypatch, operation):
    source_path, _ = source
    target = tmp_path / ("output.db" if operation == "backup" else "restored")
    marker = target if operation == "backup" else target / "other-owner.txt"
    original_publish = module._publish
    foreign_sidecars = []

    def collide(temp, final):
        own_db = temp if operation == "backup" else temp / "radar.db"
        if operation == "restore":
            target.mkdir()
        marker.write_bytes(b"foreign final")
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(own_db) + suffix).write_bytes(b"owned temporary sidecar")
            foreign = Path(str(marker) + suffix)
            foreign.write_bytes(b"foreign sidecar")
            foreign_sidecars.append(foreign)
        original_publish(temp, final)

    monkeypatch.setattr(module, "_publish", collide)
    function = backup_database if operation == "backup" else restore_database
    with pytest.raises(BackupError, match="target_already_exists") as caught:
        function(source_path, target, allowed_root=tmp_path)
    assert not caught.value.cleanup_failed
    assert marker.read_bytes() == b"foreign final"
    assert all(path.read_bytes() == b"foreign sidecar" for path in foreign_sidecars)
    assert not list(tmp_path.glob(".*tmp-*"))


@pytest.mark.parametrize("operation", ["backup", "restore"])
@pytest.mark.parametrize("failure", ["validation", "sqlite"])
def test_cli_keeps_primary_code_and_reports_cleanup_path(
    tmp_path, source, monkeypatch, capsys, operation, failure
):
    source_path, _ = source
    target = tmp_path / ("output.db" if operation == "backup" else "restored")
    original_read = module._read_metadata
    reads = 0

    def fail_destination(conn):
        nonlocal reads
        reads += 1
        if reads == 2:
            if failure == "sqlite":
                raise sqlite3.OperationalError("raw-database-error-must-not-be-output")
            raise BackupError("destination_validation_failed")
        return original_read(conn)

    original_unlink = Path.unlink
    original_rmtree = module.shutil.rmtree

    def deny_owned_file(path, *args, **kwargs):
        if path.parent == tmp_path and path.name.startswith(".output.db.tmp-"):
            raise PermissionError("raw-cleanup-error-must-not-be-output")
        return original_unlink(path, *args, **kwargs)

    def deny_owned_dir(path, *args, **kwargs):
        if Path(path).parent == tmp_path and Path(path).name.startswith(".restored.restore-tmp-"):
            raise PermissionError("raw-cleanup-error-must-not-be-output")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(module, "_read_metadata", fail_destination)
    if operation == "backup":
        # Deny only the real main file, not absent optional sidecars.
        def deny_existing(path, *args, **kwargs):
            if path.exists():
                return deny_owned_file(path, *args, **kwargs)
            return original_unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", deny_existing)
    else:
        monkeypatch.setattr(module.shutil, "rmtree", deny_owned_dir)

    script = Path(__file__).resolve().parents[2] / "scripts" / "backup_restore.py"
    spec = importlib.util.spec_from_file_location("backup_cli_regression", script)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main([operation, str(source_path), str(target)]) == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "failed"
    assert result["code"] == ("destination_validation_failed" if failure == "validation" else f"{operation}_failed")
    assert result["cleanup_failed"] is True
    assert len(result["cleanup_paths"]) == 1
    leftover = Path(result["cleanup_paths"][0])
    assert leftover.is_absolute() and leftover.parent == tmp_path and leftover.exists()
    assert leftover.name.startswith(".output.db.tmp-" if operation == "backup" else ".restored.restore-tmp-")
    assert not target.exists()
    assert "raw-" not in output.out
    assert output.err == ""


def test_cleanup_reports_exact_locked_sidecar_and_still_removes_other_owned_files(
    tmp_path, source, monkeypatch
):
    source_path, _ = source
    target = tmp_path / "output.db"
    locked = []

    def fail_publish(temp, final):
        locked.append(Path(str(temp) + "-wal"))
        locked[0].write_bytes(b"locked temporary WAL")
        Path(str(temp) + "-shm").write_bytes(b"temporary SHM")
        raise BackupError("publish_failed")

    unlink = Path.unlink

    def deny_wal(path, *args, **kwargs):
        if path in locked:
            raise PermissionError("locked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(module, "_publish", fail_publish)
    monkeypatch.setattr(Path, "unlink", deny_wal)
    with pytest.raises(BackupError, match="publish_failed") as caught:
        backup_database(source_path, target, allowed_root=tmp_path)
    assert caught.value.cleanup_failed
    assert caught.value.cleanup_paths == [str(locked[0])]
    assert list(tmp_path.glob(".*tmp-*")) == locked
    assert not target.exists()
