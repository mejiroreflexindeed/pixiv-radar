"""只读 Dashboard 投影 API（阶段 E）：七次扫描热力图。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from ...core.config import get_settings
from ...storage.connection import connect
from ...storage.phase_c_repository import PhaseCRepository
from ..json_utils import decode_reason_codes

router = APIRouter()

_phase_repo = PhaseCRepository()


@router.get("/dashboard/heatmap")
def heatmap(
    q: str | None = Query(default=None, min_length=1, max_length=100),
    state: Literal["cold_start", "broad", "track"] | None = None,
    result: Literal["qualified", "not_qualified", "unknown"] | None = None,
    min_signal_ratio: float | None = Query(default=None, ge=0, le=100),
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        items = _phase_repo.list_heatmap(
            conn,
            q=q,
            state=state,
            result=result,
            min_signal_ratio=min_signal_ratio,
        )
    finally:
        conn.close()
    return {
        "items": [
            {
                **item,
                "points": [
                    decode_reason_codes(point, "reason_codes")
                    for point in item["points"]
                ],
            }
            for item in items
        ]
    }
