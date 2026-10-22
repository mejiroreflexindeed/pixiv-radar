"""启动单回环、单服务的前端构建预览。

默认不初始化演示数据；只有显式传入 --seed 且专用数据库不存在时才运行
既有离线 E demo 种子脚本。预览永远注入空凭据与不可用授权提供者。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.auth.service import (  # noqa: E402
    AuthCapability,
    AuthService,
    UnavailableAuthorizationProvider,
)
from app.core.config import _is_loopback, get_settings  # noqa: E402
from app.core.credentials import InMemoryCredentialStore  # noqa: E402
from app.main import create_app  # noqa: E402

from launch_support import check_loopback_port, run_server  # noqa: E402

# 旧独立探针注入点：保留模块级别名，行为仍是同一个回环端口探测。
_check_port = check_loopback_port

DEFAULT_DB = PROJECT_ROOT / "data" / "local-preview" / "radar.db"
DEFAULT_STATIC_DIR = PROJECT_ROOT / "frontend" / "dist"


def _resolve_workspace_path(value: Path) -> Path:
    resolved = value.resolve(strict=False)
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError("路径必须位于项目工作区内")
    if any(parent.is_symlink() for parent in (resolved, *resolved.parents)):
        raise ValueError("不允许通过符号链接绕过预览路径边界")
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local loopback preview")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--static-dir", type=Path, default=DEFAULT_STATIC_DIR)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="显式初始化一次离线演示数据库")
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
        db_path = _resolve_workspace_path(args.db_path)
        static_dir = _resolve_workspace_path(args.static_dir)
        if not _is_loopback("127.0.0.1"):
            raise ValueError("预览只允许监听回环地址")
        _check_port(args.port)
        if (
            db_path.name.lower() in {"radar.db", "radar_demo.db"}
            and db_path.parent.name.lower() == "state"
        ):
            raise ValueError("预览不得使用 data/state 正式或旧演示数据库")
        # 前置校验：构建产物必须在 seed 之前完成，避免无效构建仍写出演示库
        if not static_dir.is_dir() or not (static_dir / "index.html").is_file():
            raise ValueError(f"构建前端不存在: {static_dir}；请先运行 frontend/npm run build")
        if not (static_dir / "assets").is_dir():
            raise ValueError(f"构建产物缺少 assets 目录: {static_dir / 'assets'}；请重新运行 frontend/npm run build")
        if args.seed:
            if db_path.exists():
                raise ValueError("--seed 只允许用于不存在的专用预览数据库")
            db_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "seed_e_demo.py"), str(db_path)],
                cwd=PROJECT_ROOT,
                check=True,
            )
        os.environ["RADAR_DB_PATH"] = str(db_path)
        os.environ["RADAR_AUTH_ALLOW_REAL"] = "0"
        os.environ["RADAR_DISCOVERY_MODE"] = "demo"
        get_settings.cache_clear()
        auth_service = AuthService(
            InMemoryCredentialStore(),
            UnavailableAuthorizationProvider(),
            # 演示模式是明确能力：不显示"缺少依赖"误导用户。
            capability=lambda: AuthCapability(False, False, "demo_mode"),
        )
        application = create_app(auth_service=auth_service, static_dir=static_dir)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"预览启动失败：{exc}", file=sys.stderr)
        return 2

    print(f"预览准备阶段已通过校验（离线演示数据：{db_path}；静态目录：{static_dir}）")
    print(f"正在启动 http://127.0.0.1:{args.port}（Ctrl+C 停止）；服务就绪后打开浏览器")
    return run_server(application, port=args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
