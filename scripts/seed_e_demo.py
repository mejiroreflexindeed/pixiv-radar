"""阶段 E 离线演示/验收库种子：12 目标 × 7 轮扫描 + 无指标目标。

用法（项目根目录）：
    .\backend\.venv\Scripts\python.exe scripts\seed_e_demo.py [db_path]

默认写入 data/state/radar_e_demo.db（本任务专用，验收后可删除）；
不写正式 radar.db、不联网、不访问 Pixiv。
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

DEFAULT_DB = PROJECT_ROOT / "data" / "state" / "radar_e_demo.db"
TIMES = [
    "2026-08-30T00:00:00+00:00",
    "2026-08-30T01:00:00+00:00",
    "2026-08-30T02:00:00+00:00",
    "2026-08-30T03:00:00+00:00",
    "2026-08-30T04:00:00+00:00",
    "2026-08-30T05:00:00+00:00",
    "2026-08-30T06:00:00+00:00",
]


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
        novel_id=novel_id,
        title=f"novel-{novel_id:02d}",
        author_name=f"author-{novel_id}",
        tags=(NovelTag("ウマ娘"),),
        text_length=1000,
        total_bookmarks=bookmarks,
        total_view=views,
        total_comments=0,
        is_original=True,
        create_date="2026-08-01T00:00:00+00:00",
    )


def _build_rounds():
    rounds = []
    for r in range(7):
        batch = []
        for i in range(1, 13):
            if i <= 3:
                views = 10 + i + r
                bookmarks = 1 + (r if i == 1 else 0)
            elif i == 4:
                views = 5000 + r
                bookmarks = 200
            else:
                views = 300 + i * 10 + r * (20 + i)
                bookmarks = 10 + i + r * 3
            batch.append(make_novel(i, views, bookmarks))
        rounds.append(batch)
    return rounds


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    conn = connect(db_path)
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO targets (novel_id, title, author_name,"
            " first_seen_at, last_seen_at)"
            " VALUES (13, 'novel-13', 'author-13', ?, ?)",
            ("2026-08-30T07:00:00+00:00", "2026-08-30T07:00:00+00:00"),
        )
    finally:
        conn.close()

    coordinator = ScanCoordinator(
        ScriptedAdapter(_build_rounds()),
        ScanRepository(),
        PhaseCRepository(),
        db_path,
    )
    for i, now in enumerate(TIMES):
        outcome = coordinator.run("ウマ娘", run_key=f"e-demo-{i}", now_iso=now, source="mock")
        print(f"run {i}: status={outcome.status} written={outcome.results_written}")
    print(f"db={db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
