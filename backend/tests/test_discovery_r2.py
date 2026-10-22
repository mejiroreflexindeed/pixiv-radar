"""R2 repository integrity boundaries (offline, fake database only)."""

import sqlite3

import pytest

from app.storage.connection import connect
from app.storage.discovery_repository import DiscoveryRepository
from app.storage.migrations import apply_migrations
from app.storage.scan_repository import ScanRepository
from app.pixiv.adapter import NovelSummary


def _db(tmp_path):
    path = tmp_path / "r2-integrity.db"
    conn = connect(path)
    apply_migrations(conn)
    return path


def _seed_target_and_run(conn):
    scan = ScanRepository()
    scan.upsert_target(conn, NovelSummary(novel_id=7, title="n7"), now_iso="2026-09-01T00:00:00+00:00")
    repo = DiscoveryRepository()
    run_id, _ = repo.create_run(
        conn, run_key="r2", started_at="2026-09-01T00:00:00+00:00"
    )
    return repo, run_id


def test_duplicate_sighting_is_only_idempotent_noop(tmp_path):
    conn = connect(_db(tmp_path))
    try:
        repo, run_id = _seed_target_and_run(conn)
        kwargs = dict(
            discovery_run_id=run_id,
            novel_id=7,
            source_kind="exact_tag",
            query_key="exact_tag:r2",
            query_text="r2",
            source_rank=1,
            discovered_at="2026-09-01T00:00:00+00:00",
        )
        assert repo.insert_sighting(conn, **kwargs) is True
        assert repo.insert_sighting(conn, **kwargs) is False
    finally:
        conn.close()


def test_bad_foreign_key_is_not_treated_as_duplicate(tmp_path):
    conn = connect(_db(tmp_path))
    try:
        repo, run_id = _seed_target_and_run(conn)
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_sighting(
                conn,
                discovery_run_id=run_id,
                novel_id=99999,
                source_kind="exact_tag",
                query_key="exact_tag:bad-fk",
                query_text="bad-fk",
                source_rank=1,
                discovered_at="2026-09-01T00:00:00+00:00",
            )
    finally:
        conn.close()


def test_invalid_rank_fails_at_repository_boundary(tmp_path):
    conn = connect(_db(tmp_path))
    try:
        repo, run_id = _seed_target_and_run(conn)
        with pytest.raises(ValueError):
            repo.insert_sighting(
                conn,
                discovery_run_id=run_id,
                novel_id=7,
                source_kind="exact_tag",
                query_key="exact_tag:bad-rank",
                query_text="bad-rank",
                source_rank=True,
                discovered_at="2026-09-01T00:00:00+00:00",
            )
    finally:
        conn.close()
