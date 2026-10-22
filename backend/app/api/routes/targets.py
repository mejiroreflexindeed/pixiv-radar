"""只读目标 API（阶段 E）：分页列表（筛选/排序/显示层 signal_ratio）、详情、历史。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from ...core.config import get_settings
from ...storage.connection import connect
from ...storage.phase_c_repository import PhaseCRepository
from ...storage.scan_repository import ScanRepository
from ..json_utils import decode_reason_codes

router = APIRouter()

_scan_repo = ScanRepository()
_phase_repo = PhaseCRepository()


@router.get("/targets")
def list_targets(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, min_length=1, max_length=100),
    state: Literal["cold_start", "broad", "track"] | None = None,
    result: Literal["qualified", "not_qualified", "unknown"] | None = None,
    min_signal_ratio: float | None = Query(default=None, ge=0, le=100),
    sort: Literal["time", "title", "signal", "views", "bookmarks"] = "time",
    sort_dir: Literal["asc", "desc"] = "desc",
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        items = _phase_repo.list_targets_extended_filtered(
            conn,
            limit=limit,
            offset=offset,
            q=q,
            state=state,
            result=result,
            min_signal_ratio=min_signal_ratio,
            sort=sort,
            sort_dir=sort_dir,
        )
        total, track_total = _phase_repo.count_targets_extended(
            conn,
            q=q,
            state=state,
            result=result,
            min_signal_ratio=min_signal_ratio,
        )
        latest_scan = _scan_repo.get_latest_scan_run(conn)
    finally:
        conn.close()
    return {
        "items": items,
        "total": total,
        "track_total": track_total,
        "limit": limit,
        "offset": offset,
        "latest_scan": latest_scan,
    }


@router.get("/targets/{novel_id}")
def get_target(novel_id: int) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        item = _phase_repo.get_target_detail(conn, novel_id)
        tags = (
            _phase_repo.list_target_tags(conn, novel_id)
            if item is not None
            else []
        )
    finally:
        conn.close()
    if item is None:
        raise HTTPException(status_code=404, detail="target not found")
    item["tags"] = tags
    return decode_reason_codes(item, "latest_reason_codes")


@router.get("/targets/{novel_id}/history")
def get_target_history(
    novel_id: int,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        exists = _phase_repo.get_target_detail(conn, novel_id)
        items = (
            _phase_repo.list_target_history(conn, novel_id, limit=limit)
            if exists is not None
            else None
        )
    finally:
        conn.close()
    if exists is None:
        raise HTTPException(status_code=404, detail="target not found")
    return {
        "items": [
            decode_reason_codes(item, "reason_codes") for item in (items or [])
        ]
    }
