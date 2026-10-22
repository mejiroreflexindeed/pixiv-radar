"""离线演示入口：用 MockPixivAdapter 跑一次广域扫描并写入**独立演示数据库**。

用法（在项目根目录）：
    .\backend\.venv\Scripts\python.exe scripts\run_broad_scan.py

数据写入 data/state/radar_demo.db，不污染正式 data/state/radar.db。
如需用演示数据预览 UI，可临时以 RADAR_DB_PATH 指向演示库启动后端：
    $env:RADAR_DB_PATH = "<项目根>\data\state\radar_demo.db"

不联网、不访问 Pixiv。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import PROJECT_ROOT  # noqa: E402
from app.domain.broad_scan import BroadScanService  # noqa: E402
from app.pixiv.mock import MockPixivAdapter  # noqa: E402
from app.storage.connection import connect  # noqa: E402
from app.storage.migrations import apply_migrations  # noqa: E402
from app.storage.scan_repository import ScanRepository  # noqa: E402

DEMO_DB_PATH = PROJECT_ROOT / "data" / "state" / "radar_demo.db"


def main() -> int:
    conn = connect(DEMO_DB_PATH)
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    service = BroadScanService(
        adapter=MockPixivAdapter(),
        repository=ScanRepository(),
        db_path=DEMO_DB_PATH,
    )
    outcome = service.run("ウマ娘", limit=5)
    print(f"db={DEMO_DB_PATH}")
    print(f"run_id={outcome.run_id} status={outcome.status} "
          f"seen={outcome.results_seen} written={outcome.results_written}")
    if outcome.error:
        print(f"error={outcome.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
