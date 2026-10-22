"""只读事件与状态转换 API（WP5）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Query

from ...core.config import get_settings
from ...storage.connection import connect
from ...storage.phase_c_repository import PhaseCRepository

router = APIRouter()

_repo = PhaseCRepository()


def _replace_json_field(item: dict, raw_field: str, output_field: str) -> dict:
    raw = item.pop(raw_field, None)
    try:
        value = json.loads(raw) if raw is not None else {}
    except (TypeError, ValueError):
        value = {}
    item[output_field] = value if isinstance(value, dict) else {}
    return item


@router.get("/events")
def list_events(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        items = _repo.list_events(conn, limit=limit, offset=offset)
    finally:
        conn.close()
    return {
        "items": [
            _replace_json_field(item, "payload_json", "payload")
            for item in items
        ]
    }


@router.get("/state-transitions")
def list_state_transitions(
    novel_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        items = _repo.list_transitions(conn, novel_id=novel_id, limit=limit)
    finally:
        conn.close()
    return {
        "items": [
            _replace_json_field(item, "evidence_json", "evidence")
            for item in items
        ]
    }
