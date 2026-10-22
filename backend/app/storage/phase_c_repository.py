"""阶段 C 持久化仓库：run/run item/租约/标签游标/状态/指标/转换/事件。

契约见 docs/PHASE_C_CONTRACT.md §4/§7。所有方法只依赖 sqlite3.Connection，
时间全部由调用方显式传入；不在此层做网络或业务决策。
"""

from __future__ import annotations

import json
import sqlite3

_RUN_ITEM_STATUSES = ("pending", "running", "succeeded", "failed", "unknown")
_TARGET_STATES = ("cold_start", "broad", "track")
_RESULTS = ("qualified", "not_qualified", "unknown")
_UNSET = object()

# 阶段 E 显示层排序白名单：API 参数 → SQL 表达式（绝不拼接用户输入）
_SORT_COLUMNS = {
    "time": "last_seen_at",
    "title": "title",
    "signal": "signal_ratio",
    "views": "total_view",
    "bookmarks": "total_bookmarks",
}
# 可空列排序时 NULL 恒排最后（两个方向一致，稳定可复现）
_NULLS_LAST_SORTS = {"signal", "views", "bookmarks"}

# 显示层 signal_ratio（契约：max(ewma,0)/max(threshold,epsilon)，可空，不写库）
_SIGNAL_RATIO_EXPR = (
    "CASE WHEN m.ewma_view_velocity IS NOT NULL AND m.threshold IS NOT NULL"
    " AND m.threshold > 0"
    " THEN MAX(m.ewma_view_velocity, 0.0) / m.threshold END"
)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PhaseCRepository:
    # ---- scan run（run_key 幂等）----

    def create_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_key: str | None,
        source: str,
        layer: str,
        started_at: str,
    ) -> tuple[int, bool]:
        """返回 (run_id, 是否新建)。重复 run_key 不产生第二个 run。"""
        if run_key:
            row = conn.execute(
                "SELECT id FROM scan_runs WHERE run_key = ?", (run_key,)
            ).fetchone()
            if row is not None:
                return int(row["id"]), False

        cur = conn.execute(
            "INSERT INTO scan_runs (layer, status, source, run_key, started_at)"
            " VALUES (?, 'running', ?, ?, ?)",
            (layer, source, run_key, started_at),
        )
        return int(cur.lastrowid), True

    def get_run(self, conn: sqlite3.Connection, run_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM scan_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def finish_run(
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

    def update_run_progress(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        *,
        results_seen: int,
        results_written: int,
    ) -> None:
        conn.execute(
            "UPDATE scan_runs SET results_seen = ?, results_written = ? WHERE id = ?",
            (results_seen, results_written, run_id),
        )

    # ---- run items（进度与恢复）----

    def upsert_run_item(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        novel_id: int,
        *,
        status: str,
        attempt: int = 0,
        error_code: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        if status not in _RUN_ITEM_STATUSES:
            raise ValueError(f"invalid run item status: {status!r}")
        conn.execute(
            """
            INSERT INTO scan_run_items
                (scan_run_id, novel_id, status, attempt, error_code, completed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_run_id, novel_id) DO UPDATE SET
                status = excluded.status,
                attempt = excluded.attempt,
                error_code = excluded.error_code,
                completed_at = excluded.completed_at
            """,
            (run_id, novel_id, status, attempt, error_code, completed_at),
        )

    def list_run_items(
        self, conn: sqlite3.Connection, run_id: int
    ) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM scan_run_items WHERE scan_run_id = ? ORDER BY novel_id",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 租约 ----

    def acquire(
        self,
        conn: sqlite3.Connection,
        resource: str,
        owner: str,
        *,
        now_iso: str,
        expires_at: str,
    ) -> bool:
        """未过期租约不可抢占；过期后可接管。单条 UPSERT 保证竞态正确。"""
        conn.execute(
            """
            INSERT INTO scan_leases
                (resource, owner, acquired_at, heartbeat_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(resource) DO UPDATE SET
                owner = excluded.owner,
                acquired_at = excluded.acquired_at,
                heartbeat_at = excluded.heartbeat_at,
                expires_at = excluded.expires_at
            WHERE scan_leases.expires_at <= excluded.acquired_at
            """,
            (resource, owner, now_iso, now_iso, expires_at),
        )
        row = conn.execute(
            "SELECT owner FROM scan_leases WHERE resource = ?", (resource,)
        ).fetchone()
        return row is not None and row["owner"] == owner

    def renew(
        self,
        conn: sqlite3.Connection,
        resource: str,
        owner: str,
        *,
        now_iso: str,
        expires_at: str,
    ) -> bool:
        """续租：owner 匹配且未过期。"""
        cur = conn.execute(
            """
            UPDATE scan_leases
               SET heartbeat_at = ?, expires_at = ?
             WHERE resource = ? AND owner = ? AND expires_at > ?
            """,
            (now_iso, expires_at, resource, owner, now_iso),
        )
        return cur.rowcount == 1

    def release(
        self, conn: sqlite3.Connection, resource: str, owner: str
    ) -> bool:
        """释放：owner 匹配（旧 owner 不能释放被接管后的新租约）。"""
        cur = conn.execute(
            "DELETE FROM scan_leases WHERE resource = ? AND owner = ?",
            (resource, owner),
        )
        return cur.rowcount == 1

    def get_lease(
        self, conn: sqlite3.Connection, resource: str
    ) -> dict | None:
        row = conn.execute(
            "SELECT * FROM scan_leases WHERE resource = ?", (resource,)
        ).fetchone()
        return dict(row) if row else None

    # ---- 标签扫描状态 ----

    def upsert_tag_state(
        self,
        conn: sqlite3.Connection,
        tag: str,
        *,
        next_due: str | None | object = _UNSET,
        cooldown_until: str | None | object = _UNSET,
        last_success_at: str | None | object = _UNSET,
        last_error_code: str | None | object = _UNSET,
        cursor: str | None | object = _UNSET,
    ) -> None:
        """只更新调用方明确提供的字段；显式 ``None`` 表示清空。

        省略字段不能覆盖既有游标、cooldown 或最近成功时间。使用动态列集合而
        不是 COALESCE，因为 ``None`` 与“未提供”具有不同语义。
        """
        candidates = {
            "cursor": cursor,
            "next_due": next_due,
            "cooldown_until": cooldown_until,
            "last_success_at": last_success_at,
            "last_error_code": last_error_code,
        }
        updates = {
            key: value for key, value in candidates.items() if value is not _UNSET
        }
        if not updates:
            conn.execute("INSERT OR IGNORE INTO tag_scan_state (tag) VALUES (?)", (tag,))
            return

        columns = list(updates)
        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        update_sql = ", ".join(f"{column} = excluded.{column}" for column in columns)
        conn.execute(
            f"INSERT INTO tag_scan_state (tag, {column_sql}) "
            f"VALUES (?, {placeholders}) "
            f"ON CONFLICT(tag) DO UPDATE SET {update_sql}",
            (tag, *(updates[column] for column in columns)),
        )

    def get_tag_state(self, conn: sqlite3.Connection, tag: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM tag_scan_state WHERE tag = ?", (tag,)
        ).fetchone()
        return dict(row) if row else None

    # ---- 目标状态 ----

    def get_target_state(
        self, conn: sqlite3.Connection, novel_id: int
    ) -> dict | None:
        row = conn.execute(
            "SELECT * FROM target_states WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_target_state(
        self,
        conn: sqlite3.Connection,
        novel_id: int,
        *,
        state: str,
        observation_count: int,
        promotion_streak: int,
        demotion_streak: int,
        last_evaluated_run_id: int | None,
        updated_at: str,
    ) -> None:
        if state not in _TARGET_STATES:
            raise ValueError(f"invalid target state: {state!r}")
        conn.execute(
            """
            INSERT INTO target_states
                (novel_id, state, observation_count, promotion_streak,
                 demotion_streak, last_evaluated_run_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(novel_id) DO UPDATE SET
                state = excluded.state,
                observation_count = excluded.observation_count,
                promotion_streak = excluded.promotion_streak,
                demotion_streak = excluded.demotion_streak,
                last_evaluated_run_id = excluded.last_evaluated_run_id,
                updated_at = excluded.updated_at
            """,
            (
                novel_id,
                state,
                observation_count,
                promotion_streak,
                demotion_streak,
                last_evaluated_run_id,
                updated_at,
            ),
        )

    # ---- 指标 ----

    def insert_metric(
        self,
        conn: sqlite3.Connection,
        novel_id: int,
        scan_run_id: int,
        *,
        exposure_eligible: bool,
        smoothed_bookmark_rate: float | None,
        view_velocity: float | None,
        bookmark_velocity: float | None,
        ewma_view_velocity: float | None,
        threshold: float | None,
        threshold_source: str | None,
        sample_count: int | None,
        result: str,
        reason_codes: list[str],
        created_at: str,
    ) -> None:
        if result not in _RESULTS:
            raise ValueError(f"invalid result: {result!r}")
        conn.execute(
            """
            INSERT INTO target_metrics
                (novel_id, scan_run_id, exposure_eligible, smoothed_bookmark_rate,
                 view_velocity, bookmark_velocity, ewma_view_velocity,
                 threshold, threshold_source, sample_count, result,
                 reason_codes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(novel_id, scan_run_id) DO UPDATE SET
                exposure_eligible = excluded.exposure_eligible,
                smoothed_bookmark_rate = excluded.smoothed_bookmark_rate,
                view_velocity = excluded.view_velocity,
                bookmark_velocity = excluded.bookmark_velocity,
                ewma_view_velocity = excluded.ewma_view_velocity,
                threshold = excluded.threshold,
                threshold_source = excluded.threshold_source,
                sample_count = excluded.sample_count,
                result = excluded.result,
                reason_codes = excluded.reason_codes,
                created_at = excluded.created_at
            """,
            (
                novel_id,
                scan_run_id,
                int(exposure_eligible),
                smoothed_bookmark_rate,
                view_velocity,
                bookmark_velocity,
                ewma_view_velocity,
                threshold,
                threshold_source,
                sample_count,
                result,
                json.dumps(reason_codes, ensure_ascii=False),
                created_at,
            ),
        )

    def get_latest_metric(
        self, conn: sqlite3.Connection, novel_id: int
    ) -> dict | None:
        row = conn.execute(
            """
            SELECT * FROM target_metrics
             WHERE novel_id = ?
             ORDER BY scan_run_id DESC LIMIT 1
            """,
            (novel_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_metrics(
        self, conn: sqlite3.Connection, novel_id: int, *, limit: int
    ) -> list[dict]:
        rows = conn.execute(
            """
            SELECT * FROM target_metrics
             WHERE novel_id = ?
             ORDER BY scan_run_id ASC LIMIT ?
            """,
            (novel_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_run_metrics(
        self, conn: sqlite3.Connection, scan_run_id: int
    ) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM target_metrics WHERE scan_run_id = ? ORDER BY novel_id",
            (scan_run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 状态转换 ----

    def insert_transition(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: int,
        from_state: str,
        to_state: str,
        reason_code: str,
        evidence: dict,
        scan_run_id: int,
        created_at: str,
    ) -> bool:
        """按 dedupe key 幂等；重复插入返回 False。"""
        dedupe_key = f"{novel_id}:{scan_run_id}:{from_state}:{to_state}"
        try:
            conn.execute(
                """
                INSERT INTO state_transitions
                    (novel_id, from_state, to_state, reason_code,
                     evidence_json, scan_run_id, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    novel_id,
                    from_state,
                    to_state,
                    reason_code,
                    json.dumps(evidence, ensure_ascii=False),
                    scan_run_id,
                    dedupe_key,
                    created_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_transitions(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: int | None = None,
        limit: int,
    ) -> list[dict]:
        if novel_id is not None:
            rows = conn.execute(
                "SELECT * FROM state_transitions WHERE novel_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (novel_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM state_transitions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 事件 ----

    def insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_key: str,
        type_: str,
        severity: str,
        payload: dict,
        novel_id: int | None = None,
        scan_run_id: int | None = None,
        created_at: str,
    ) -> bool:
        """按 event_key 幂等；重复插入返回 False。"""
        try:
            conn.execute(
                """
                INSERT INTO events
                    (event_key, type, severity, payload_json,
                     novel_id, scan_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    type_,
                    severity,
                    json.dumps(payload, ensure_ascii=False),
                    novel_id,
                    scan_run_id,
                    created_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_events(
        self, conn: sqlite3.Connection, *, limit: int, offset: int
    ) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 只读查询（WP5）----

    # "最新"按 (captured_at, scan_run_id) 稳定排序，不依赖 MAX(scan_run_id) 的
    # 单进程隐含假设。
    _LATEST_SNAPSHOT = """
        LEFT JOIN metric_snapshots s
          ON s.novel_id = t.novel_id
         AND (s.captured_at, s.scan_run_id) = (
               SELECT s2.captured_at, s2.scan_run_id
                 FROM metric_snapshots s2
                WHERE s2.novel_id = t.novel_id
                ORDER BY s2.captured_at DESC, s2.scan_run_id DESC
                LIMIT 1
             )
    """
    _LATEST_METRIC = """
        LEFT JOIN target_metrics m
          ON m.novel_id = t.novel_id
         AND (m.created_at, m.scan_run_id) = (
               SELECT m2.created_at, m2.scan_run_id
                 FROM target_metrics m2
                WHERE m2.novel_id = t.novel_id
                ORDER BY m2.created_at DESC, m2.scan_run_id DESC
                LIMIT 1
             )
    """

    def list_targets_extended(
        self, conn: sqlite3.Connection, *, limit: int, offset: int
    ) -> list[dict]:
        rows = conn.execute(
            f"""
            SELECT t.*,
                   st.state, st.observation_count, st.promotion_streak,
                   st.demotion_streak,
                   s.total_bookmarks, s.total_view, s.total_comments,
                   s.captured_at AS snapshot_at,
                   m.result AS latest_result,
                   m.ewma_view_velocity AS latest_ewma,
                   m.threshold AS latest_threshold,
                   m.threshold_source AS latest_threshold_source,
                   r.source AS data_source
              FROM targets t
              {self._LATEST_SNAPSHOT}
              LEFT JOIN target_states st ON st.novel_id = t.novel_id
              {self._LATEST_METRIC}
              LEFT JOIN scan_runs r ON r.id = s.scan_run_id
             ORDER BY t.last_seen_at DESC, t.novel_id DESC
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_target_detail(
        self, conn: sqlite3.Connection, novel_id: int
    ) -> dict | None:
        row = conn.execute(
            f"""
            SELECT t.*,
                   st.state, st.observation_count, st.promotion_streak,
                   st.demotion_streak,
                   s.total_bookmarks, s.total_view, s.total_comments,
                   s.captured_at AS snapshot_at,
                   m.result AS latest_result,
                   m.ewma_view_velocity AS latest_ewma,
                   m.threshold AS latest_threshold,
                   m.threshold_source AS latest_threshold_source,
                   m.reason_codes AS latest_reason_codes,
                   r.source AS data_source,
                   {_SIGNAL_RATIO_EXPR} AS signal_ratio
              FROM targets t
              {self._LATEST_SNAPSHOT}
              LEFT JOIN target_states st ON st.novel_id = t.novel_id
              {self._LATEST_METRIC}
              LEFT JOIN scan_runs r ON r.id = s.scan_run_id
             WHERE t.novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_target_history(
        self, conn: sqlite3.Connection, novel_id: int, *, limit: int
    ) -> list[dict]:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT s.novel_id, s.scan_run_id, s.total_bookmarks,
                       s.total_view, s.total_comments, s.captured_at,
                       m.result, m.ewma_view_velocity, m.threshold,
                       m.threshold_source, m.reason_codes
                  FROM metric_snapshots s
                  LEFT JOIN target_metrics m
                    ON m.novel_id = s.novel_id AND m.scan_run_id = s.scan_run_id
                 WHERE s.novel_id = ?
                 ORDER BY s.captured_at DESC, s.scan_run_id DESC
                 LIMIT ?
            ) recent
            ORDER BY captured_at ASC, scan_run_id ASC
            """,
            (novel_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_target_tags(
        self, conn: sqlite3.Connection, novel_id: int
    ) -> list[dict]:
        """作品标签（只读展示用；已有 target_tags 存储的最小映射）。"""
        rows = conn.execute(
            """
            SELECT name, translated_name
              FROM target_tags
             WHERE novel_id = ?
             ORDER BY name ASC
            """,
            (novel_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 阶段 E 只读投影（筛选/排序/热力图；不改 Migration 与阶段 C 数学）----

    def _dashboard_cte(self) -> str:
        return f"""
        WITH base AS (
          SELECT t.*,
                 st.state, st.observation_count, st.promotion_streak,
                 st.demotion_streak,
                 s.total_bookmarks, s.total_view, s.total_comments,
                 s.captured_at AS snapshot_at,
                 m.result AS latest_result,
                 m.ewma_view_velocity AS latest_ewma,
                 m.threshold AS latest_threshold,
                 m.threshold_source AS latest_threshold_source,
                 r.source AS data_source,
                 {_SIGNAL_RATIO_EXPR} AS signal_ratio
            FROM targets t
            {self._LATEST_SNAPSHOT}
            LEFT JOIN target_states st ON st.novel_id = t.novel_id
            {self._LATEST_METRIC}
            LEFT JOIN scan_runs r ON r.id = s.scan_run_id
        )
        """

    @staticmethod
    def _dashboard_filters(
        *,
        q: str | None,
        state: str | None,
        result: str | None,
        min_signal_ratio: float | None,
    ) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if q:
            pattern = f"%{_escape_like(q)}%"
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR author_name LIKE ? ESCAPE '\\')"
            )
            params += [pattern, pattern]
        if state:
            clauses.append("state = ?")
            params.append(state)
        if result:
            clauses.append("latest_result = ?")
            params.append(result)
        if min_signal_ratio is not None:
            clauses.append("signal_ratio >= ?")
            params.append(min_signal_ratio)
        return (" AND ".join(clauses) or "1=1"), params

    def list_targets_extended_filtered(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int,
        offset: int,
        q: str | None = None,
        state: str | None = None,
        result: str | None = None,
        min_signal_ratio: float | None = None,
        sort: str = "time",
        sort_dir: str = "desc",
    ) -> list[dict]:
        """阶段 E：带白名单筛选/排序的扩展目标列表（signal_ratio 为显示层列）。"""
        where, params = self._dashboard_filters(
            q=q, state=state, result=result, min_signal_ratio=min_signal_ratio
        )
        sort_col = _SORT_COLUMNS[sort]
        direction = "DESC" if sort_dir == "desc" else "ASC"
        if sort in _NULLS_LAST_SORTS:
            order = f"({sort_col} IS NULL) ASC, {sort_col} {direction}, novel_id DESC"
        else:
            order = f"{sort_col} {direction}, novel_id DESC"
        rows = conn.execute(
            f"""
            {self._dashboard_cte()}
            SELECT * FROM base
             WHERE {where}
             ORDER BY {order}
             LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        return [dict(row) for row in rows]

    def count_targets_extended(
        self,
        conn: sqlite3.Connection,
        *,
        q: str | None = None,
        state: str | None = None,
        result: str | None = None,
        min_signal_ratio: float | None = None,
    ) -> tuple[int, int]:
        """返回 (筛选后总数, 筛选后 track 数)。"""
        where, params = self._dashboard_filters(
            q=q, state=state, result=result, min_signal_ratio=min_signal_ratio
        )
        total = conn.execute(
            f"{self._dashboard_cte()} SELECT COUNT(*) AS n FROM base WHERE {where}",
            params,
        ).fetchone()["n"]
        track = conn.execute(
            f"{self._dashboard_cte()} SELECT COUNT(*) AS n FROM base"
            f" WHERE {where} AND state = ?",
            params + ["track"],
        ).fetchone()["n"]
        return int(total), int(track)

    def list_heatmap(
        self,
        conn: sqlite3.Connection,
        *,
        q: str | None = None,
        state: str | None = None,
        result: str | None = None,
        min_signal_ratio: float | None = None,
        max_targets: int = 12,
        max_points: int = 7,
    ) -> list[dict]:
        """热力图投影：最多 12 个目标 × 最近 7 次扫描（一次请求，避免前端 N+1）。"""
        where, params = self._dashboard_filters(
            q=q, state=state, result=result, min_signal_ratio=min_signal_ratio
        )
        rows = conn.execute(
            f"""
            {self._dashboard_cte()}
            SELECT novel_id, title, author_name, state
              FROM base
             WHERE {where}
             ORDER BY last_seen_at DESC, novel_id DESC
             LIMIT ?
            """,
            params + [max_targets],
        ).fetchall()
        items: list[dict] = []
        for row in rows:
            points = conn.execute(
                """
                SELECT m.scan_run_id, s.captured_at,
                       m.ewma_view_velocity AS ewma,
                       m.threshold, m.result, m.reason_codes
                  FROM target_metrics m
                  JOIN metric_snapshots s
                    ON s.novel_id = m.novel_id AND s.scan_run_id = m.scan_run_id
                 WHERE m.novel_id = ?
                 ORDER BY s.captured_at DESC, s.scan_run_id DESC
                 LIMIT ?
                """,
                (row["novel_id"], max_points),
            ).fetchall()
            items.append(
                {
                    "novel_id": row["novel_id"],
                    "title": row["title"],
                    "author_name": row["author_name"],
                    "state": row["state"],
                    "points": [dict(p) for p in reversed(points)],
                }
            )
        return items
