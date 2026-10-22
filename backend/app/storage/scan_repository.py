"""扫描持久化：targets / target_tags / metric_snapshots / scan_runs 仓库。

所有方法只依赖 sqlite3.Connection；事务边界由调用方（BroadScanService）控制。
"""

from __future__ import annotations

import sqlite3

from ..core.timeutil import normalize_utc_iso
from ..pixiv.adapter import NovelSummary, NovelTag


class ScanRepository:
    def create_scan_run(
        self, conn: sqlite3.Connection, *, layer: str, started_at: str
    ) -> int:
        cur = conn.execute(
            "INSERT INTO scan_runs (layer, status, started_at)"
            " VALUES (?, 'running', ?)",
            (layer, started_at),
        )
        return int(cur.lastrowid)

    def finish_scan_run(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        *,
        status: str,
        finished_at: str,
        error: str | None = None,
        results_seen: int = 0,
        results_written: int = 0,
    ) -> None:
        conn.execute(
            """
            UPDATE scan_runs
               SET status = ?, finished_at = ?, error = ?,
                   results_seen = ?, results_written = ?
             WHERE id = ?
            """,
            (status, finished_at, error, results_seen, results_written, run_id),
        )

    def upsert_target(
        self, conn: sqlite3.Connection, novel: NovelSummary, *, now_iso: str
    ) -> None:
        # 入库边界归一化：不可解析的 create_date 落 NULL，绝不原样写入 UTC 字段
        create_date = normalize_utc_iso(novel.create_date)
        conn.execute(
            """
            INSERT INTO targets (
                novel_id, title, caption, author_id, author_name,
                series_id, series_name, text_length, is_original, create_date,
                x_restrict, visible, novel_ai_type, likes, updated_at,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(novel_id) DO UPDATE SET
                title = excluded.title,
                caption = excluded.caption,
                author_id = excluded.author_id,
                author_name = excluded.author_name,
                series_id = excluded.series_id,
                series_name = excluded.series_name,
                text_length = excluded.text_length,
                is_original = excluded.is_original,
                create_date = excluded.create_date,
                x_restrict = excluded.x_restrict,
                visible = excluded.visible,
                novel_ai_type = excluded.novel_ai_type,
                likes = excluded.likes,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                novel.novel_id,
                novel.title,
                novel.caption,
                novel.author_id,
                novel.author_name,
                novel.series_id,
                novel.series_name,
                novel.text_length,
                int(novel.is_original),
                create_date,
                novel.x_restrict,
                int(novel.visible),
                novel.novel_ai_type,
                novel.likes,
                novel.updated_at,
                now_iso,
                now_iso,
            ),
        )

    def replace_target_tags(
        self,
        conn: sqlite3.Connection,
        novel_id: int,
        tags: tuple[NovelTag, ...],
    ) -> None:
        conn.execute("DELETE FROM target_tags WHERE novel_id = ?", (novel_id,))
        conn.executemany(
            "INSERT INTO target_tags (novel_id, name, translated_name)"
            " VALUES (?, ?, ?)",
            [(novel_id, tag.name, tag.translated_name) for tag in tags],
        )

    def insert_snapshot(
        self,
        conn: sqlite3.Connection,
        novel_id: int,
        run_id: int,
        novel: NovelSummary,
        *,
        captured_at: str,
    ) -> None:
        # OR IGNORE：恢复/重试时同一 (novel_id, run_id) 不产生重复快照
        conn.execute(
            """
            INSERT OR IGNORE INTO metric_snapshots (
                novel_id, scan_run_id, total_bookmarks,
                total_view, total_comments, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                novel_id,
                run_id,
                novel.total_bookmarks,
                novel.total_view,
                novel.total_comments,
                captured_at,
            ),
        )

    # ---- 查询 ----

    def count_targets(self, conn: sqlite3.Connection) -> int:
        return int(
            conn.execute("SELECT COUNT(*) AS n FROM targets").fetchone()["n"]
        )

    def count_targets_in_state(
        self, conn: sqlite3.Connection, state: str
    ) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM target_states WHERE state = ?",
                (state,),
            ).fetchone()["n"]
        )

    def list_targets_with_latest(
        self, conn: sqlite3.Connection, *, limit: int, offset: int
    ) -> list[dict]:
        rows = conn.execute(
            """
            SELECT t.*,
                   s.total_bookmarks, s.total_view, s.total_comments,
                   s.captured_at AS snapshot_at
              FROM targets t
              LEFT JOIN metric_snapshots s
                ON s.novel_id = t.novel_id
               AND s.scan_run_id = (
                     SELECT MAX(scan_run_id)
                       FROM metric_snapshots
                      WHERE novel_id = t.novel_id
                   )
             ORDER BY t.last_seen_at DESC, t.novel_id DESC
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_scan_runs(
        self, conn: sqlite3.Connection, *, limit: int
    ) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_scan_run(self, conn: sqlite3.Connection) -> dict | None:
        row = conn.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
