"""A1：health 的运行信息（模式/数据库路径/范围）只读判定单元覆盖。"""

from app.api.routes.health import _db_scope
from app.core.config import PROJECT_ROOT


def test_db_scope_distinguishes_formal_preview_and_custom():
    assert _db_scope(PROJECT_ROOT / "data" / "state" / "radar.db") == "formal"
    assert (
        _db_scope(PROJECT_ROOT / "data" / "local-preview" / "radar.db")
        == "preview"
    )
    assert _db_scope(PROJECT_ROOT / "docs" / "tmp" / "radar.db") == "custom"
    # 不把任意叫 radar.db 的文件误判为正式库
    assert _db_scope(PROJECT_ROOT / "backups" / "radar.db") == "custom"
