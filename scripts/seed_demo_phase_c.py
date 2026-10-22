"""离线演示/验收种子：用 Mock 适配器 + ScanCoordinator 跑三轮扫描。

用法（项目根目录）：
    .\backend\.venv\Scripts\python.exe scripts\seed_demo_phase_c.py [db_path]

默认写入 data/state/radar_acceptance.db（验收临时库，可安全删除）；
绝不写入正式 data/state/radar.db，不联网、不访问 Pixiv。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import PROJECT_ROOT  # noqa: E402
from app.domain.scan_coordinator import ScanCoordinator  # noqa: E402
from app.pixiv.adapter import NovelSummary, NovelTag  # noqa: E402
from app.storage.connection import connect  # noqa: E402
from app.storage.migrations import apply_migrations  # noqa: E402
from app.storage.phase_c_repository import PhaseCRepository  # noqa: E402
from app.storage.scan_repository import ScanRepository  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "state" / "radar_acceptance.db"


class ScriptedAdapter:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def search_novels(
        self, word, *, limit=30,
        search_target="partial_match_for_tags", sort="date_desc",
    ):
        item = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        return list(item)

    def novel_detail(self, novel_id):
        raise NotImplementedError

    def novel_text(self, novel_id):
        raise NotImplementedError


def make_novel(novel_id, views, bookmarks):
    return NovelSummary(
        novel_id=novel_id, title=f"演示目标 {novel_id}",
        tags=(NovelTag("ウマ娘"),),
        text_length=1000, total_bookmarks=bookmarks, total_view=views,
        total_comments=0, is_original=True,
        create_date="2026-08-01T00:00:00+00:00",
    )


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    rounds = [
        [make_novel(1, 300, 10), make_novel(2, 5000, 200)],
        [make_novel(1, 500, 25), make_novel(2, 5001, 200)],
        [make_novel(1, 700, 40), make_novel(2, 5002, 200)],
    ]
    times = [
        "2026-08-30T00:00:00+00:00",
        "2026-08-30T01:00:00+00:00",
        "2026-08-30T02:00:00+00:00",
    ]
    coordinator = ScanCoordinator(
        ScriptedAdapter(rounds), ScanRepository(), PhaseCRepository(), db_path
    )
    for i, now in enumerate(times):
        outcome = coordinator.run(
            "ウマ娘", run_key=f"demo-{i}", now_iso=now, source="mock"
        )
        print(f"run {i}: status={outcome.status} written={outcome.results_written}")
    print(f"db={db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
