"""正文与轻量特征仓库。

原文仅供本地重算和缓存，API 投影永远只返回特征，不返回正文。
"""

from __future__ import annotations

import json
import sqlite3

from ..content.analyzer import ContentFeatures


class ContentRepository:
    def get_target_state(self, conn: sqlite3.Connection, novel_id: int) -> str | None:
        row = conn.execute(
            """
            SELECT st.state
              FROM targets t
              LEFT JOIN target_states st ON st.novel_id = t.novel_id
             WHERE t.novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["state"] or "cold_start")

    def get_document(self, conn: sqlite3.Connection, novel_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM content_documents WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_analysis(self, conn: sqlite3.Connection, novel_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM content_features WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["is_clutter"] = bool(item["is_clutter"])
        item["is_suspect"] = bool(item["is_suspect"])
        item["keyword_hits"] = json.loads(item.pop("keyword_hits_json"))
        item["reason_codes"] = json.loads(item.pop("reason_codes_json"))
        return item

    def list_similarity_candidates(
        self, conn: sqlite3.Connection, *, exclude_novel_id: int, limit: int
    ) -> list[dict]:
        rows = conn.execute(
            """
            SELECT d.novel_id, t.title, d.body_text,
                   f.structure_score, f.is_clutter, f.is_suspect,
                   f.keyword_hits_json, f.analyzed_at
              FROM content_documents d
              JOIN content_features f ON f.novel_id = d.novel_id
              JOIN targets t ON t.novel_id = d.novel_id
             WHERE d.novel_id <> ?
             ORDER BY f.analyzed_at DESC, d.novel_id DESC
             LIMIT ?
            """,
            (exclude_novel_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: int,
        content_hash: str,
        body_text: str,
        fetched_at: str,
        source_revision: str | None,
        features: ContentFeatures,
        analyzed_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO content_documents
                (novel_id, content_hash, body_text, fetched_at, source_revision)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(novel_id) DO UPDATE SET
                content_hash = excluded.content_hash,
                body_text = excluded.body_text,
                fetched_at = excluded.fetched_at,
                source_revision = excluded.source_revision
            """,
            (novel_id, content_hash, body_text, fetched_at, source_revision),
        )
        conn.execute(
            """
            INSERT INTO content_features
                (novel_id, content_hash, analyzer_version, language, char_count,
                 paragraph_count, dialogue_ratio, image_marker_count,
                 structure_score, is_clutter, is_suspect, keyword_hits_json,
                 reason_codes_json, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(novel_id) DO UPDATE SET
                content_hash = excluded.content_hash,
                analyzer_version = excluded.analyzer_version,
                language = excluded.language,
                char_count = excluded.char_count,
                paragraph_count = excluded.paragraph_count,
                dialogue_ratio = excluded.dialogue_ratio,
                image_marker_count = excluded.image_marker_count,
                structure_score = excluded.structure_score,
                is_clutter = excluded.is_clutter,
                is_suspect = excluded.is_suspect,
                keyword_hits_json = excluded.keyword_hits_json,
                reason_codes_json = excluded.reason_codes_json,
                analyzed_at = excluded.analyzed_at
            """,
            (
                novel_id,
                content_hash,
                features.analyzer_version,
                features.language,
                features.char_count,
                features.paragraph_count,
                features.dialogue_ratio,
                features.image_marker_count,
                features.structure_score,
                int(features.is_clutter),
                int(features.is_suspect),
                json.dumps(features.keyword_hits, ensure_ascii=False, sort_keys=True),
                json.dumps(features.reason_codes, ensure_ascii=False),
                analyzed_at,
            ),
        )
