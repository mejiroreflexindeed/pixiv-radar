"""Discovery 持久化仓库（Migration v4 表）。

校验：source_kind 枚举、query_key 字符集与长度、query_text/cursor 长度、
source_rank >= 1、run 状态枚举；错误码不落第三方异常正文。
"""

from __future__ import annotations

import sqlite3

from ..discovery import QUERY_TEXT_MAX, SOURCE_KINDS
from ..discovery.types import validate_query_key as validate_domain_query_key

RUN_STATUSES = ("running", "succeeded", "partial", "failed", "cancelled")

CURSOR_MAX = 500


def validate_source_kind(value: str) -> str:
    if not isinstance(value, str) or value not in SOURCE_KINDS:
        raise ValueError(f"invalid source_kind: {value!r}")
    return value


def validate_query_key(value: str) -> str:
    return validate_domain_query_key(value)


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"value exceeds {limit} chars")
    return value


class DiscoveryRepository:
    # ---- runs ----

    def create_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_key: str,
        started_at: str,
        planned_requests: int = 0,
    ) -> tuple[int, bool]:
        row = conn.execute(
            "SELECT id FROM discovery_runs WHERE run_key = ?", (run_key,)
        ).fetchone()
        if row is not None:
            return int(row["id"]), False
        cur = conn.execute(
            "INSERT INTO discovery_runs (run_key, status, started_at,"
            " planned_requests) VALUES (?, 'running', ?, ?)",
            (run_key, started_at, planned_requests),
        )
        return int(cur.lastrowid), True

    def finish_run(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        *,
        status: str,
        finished_at: str,
        executed_requests: int = 0,
        results_seen: int = 0,
        unique_candidates: int = 0,
        error_code: str | None = None,
    ) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run status: {status!r}")
        conn.execute(
            """
            UPDATE discovery_runs
               SET status = ?, finished_at = ?, executed_requests = ?,
                   results_seen = ?, unique_candidates = ?, error_code = ?
             WHERE id = ?
            """,
            (
                status,
                finished_at,
                executed_requests,
                results_seen,
                unique_candidates,
                error_code,
                run_id,
            ),
        )

    def get_run(self, conn: sqlite3.Connection, run_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM discovery_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_runs(
        self, conn: sqlite3.Connection, *, limit: int, offset: int
    ) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM discovery_runs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- sightings ----

    def insert_sighting(
        self,
        conn: sqlite3.Connection,
        *,
        discovery_run_id: int,
        novel_id: int,
        source_kind: str,
        query_key: str,
        query_text: str | None,
        source_rank: int,
        discovered_at: str,
    ) -> bool:
        """按唯一约束幂等；重复返回 False。"""
        validate_source_kind(source_kind)
        validate_query_key(query_key)
        _bounded(query_text, QUERY_TEXT_MAX)
        if isinstance(source_rank, bool) or not isinstance(source_rank, int) or source_rank < 1:
            raise ValueError("source_rank must be >= 1")
        cur = conn.execute(
            """
            INSERT INTO discovery_sightings
                (discovery_run_id, novel_id, source_kind, query_key,
                 query_text, source_rank, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discovery_run_id, novel_id, source_kind, query_key)
            DO NOTHING
            """,
            (
                discovery_run_id,
                novel_id,
                source_kind,
                query_key,
                query_text,
                source_rank,
                discovered_at,
            ),
        )
        return cur.rowcount == 1

    def list_sightings_for_run(
        self, conn: sqlite3.Connection, run_id: int
    ) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM discovery_sightings WHERE discovery_run_id = ?"
            " ORDER BY novel_id, source_kind, source_rank",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_candidates(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: int,
        limit: int,
        offset: int,
    ) -> list[dict]:
        """候选摘要 + 受控 sightings 数组（同一 novel 多来源一次响应可见）。"""
        ids = [
            row["novel_id"]
            for row in conn.execute(
                "SELECT DISTINCT novel_id FROM discovery_sightings"
                " WHERE discovery_run_id = ?"
                " ORDER BY novel_id LIMIT ? OFFSET ?",
                (run_id, limit, offset),
            ).fetchall()
        ]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        target_rows = conn.execute(
            f"""
            SELECT t.novel_id, t.title, t.author_name,
                   s.total_bookmarks, s.total_view, s.total_comments
              FROM targets t
              LEFT JOIN metric_snapshots s
                ON s.novel_id = t.novel_id
               AND (s.captured_at, s.scan_run_id) = (
                     SELECT s2.captured_at, s2.scan_run_id
                       FROM metric_snapshots s2
                      WHERE s2.novel_id = t.novel_id
                      ORDER BY s2.captured_at DESC, s2.scan_run_id DESC
                      LIMIT 1
                   )
             WHERE t.novel_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        targets = {row["novel_id"]: dict(row) for row in target_rows}
        sighting_rows = conn.execute(
            f"SELECT discovery_run_id, novel_id, source_kind, query_key,"
            f" query_text, source_rank, discovered_at"
            f" FROM discovery_sightings"
            f" WHERE discovery_run_id = ? AND novel_id IN ({placeholders})"
            f" ORDER BY novel_id, source_rank",
            [run_id] + ids,
        ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for row in sighting_rows:
            grouped.setdefault(row["novel_id"], []).append(
                {
                    "source_kind": row["source_kind"],
                    "query_key": row["query_key"],
                    "query_text": row["query_text"],
                    "source_rank": row["source_rank"],
                    "discovered_at": row["discovered_at"],
                }
            )
        items = []
        for novel_id in ids:
            target = targets.get(novel_id, {"novel_id": novel_id})
            items.append(
                {
                    "novel_id": novel_id,
                    "title": target.get("title", ""),
                    "author_name": target.get("author_name", ""),
                    "total_bookmarks": target.get("total_bookmarks", 0),
                    "total_view": target.get("total_view", 0),
                    "total_comments": target.get("total_comments", 0),
                    "sightings": grouped.get(novel_id, []),
                }
            )
        return items

    # ---- 排序输入（WP2，纯读）----

    def list_ranking_inputs(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: int,
        novel_ids: list[int],
    ) -> dict[int, dict]:
        """候选排序输入：visible、快照序列与来源种类（不落库、不计算结论）。"""
        if not novel_ids:
            return {}
        placeholders = ",".join("?" for _ in novel_ids)
        target_rows = conn.execute(
            f"SELECT novel_id, title, visible FROM targets"
            f" WHERE novel_id IN ({placeholders})",
            novel_ids,
        ).fetchall()
        targets = {row["novel_id"]: dict(row) for row in target_rows}
        snapshot_rows = conn.execute(
            f"SELECT novel_id, scan_run_id, total_bookmarks, total_view,"
            f" captured_at FROM metric_snapshots"
            f" WHERE novel_id IN ({placeholders})"
            f" ORDER BY novel_id, captured_at ASC, scan_run_id ASC",
            novel_ids,
        ).fetchall()
        snapshots: dict[int, list[dict]] = {}
        for row in snapshot_rows:
            snapshots.setdefault(row["novel_id"], []).append(
                {
                    "views": row["total_view"],
                    "bookmarks": row["total_bookmarks"],
                    "captured_at": row["captured_at"],
                    "scan_run_id": row["scan_run_id"],
                }
            )
        sighting_rows = conn.execute(
            f"SELECT novel_id, source_kind FROM discovery_sightings"
            f" WHERE discovery_run_id = ? AND novel_id IN ({placeholders})",
            [run_id] + novel_ids,
        ).fetchall()
        sources: dict[int, set[str]] = {}
        for row in sighting_rows:
            sources.setdefault(row["novel_id"], set()).add(row["source_kind"])
        result = {}
        for novel_id in novel_ids:
            target = targets.get(novel_id, {})
            result[novel_id] = {
                "novel_id": novel_id,
                "title": target.get("title", ""),
                "visible": bool(target.get("visible", True)),
                "snapshots": snapshots.get(novel_id, []),
                "source_kinds": sorted(sources.get(novel_id, set())),
            }
        return result

    # ---- query state ----
    def upsert_query_state(
        self,
        conn: sqlite3.Connection,
        *,
        query_key: str,
        source_kind: str,
        cursor: str | None = None,
        next_due: str | None = None,
        cooldown_until: str | None = None,
        last_success_at: str | None = None,
        last_error_code: str | None = None,
    ) -> None:
        validate_source_kind(source_kind)
        validate_query_key(query_key)
        _bounded(cursor, CURSOR_MAX)
        conn.execute(
            """
            INSERT INTO discovery_query_state
                (query_key, source_kind, cursor, next_due, cooldown_until,
                 last_success_at, last_error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_key) DO UPDATE SET
                source_kind = excluded.source_kind,
                cursor = excluded.cursor,
                next_due = excluded.next_due,
                cooldown_until = excluded.cooldown_until,
                last_success_at = excluded.last_success_at,
                last_error_code = excluded.last_error_code
            """,
            (
                query_key,
                source_kind,
                cursor,
                next_due,
                cooldown_until,
                last_success_at,
                last_error_code,
            ),
        )
