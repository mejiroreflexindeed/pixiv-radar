"""阶段 F 反馈事件持久化与可解释聚合。"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

EVENT_TYPES = ("good", "exclude", "undo", "exposure", "click", "dwell")
EXPLICIT_TYPES = ("good", "exclude")
SCORING_VERSION = "feedback-v1"
_WEIGHTS = {
    "good": 1.0,
    "exclude": -1.0,
    "undo": 0.0,
    "exposure": 0.0,
    "click": 0.05,
    "dwell": 0.05,
}


class FeedbackError(ValueError):
    """可安全暴露给本地 API 的固定反馈错误。"""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class FeedbackRepository:
    @staticmethod
    def _target_exists(conn: sqlite3.Connection, novel_id: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM targets WHERE novel_id = ?", (novel_id,)
        ).fetchone() is not None

    def insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: int,
        event_type: str,
        created_at: str,
        reverts_event_id: int | None = None,
        scoring_version: str = SCORING_VERSION,
    ) -> dict:
        if event_type not in EVENT_TYPES:
            raise FeedbackError(422, "invalid_feedback_event_type")
        if not isinstance(novel_id, int) or isinstance(novel_id, bool) or novel_id < 1:
            raise FeedbackError(422, "invalid_novel_id")
        if not self._target_exists(conn, novel_id):
            raise FeedbackError(404, "target_not_found")
        if scoring_version != SCORING_VERSION:
            raise FeedbackError(422, "unsupported_feedback_scoring_version")
        if not isinstance(created_at, str) or not created_at:
            raise FeedbackError(422, "invalid_feedback_timestamp")

        if event_type == "undo":
            if (
                not isinstance(reverts_event_id, int)
                or isinstance(reverts_event_id, bool)
                or reverts_event_id < 1
            ):
                raise FeedbackError(422, "undo_target_required")
            target_event = conn.execute(
                "SELECT id, novel_id, event_type FROM feedback_events WHERE id = ?",
                (reverts_event_id,),
            ).fetchone()
            if target_event is None or target_event["novel_id"] != novel_id:
                raise FeedbackError(404, "feedback_event_not_found")
            if target_event["event_type"] not in EXPLICIT_TYPES:
                raise FeedbackError(422, "feedback_event_not_reversible")
            if conn.execute(
                """
                SELECT 1 FROM feedback_events
                 WHERE event_type = 'undo' AND reverts_event_id = ?
                 LIMIT 1
                """,
                (reverts_event_id,),
            ).fetchone() is not None:
                raise FeedbackError(409, "feedback_event_already_reverted")
        elif reverts_event_id is not None:
            raise FeedbackError(422, "unexpected_undo_target")

        cur = conn.execute(
            """
            INSERT INTO feedback_events
                (novel_id, event_type, reverts_event_id,
                 scoring_version, weight, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                novel_id,
                event_type,
                reverts_event_id,
                scoring_version,
                _WEIGHTS[event_type],
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM feedback_events WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        assert row is not None
        return dict(row)

    @staticmethod
    def _active_rows(rows: list[dict]) -> tuple[list[dict], set[int]]:
        reverted = {
            int(row["reverts_event_id"])
            for row in rows
            if row["event_type"] == "undo" and row["reverts_event_id"] is not None
        }
        active = [
            row for row in rows
            if row["event_type"] != "undo" and int(row["id"]) not in reverted
        ]
        return active, reverted

    def list_events(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: int,
        limit: int,
        offset: int,
    ) -> list[dict]:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM feedback_events
                 WHERE novel_id = ? ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (novel_id, limit, offset),
            ).fetchall()
        ]
        all_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, event_type, reverts_event_id"
                " FROM feedback_events WHERE novel_id = ?",
                (novel_id,),
            ).fetchall()
        ]
        _, reverted = self._active_rows(all_rows)
        for row in rows:
            row["is_reverted"] = row["id"] in reverted
        return rows

    def get_summary(self, conn: sqlite3.Connection, *, novel_id: int) -> dict | None:
        if not self._target_exists(conn, novel_id):
            return None
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM feedback_events WHERE novel_id = ? ORDER BY id",
                (novel_id,),
            ).fetchall()
        ]
        active, reverted = self._active_rows(rows)
        explicit = [row for row in active if row["event_type"] in EXPLICIT_TYPES]
        weak = [row for row in active if row["event_type"] in ("click", "dwell")]
        latest = explicit[-1] if explicit else None
        return {
            "novel_id": novel_id,
            "effective_feedback": latest["event_type"] if latest else None,
            "active_feedback_event_id": latest["id"] if latest else None,
            "explicit_score": round(sum(float(r["weight"]) for r in explicit), 6),
            "weak_signal_score": round(sum(float(r["weight"]) for r in weak), 6),
            "score": round(sum(float(r["weight"]) for r in explicit + weak), 6),
            "good_count": sum(r["event_type"] == "good" for r in explicit),
            "exclude_count": sum(r["event_type"] == "exclude" for r in explicit),
            "undo_count": sum(r["event_type"] == "undo" for r in rows),
            "exposure_count": sum(r["event_type"] == "exposure" for r in rows),
            "click_count": sum(r["event_type"] == "click" for r in rows),
            "dwell_count": sum(r["event_type"] == "dwell" for r in rows),
            "scoring_version": rows[-1]["scoring_version"] if rows else SCORING_VERSION,
            "active_event_count": len(active),
            "reverted_event_count": len(reverted),
        }

    def get_summaries_batch(
        self, conn: sqlite3.Connection, *, novel_ids: list[int]
    ) -> dict[int, str | None]:
        """批量返回 effective_feedback（WP2 排序用；单次全量查询）。"""
        if not novel_ids:
            return {}
        placeholders = ",".join("?" for _ in novel_ids)
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM feedback_events WHERE novel_id IN ({placeholders})"
                f" ORDER BY novel_id, id",
                novel_ids,
            ).fetchall()
        ]
        result: dict[int, str | None] = {}
        for novel_id in novel_ids:
            own = [row for row in rows if row["novel_id"] == novel_id]
            if not own:
                result[novel_id] = None
                continue
            active, _ = self._active_rows(own)
            explicit = [
                row for row in active if row["event_type"] in EXPLICIT_TYPES
            ]
            result[novel_id] = explicit[-1]["event_type"] if explicit else None
        return result

    def list_preference_tags(
        self, conn: sqlite3.Connection, *, limit: int
    ) -> list[dict]:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT f.id, f.event_type, f.weight, t.name, t.translated_name
                  FROM feedback_events f
                  JOIN target_tags t ON t.novel_id = f.novel_id
                 WHERE f.event_type IN ('good','exclude','click','dwell')
                 ORDER BY f.id ASC, t.name ASC
                """
            ).fetchall()
        ]
        all_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, event_type, reverts_event_id FROM feedback_events"
            ).fetchall()
        ]
        _, reverted = self._active_rows(all_rows)
        aggregate: dict[str, dict] = defaultdict(
            lambda: {
                "tag": "",
                "translated_name": None,
                "positive_count": 0,
                "negative_count": 0,
                "weak_signal_count": 0,
                "score": 0.0,
            }
        )
        for row in rows:
            if int(row["id"]) in reverted:
                continue
            item = aggregate[row["name"]]
            item["tag"] = row["name"]
            translation = row.get("translated_name")
            if translation and (
                item["translated_name"] is None
                or translation < item["translated_name"]
            ):
                item["translated_name"] = translation
            if row["event_type"] == "good":
                item["positive_count"] += 1
            elif row["event_type"] == "exclude":
                item["negative_count"] += 1
            else:
                item["weak_signal_count"] += 1
            item["score"] += float(row["weight"])
        result = list(aggregate.values())
        for item in result:
            item["score"] = round(item["score"], 6)
        result.sort(
            key=lambda item: (
                -item["score"], -item["positive_count"],
                -item["negative_count"], item["tag"],
            )
        )
        return result[:limit]
