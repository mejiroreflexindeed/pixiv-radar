"""启动真实 Pixiv 模式的本地单服务 WebUI。

此入口不会在启动时读取凭据或联网；只有用户在页面中点击登录/运行发现后，才会
使用项目固定的 Windows 凭据项并进行低频只读请求。必须显式传入
``--enable-real``，避免误把 Demo 启动成真实模式。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
DEFAULT_DB = PROJECT_ROOT / "data" / "state" / "radar.db"
DEFAULT_STATIC_DIR = PROJECT_ROOT / "frontend" / "dist"
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from launch_support import check_loopback_port, run_server  # noqa: E402

try:
    from check_readiness import check_readiness  # noqa: E402
except ImportError:  # pragma: no cover - scripts 同目录，正常不会发生
    check_readiness = None  # type: ignore[assignment]


def _workspace_path(value: Path) -> Path:
    resolved = value.resolve(strict=False)
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError("路径必须位于项目工作区内")
    if any(parent.is_symlink() for parent in (resolved, *resolved.parents)):
        raise ValueError("不允许通过符号链接绕过路径边界")
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start local real Pixiv mode")
    parser.add_argument(
        "--enable-real",
        action="store_true",
        help="确认启用用户触发的真实登录与低频只读发现",
    )
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--static-dir", type=Path, default=DEFAULT_STATIC_DIR)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--schedule-minutes",
        type=int,
        default=0,
        help="自动发现周期；0=关闭，启用时必须为 15..1440",
    )
    parser.add_argument(
        "--tag",
        default="ウマ娘",
        help="手动发现默认标签，也是可选自动发现标签",
    )
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument(
        "--open-browser", dest="open_browser", action="store_true", default=True
    )
    browser.add_argument(
        "--no-browser", dest="open_browser", action="store_false"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.enable_real:
            raise ValueError("真实模式必须显式传入 --enable-real")
        if args.schedule_minutes != 0 and not 15 <= args.schedule_minutes <= 1440:
            raise ValueError("--schedule-minutes 必须为 0 或 15..1440")
        tag = args.tag.strip()
        if not 1 <= len(tag) <= 100:
            raise ValueError("--tag 必须包含 1..100 个字符")
        db_path = _workspace_path(args.db_path)
        static_dir = _workspace_path(args.static_dir)
        if db_path.name.lower() == "radar_demo.db":
            raise ValueError("真实模式不得使用离线 radar_demo.db")
        if not static_dir.is_dir() or not (static_dir / "index.html").is_file():
            raise ValueError("前端构建不存在；请先运行 frontend/npm run build")
        if not (static_dir / "assets").is_dir():
            raise ValueError("前端构建缺少 assets 目录")
        check_loopback_port(args.port)

        os.environ["RADAR_DB_PATH"] = str(db_path)
        os.environ["RADAR_DISCOVERY_MODE"] = "real"
        os.environ["RADAR_AUTH_ALLOW_REAL"] = "1"
        os.environ["RADAR_DISCOVERY_INTERVAL_MINUTES"] = str(args.schedule_minutes)
        os.environ["RADAR_DISCOVERY_TAG"] = tag
        sys.path.insert(0, str(BACKEND_DIR))
        from app.core.config import get_settings

        get_settings.cache_clear()
        from app.main import create_app

        application = create_app(static_dir=static_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"真实模式启动失败：{exc}", file=sys.stderr)
        return 2

    # 预检只提示、不阻断：查看数据仍可用，但登录未就绪时不自动打开页面。
    open_browser = args.open_browser
    if check_readiness is not None:
        readiness = check_readiness(
            db_path=db_path, static_dir=static_dir, port=args.port
        )
        if readiness["status"] != "ready":
            open_browser = False
            print(
                f"真实模式预检提示（{readiness['status']}）："
                "登录或自动打开页面暂不可用，修复后重试即可",
                file=sys.stderr,
            )
            for remedy in readiness["remedies"]:
                print(f"  - {remedy}", file=sys.stderr)

    print(f"真实模式准备完成：数据库 {db_path}")
    if args.schedule_minutes:
        print(
            f"正在启动 http://127.0.0.1:{args.port}；"
            "页面操作和已显式启用的低频调度可能触发 Pixiv 请求"
        )
        print(
            f"自动发现已显式启用：标签 {tag}，每 {args.schedule_minutes} 分钟；"
            "首次请求将在一个完整周期后发生"
        )
    else:
        print(
            f"正在启动 http://127.0.0.1:{args.port}；"
            "只有页面中的用户操作会触发登录或 Pixiv 请求"
        )
    return run_server(application, port=args.port, open_browser=open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
