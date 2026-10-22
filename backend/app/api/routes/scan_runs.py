"""只读扫描运行状态 API。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...core.config import get_settings
from ...storage.connection import connect
from ...storage.scan_repository import ScanRepository

router = APIRouter()

_repository = ScanRepository()


@router.get("/scan-runs")
def list_scan_runs(
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        items = _repository.list_scan_runs(conn, limit=limit)
    finally:
        conn.close()
    return {"items": items}
