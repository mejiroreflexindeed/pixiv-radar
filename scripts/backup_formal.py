"""用户手动触发的正式库只读备份入口。

只在用户显式运行 `备份我的数据.cmd` 时调用；夜间/自动任务不得运行本入口。
默认源为项目 `data/state/radar.db`，目标为 `data/backups/<唯一时间戳>.db`。
源库只读打开、不做迁移，备份不含账号登录信息，也不导出系统凭据。
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.storage.backup import BackupError, backup_database

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "data" / "state" / "radar.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"


def unique_target(
    directory: Path, *, now: _datetime.datetime | None = None
) -> Path:
    """同一秒内重复备份也不覆盖：追加递增序号。"""
    stamp = (now or _datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"radar-{stamp}.db"
    suffix = 1
    while candidate.exists():
        candidate = directory / f"radar-{stamp}-{suffix:02d}.db"
        suffix += 1
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读备份正式 radar.db 到 data/backups（不覆盖、不迁移）"
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=None)
    args = parser.parse_args(argv)
    target = args.target if args.target is not None else unique_target(BACKUP_DIR)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        result = backup_database(
            args.source,
            target,
            allowed_root=PROJECT_ROOT,
            allow_formal_source=True,
        )
    except BackupError as exc:
        print(json.dumps({"status": "failed", "code": exc.code}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": "succeeded",
                **result,
                "note": "备份不含账号登录信息；恢复请使用显式命令和新目录",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
