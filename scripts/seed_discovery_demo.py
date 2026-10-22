"""Search-1 离线演示/验收种子（fake 来源，data_source=mock）。

用法（项目根目录）：
    .\backend\.venv\Scripts\python.exe scripts\seed_discovery_demo.py [db_path]

默认写入 data/state/radar_discovery_demo.db（本任务专用，验收后可删除）。
fixture：同一 novel 被 exact_tag/novel_new/author_novels 三来源发现；两篇单一
来源冷门新作；一条空结果来源；一条临时失败来源；一个 ratio>2 目标（两轮扫描）。
不构造真实 Pixiv 会话、不联网。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import PROJECT_ROOT  # noqa: E402
from app.discovery import make_query_key  # noqa: E402
from app.discovery.fake import FakeDiscoveryAdapter  # noqa: E402
from app.discovery.service import DiscoveryService  # noqa: E402
from app.discovery.types import DiscoveryQuery  # noqa: E402
from app.domain.scan_coordinator import ScanCoordinator  # noqa: E402
from app.pixiv.adapter import NovelSummary, NovelTag  # noqa: E402
from app.pixiv.exceptions import TemporaryFailure  # noqa: E402
from app.storage.connection import connect  # noqa: E402
from app.storage.discovery_repository import DiscoveryRepository  # noqa: E402
from app.storage.migrations import apply_migrations  # noqa: E402
from app.storage.phase_c_repository import PhaseCRepository  # noqa: E402
from app.storage.scan_repository import ScanRepository  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "state" / "radar_discovery_demo.db"


def make_novel(novel_id, title=None):
    return NovelSummary(
        novel_id=novel_id,
        title=title or f"发现目标 {novel_id}",
        tags=(NovelTag("ウマ娘"),),
        text_length=1000,
        total_bookmarks=5,
        total_view=300,
        total_comments=0,
    )


class RoundAdapter:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def search_novels(self, word, *, limit=30, search_target="partial_match_for_tags", sort="date_desc"):
        item = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        return list(item)

    def novel_detail(self, novel_id):
        raise NotImplementedError

    def novel_text(self, novel_id):
        raise NotImplementedError


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    conn = connect(db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    # 1) Search-1 fake 召回
    adapter = FakeDiscoveryAdapter(
        {
            make_query_key("exact_tag", "ウマ娘"): [make_novel(7001, "三来源小说")],
            make_query_key("novel_new"): [make_novel(7001, "三来源小说"), make_novel(7002, "冷门新作A"), make_novel(7003, "冷门新作B")],
            make_query_key("author_novels", entity_id=9): [make_novel(7001, "三来源小说")],
            make_query_key("exact_tag", "空结果标签"): [],
            make_query_key("keyword", "临时失败"): TemporaryFailure("network"),
        }
    )
    service = DiscoveryService(adapter, ScanRepository(), DiscoveryRepository(), db_path)
    outcome = service.run(
        run_key="discovery-demo-1",
        queries=[
            DiscoveryQuery(make_query_key("exact_tag", "ウマ娘"), "exact_tag", text="ウマ娘"),
            DiscoveryQuery(make_query_key("novel_new"), "novel_new"),
            DiscoveryQuery(make_query_key("author_novels", entity_id=9), "author_novels", entity_id=9),
            DiscoveryQuery(make_query_key("exact_tag", "空结果标签"), "exact_tag", text="空结果标签"),
            DiscoveryQuery(make_query_key("keyword", "临时失败"), "keyword", text="临时失败"),
        ],
    )
    print(f"discovery: status={outcome.status} executed={outcome.executed_requests}"
          f" candidates={outcome.unique_candidates} error={outcome.error_code}")

    # 2) 两轮扫描给 7001 制造 ratio>2 的指标
    rounds = [
        [make_novel(7001, "三来源小说")],
        [NovelSummary(novel_id=7001, title="三来源小说", tags=(NovelTag("ウマ娘"),),
                      text_length=1000, total_bookmarks=60, total_view=700, total_comments=2)],
    ]
    coordinator = ScanCoordinator(
        RoundAdapter(rounds), ScanRepository(), PhaseCRepository(), db_path
    )
    for i, now in enumerate(
        ("2026-09-01T00:00:00+00:00", "2026-09-01T01:00:00+00:00")
    ):
        result = coordinator.run("ウマ娘", run_key=f"scan-{i}", now_iso=now, source="mock")
        print(f"scan {i}: status={result.status}")

    print(f"db={db_path}")
    print("data_source=mock（离线演示数据，非真实 Pixiv）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
