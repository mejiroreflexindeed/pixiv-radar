"""W07：显式正式源只读备份边界；全部使用隔离 fixture，不触碰真实 data/state。"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from app.storage.backup import BackupError, backup_database
from app.storage.connection import connect, transaction
from app.storage.feedback_repository import FeedbackRepository
from app.storage.migrations import apply_migrations


def _make_source(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.execute("PRAGMA wal_autocheckpoint=0")
    apply_migrations(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO targets(novel_id,title,first_seen_at,last_seen_at)"
            " VALUES(1,'正式源','now','now')"
        )
        FeedbackRepository().insert_event(
            conn, novel_id=1, event_type="good", created_at="2026-09-02T00:00:00+00:00"
        )
    return conn


def _formal_workspace(root: Path) -> Path:
    source = root / "data" / "state" / "radar.db"
    _make_source(source).close()
    return source


def test_default_still_rejects_formal_and_explicit_flag_allows_readonly_copy(tmp_path):
    source = _formal_workspace(tmp_path)
    target = tmp_path / "data" / "backups" / "radar-1.db"
    target.parent.mkdir(parents=True)
    with pytest.raises(BackupError, match="formal_database_not_allowed"):
        backup_database(source, target, allowed_root=tmp_path)
    assert not target.exists()
    result = backup_database(
        source, target, allowed_root=tmp_path, allow_formal_source=True
    )
    assert result["operation"] == "backup"
    conn = sqlite3.connect(target)
    try:
        assert conn.execute(
            "SELECT novel_id,event_type FROM feedback_events"
        ).fetchall() == [(1, "good")]
    finally:
        conn.close()
    # 源库保持 WAL 且未被改写
    conn = sqlite3.connect(source)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT count(*) FROM targets").fetchone()[0] == 1
    finally:
        conn.close()


def test_explicit_flag_still_rejects_demo_arbitrary_state_and_out_of_workspace(tmp_path):
    demo = tmp_path / "data" / "state" / "radar_demo.db"
    _make_source(demo).close()
    with pytest.raises(BackupError, match="formal_database_not_allowed"):
        backup_database(
            demo, tmp_path / "out.db", allowed_root=tmp_path, allow_formal_source=True
        )

    other = tmp_path / "other" / "state" / "radar.db"
    _make_source(other).close()
    with pytest.raises(BackupError, match="formal_database_not_allowed"):
        backup_database(
            other, tmp_path / "out2.db", allowed_root=tmp_path, allow_formal_source=True
        )

    # 开关不能越过 allowed_root：源不在允许工作区内仍被拒绝
    narrow_root = tmp_path / "narrow"
    narrow_root.mkdir()
    with pytest.raises(BackupError, match="path_out_of_scope"):
        backup_database(
            other,
            tmp_path / "out3.db",
            allowed_root=narrow_root,
            allow_formal_source=True,
        )

    # 未提供 allowed_root 时开关不能自行放行
    with pytest.raises(BackupError, match="formal_database_not_allowed"):
        backup_database(other, other.parent / "copy.db", allow_formal_source=True)


def test_explicit_flag_never_allows_formal_destination(tmp_path):
    source = _formal_workspace(tmp_path)
    with pytest.raises(BackupError, match="formal_target_not_allowed"):
        backup_database(
            source,
            source.parent / "copy.db",
            allowed_root=tmp_path,
            allow_formal_source=True,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows 大小写不敏感")
def test_windows_case_variant_of_formal_source_follows_same_rule(tmp_path):
    source = _formal_workspace(tmp_path)
    upper = source.parent / "RADAR.DB"
    source.rename(upper)
    target = tmp_path / "data" / "backups" / "case.db"
    target.parent.mkdir(parents=True)
    result = backup_database(
        upper, target, allowed_root=tmp_path, allow_formal_source=True
    )
    assert result["operation"] == "backup"


def _load_formal_script(name: str):
    script = Path(__file__).resolve().parents[2] / "scripts" / "backup_formal.py"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_formal_script_names_unique_timestamp_and_notes_no_credentials(
    tmp_path, monkeypatch, capsys
):
    source = _formal_workspace(tmp_path)
    module = _load_formal_script("backup_formal_w07")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "DEFAULT_SOURCE", source)
    monkeypatch.setattr(module, "BACKUP_DIR", tmp_path / "data" / "backups")
    assert module.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "succeeded"
    assert "登录" in output["note"]
    first = Path(output["target"])
    assert first.parent == tmp_path / "data" / "backups"
    assert first.name.startswith("radar-")
    # 同一秒内再次备份不得覆盖已有文件
    assert module.main([]) == 0
    second = Path(json.loads(capsys.readouterr().out)["target"])
    assert second != first and second.exists()


def test_unique_target_increments_within_same_second(tmp_path):
    module = _load_formal_script("backup_formal_unique_w07")
    directory = tmp_path / "backups"
    directory.mkdir()
    moment = datetime(2026, 9, 9, 12, 0, 0)
    first = module.unique_target(directory, now=moment)
    assert first.name == "radar-20260909-120000.db"
    first.write_bytes(b"x")
    second = module.unique_target(directory, now=moment)
    assert second.name == "radar-20260909-120000-01.db"
