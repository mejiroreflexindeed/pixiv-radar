"""为离线 E2E fixture 预置 8 条 click 反馈事件（仅任务专用库）。

只用于 H0 夜间端到端的“加载更多”验证；不写正式库、不联网。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main() -> int:
    db_path = Path(sys.argv[1])
    conn = sqlite3.connect(str(db_path))
    try:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM feedback_events WHERE novel_id=1"
        ).fetchone()[0]
        if existing > 0:
            print(f"已存在 {existing} 条事件，跳过预置")
            return 0
        for i in range(8):
            conn.execute(
                "INSERT INTO feedback_events"
                " (novel_id, event_type, scoring_version, weight, created_at)"
                " VALUES (1, 'click', 'feedback-v1', 0.05, ?)",
                (f"2026-09-01T00:00:{i:02d}+00:00",),
            )
        conn.commit()
        print("已预置 8 条 click 事件（novel 1）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
