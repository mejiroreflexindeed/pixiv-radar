"""极简 SQLite 迁移机制：schema_migrations 表 + 有序迁移列表，幂等执行。

每个迁移是明确的 statements 元组（不做分号拆分），整个迁移在一个
BEGIN IMMEDIATE 事务内执行：语句与版本记录同生共死。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .connection import transaction


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="create_app_meta",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=2,
        name="create_scan_tables",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS scan_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                layer           TEXT NOT NULL,
                status          TEXT NOT NULL,
                started_at      TEXT NOT NULL,
                finished_at     TEXT,
                error           TEXT,
                results_seen    INTEGER NOT NULL DEFAULT 0,
                results_written INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS targets (
                novel_id      INTEGER PRIMARY KEY,
                title         TEXT NOT NULL,
                caption       TEXT NOT NULL DEFAULT '',
                author_id     INTEGER NOT NULL DEFAULT 0,
                author_name   TEXT NOT NULL DEFAULT '',
                series_id     INTEGER,
                series_name   TEXT,
                text_length   INTEGER NOT NULL DEFAULT 0,
                is_original   INTEGER NOT NULL DEFAULT 1,
                create_date   TEXT,
                x_restrict    INTEGER NOT NULL DEFAULT 0,
                visible       INTEGER NOT NULL DEFAULT 1,
                novel_ai_type INTEGER NOT NULL DEFAULT 0,
                likes         INTEGER,
                updated_at    TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS target_tags (
                novel_id        INTEGER NOT NULL
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                name            TEXT NOT NULL,
                translated_name TEXT,
                PRIMARY KEY (novel_id, name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS metric_snapshots (
                novel_id        INTEGER NOT NULL
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                scan_run_id     INTEGER NOT NULL
                    REFERENCES scan_runs(id) ON DELETE CASCADE,
                total_bookmarks INTEGER NOT NULL,
                total_view      INTEGER NOT NULL,
                total_comments  INTEGER NOT NULL,
                captured_at     TEXT NOT NULL,
                PRIMARY KEY (novel_id, scan_run_id)
            )
            """,
        ),
    ),
    Migration(
        version=3,
        name="create_phase_c_tables",
        statements=(
            # v2 兼容升级：run_key 可空（旧数据无 key），source 默认 unknown
            "ALTER TABLE scan_runs ADD COLUMN run_key TEXT",
            "ALTER TABLE scan_runs ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_runs_run_key
                ON scan_runs(run_key) WHERE run_key IS NOT NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS scan_run_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_run_id  INTEGER NOT NULL
                    REFERENCES scan_runs(id) ON DELETE CASCADE,
                novel_id     INTEGER NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                attempt      INTEGER NOT NULL DEFAULT 0,
                error_code   TEXT,
                completed_at TEXT,
                UNIQUE (scan_run_id, novel_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scan_leases (
                resource     TEXT PRIMARY KEY,
                owner        TEXT NOT NULL,
                acquired_at  TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at   TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tag_scan_state (
                tag             TEXT PRIMARY KEY,
                cursor          TEXT,
                next_due        TEXT,
                cooldown_until  TEXT,
                last_success_at TEXT,
                last_error_code TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS target_states (
                novel_id              INTEGER PRIMARY KEY
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                state                 TEXT NOT NULL DEFAULT 'cold_start',
                observation_count     INTEGER NOT NULL DEFAULT 0,
                promotion_streak      INTEGER NOT NULL DEFAULT 0,
                demotion_streak       INTEGER NOT NULL DEFAULT 0,
                last_evaluated_run_id INTEGER,
                updated_at            TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS target_metrics (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id               INTEGER NOT NULL
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                scan_run_id            INTEGER NOT NULL
                    REFERENCES scan_runs(id) ON DELETE CASCADE,
                exposure_eligible      INTEGER NOT NULL,
                smoothed_bookmark_rate REAL,
                view_velocity          REAL,
                bookmark_velocity      REAL,
                ewma_view_velocity     REAL,
                threshold              REAL,
                threshold_source       TEXT,
                sample_count           INTEGER,
                result                 TEXT NOT NULL,
                reason_codes           TEXT NOT NULL,
                created_at             TEXT NOT NULL,
                UNIQUE (novel_id, scan_run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS state_transitions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id      INTEGER NOT NULL
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                from_state    TEXT NOT NULL,
                to_state      TEXT NOT NULL,
                reason_code   TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                scan_run_id   INTEGER NOT NULL
                    REFERENCES scan_runs(id) ON DELETE CASCADE,
                dedupe_key    TEXT NOT NULL UNIQUE,
                created_at    TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key    TEXT NOT NULL UNIQUE,
                type         TEXT NOT NULL,
                severity     TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                novel_id     INTEGER,
                scan_run_id  INTEGER,
                created_at   TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=4,
        name="create_discovery_tables",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS discovery_runs (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                run_key            TEXT NOT NULL UNIQUE,
                status             TEXT NOT NULL
                    CHECK (status IN
                        ('running','succeeded','partial','failed','cancelled')),
                started_at         TEXT NOT NULL,
                finished_at        TEXT,
                planned_requests   INTEGER NOT NULL DEFAULT 0,
                executed_requests  INTEGER NOT NULL DEFAULT 0,
                results_seen       INTEGER NOT NULL DEFAULT 0,
                unique_candidates  INTEGER NOT NULL DEFAULT 0,
                error_code         TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS discovery_sightings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                discovery_run_id INTEGER NOT NULL
                    REFERENCES discovery_runs(id) ON DELETE CASCADE,
                novel_id         INTEGER NOT NULL
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                source_kind      TEXT NOT NULL,
                query_key        TEXT NOT NULL,
                query_text       TEXT,
                source_rank      INTEGER NOT NULL CHECK (source_rank >= 1),
                discovered_at    TEXT NOT NULL,
                UNIQUE (discovery_run_id, novel_id, source_kind, query_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_sightings_novel_time
                ON discovery_sightings (novel_id, discovered_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_sightings_source_time
                ON discovery_sightings (source_kind, query_key, discovered_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS discovery_query_state (
                query_key       TEXT PRIMARY KEY,
                source_kind     TEXT NOT NULL,
                cursor          TEXT,
                next_due        TEXT,
                cooldown_until  TEXT,
                last_success_at TEXT,
                last_error_code TEXT
            )
            """,
        ),
    ),
    Migration(
        version=5,
        name="create_feedback_events",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS feedback_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id         INTEGER NOT NULL
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                event_type       TEXT NOT NULL CHECK (event_type IN
                    ('good','exclude','undo','exposure','click','dwell')),
                reverts_event_id INTEGER
                    REFERENCES feedback_events(id),
                scoring_version  TEXT NOT NULL,
                weight           REAL NOT NULL,
                created_at       TEXT NOT NULL,
                CHECK (
                    (event_type = 'undo' AND reverts_event_id IS NOT NULL)
                    OR (event_type <> 'undo' AND reverts_event_id IS NULL)
                ),
                CHECK (
                    event_type IN ('good','exclude','click','dwell')
                    OR weight = 0.0
                )
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_feedback_events_novel_time
                ON feedback_events (novel_id, id DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_feedback_events_reverts
                ON feedback_events (reverts_event_id)
            """,
        ),
    ),
    Migration(
        version=6,
        name="create_content_analysis",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS content_documents (
                novel_id        INTEGER PRIMARY KEY
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                content_hash    TEXT NOT NULL,
                body_text       TEXT NOT NULL,
                fetched_at      TEXT NOT NULL,
                source_revision TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS content_features (
                novel_id           INTEGER PRIMARY KEY
                    REFERENCES targets(novel_id) ON DELETE CASCADE,
                content_hash       TEXT NOT NULL,
                analyzer_version   TEXT NOT NULL,
                language           TEXT NOT NULL,
                char_count         INTEGER NOT NULL,
                paragraph_count    INTEGER NOT NULL,
                dialogue_ratio     REAL NOT NULL,
                image_marker_count INTEGER NOT NULL,
                structure_score    REAL NOT NULL,
                is_clutter         INTEGER NOT NULL,
                is_suspect         INTEGER NOT NULL,
                keyword_hits_json  TEXT NOT NULL,
                reason_codes_json  TEXT NOT NULL,
                analyzed_at        TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_content_features_flags
                ON content_features (is_clutter, is_suspect, structure_score DESC)
            """,
        ),
    ),
]


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    return _apply_up_to(conn, max_version=None)


def apply_migrations_up_to(
    conn: sqlite3.Connection, max_version: int
) -> list[int]:
    """只应用 version <= max_version 的迁移（用于升级路径测试）。"""
    return _apply_up_to(conn, max_version=max_version)


def _apply_up_to(
    conn: sqlite3.Connection, max_version: int | None
) -> list[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    applied = {
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations")
    }

    new_versions: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        if max_version is not None and migration.version > max_version:
            continue
        with transaction(conn):
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        new_versions.append(migration.version)
    return new_versions
