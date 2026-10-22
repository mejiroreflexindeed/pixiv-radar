"""阶段 F 反馈闭环 API：不可变事件、撤销与可解释偏好统计。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, StrictInt

from ...core.config import get_settings
from ...storage.connection import connect, transaction
from ...storage.feedback_repository import FeedbackError, FeedbackRepository

router = APIRouter()
_repo = FeedbackRepository()


class FeedbackRequest(BaseModel):
    novel_id: StrictInt
    event_type: Literal["good", "exclude", "undo", "exposure", "click", "dwell"]
    reverts_event_id: StrictInt | None = None


def _error(exc: FeedbackError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code})


@router.post("/feedback")
def create_feedback(body: FeedbackRequest) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        try:
            with transaction(conn):
                event = _repo.insert_event(
                    conn,
                    novel_id=body.novel_id,
                    event_type=body.event_type,
                    reverts_event_id=body.reverts_event_id,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
        except FeedbackError as exc:
            raise _error(exc) from None
        summary = _repo.get_summary(conn, novel_id=body.novel_id)
    finally:
        conn.close()
    assert summary is not None
    return {"event": event, "summary": summary}


@router.get("/feedback/targets/{novel_id}")
def get_target_feedback(
    novel_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        summary = _repo.get_summary(conn, novel_id=novel_id)
        events = (
            _repo.list_events(conn, novel_id=novel_id, limit=limit, offset=offset)
            if summary is not None else None
        )
    finally:
        conn.close()
    if summary is None:
        raise HTTPException(status_code=404, detail="target not found")
    return {"summary": summary, "events": events or [], "limit": limit, "offset": offset}


@router.get("/feedback/tags")
def get_preference_tags(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        items = _repo.list_preference_tags(conn, limit=limit)
    finally:
        conn.close()
    return {"items": items, "limit": limit}
