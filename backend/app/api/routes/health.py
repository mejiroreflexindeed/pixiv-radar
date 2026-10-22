"""健康检查路由：只读返回实际进程配置的运行信息。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from ...core.config import PROJECT_ROOT, get_settings

router = APIRouter()

_FORMAL_DB = PROJECT_ROOT / "data" / "state" / "radar.db"
_PREVIEW_DB = PROJECT_ROOT / "data" / "local-preview" / "radar.db"


def _db_scope(db_path: Path) -> str:
    """按进程实际配置的路径区分正式库、演示预览库与自定义库。"""
    resolved = db_path.resolve(strict=False)
    for scope, candidate in (("formal", _FORMAL_DB), ("preview", _PREVIEW_DB)):
        if os.path.normcase(str(resolved)) == os.path.normcase(
            str(candidate.resolve(strict=False))
        ):
            return scope
    return "custom"


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "time": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "mode": settings.discovery_mode,
            "db_path": str(settings.db_path.resolve(strict=False)),
            "db_scope": _db_scope(settings.db_path),
            "schedule_minutes": settings.discovery_interval_minutes,
            "schedule_tag": settings.discovery_tag,
        },
    }
