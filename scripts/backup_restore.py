"""离线 SQLite 备份/恢复 CLI；所有路径必须显式传入。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.storage.backup import BackupError, backup_database, restore_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pixiv Radar SQLite backup/restore")
    sub = parser.add_subparsers(dest="operation", required=True)

    backup = sub.add_parser("backup", help="通过 SQLite backup API 备份数据库")
    backup.add_argument("source", type=Path)
    backup.add_argument("target", type=Path)
    backup.add_argument(
        "--allow-formal-source",
        action="store_true",
        help="仅允许工作区内 data/state/radar.db 作为只读源；不放行 demo 库或正式目的地",
    )

    restore = sub.add_parser("restore", help="恢复到一个不存在的新目录")
    restore.add_argument("backup", type=Path)
    restore.add_argument("target_dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "backup":
            result = backup_database(
                args.source,
                args.target,
                allowed_root=PROJECT_ROOT,
                allow_formal_source=args.allow_formal_source,
            )
        else:
            result = restore_database(args.backup, args.target_dir, allowed_root=PROJECT_ROOT)
    except BackupError as exc:
        result = {"status": "failed", "code": exc.code}
        if exc.cleanup_failed:
            result.update(cleanup_failed=True, cleanup_paths=exc.cleanup_paths)
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "succeeded", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
