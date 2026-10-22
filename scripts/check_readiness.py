"""离线就绪检查：不读凭据、不联网、不创建数据库。

状态优先级：missing_dependency > missing_build > missing_browser >
port_in_use > ready。输出无凭据 JSON 与工作区内可执行修复说明。
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
DEFAULT_STATIC_DIR = PROJECT_ROOT / "frontend" / "dist"
DEFAULT_DB = PROJECT_ROOT / "data" / "state" / "radar.db"
RUNTIME_BROWSERS = PROJECT_ROOT / ".runtime" / "playwright"

REQUIRED_PACKAGES = ("fastapi", "uvicorn", "pydantic-settings", "keyring", "pixivpy3")


def _package_status() -> dict[str, str | None]:
    status: dict[str, str | None] = {}
    for name in REQUIRED_PACKAGES:
        try:
            status[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            status[name] = None
    return status


def _chromium_executable() -> str | None:
    base = RUNTIME_BROWSERS
    if not base.is_dir():
        return None
    for revision in sorted(base.glob("chromium-*"), reverse=True):
        candidate = revision / "chrome-win64" / "chrome.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def check_readiness(
    *,
    db_path: Path | None = None,
    static_dir: Path | None = None,
    port: int = 8000,
    require_chromium: bool = True,
) -> dict:
    """执行离线检查；不读取任何凭据，不访问网络。"""
    static = (static_dir or DEFAULT_STATIC_DIR).resolve(strict=False)
    db = (db_path or DEFAULT_DB).resolve(strict=False)

    packages = _package_status()
    missing = [name for name, version in packages.items() if version is None]
    chromium = _chromium_executable()
    index_ok = static.is_dir() and (static / "index.html").is_file()
    assets_ok = (static / "assets").is_dir()
    port_free = _port_free(port)
    db_parent = db.parent
    # 新克隆的源码不含 data/：允许应用在启动时建立目录，但拒绝父路径中已有文件。
    db_parent_ok = all(
        not parent.exists() or parent.is_dir()
        for parent in (db_parent, *db_parent.parents)
    )

    remedies: list[str] = []
    if missing:
        remedies.append(
            f"在 {BACKEND_DIR} 运行: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        )
    if not index_ok or not assets_ok:
        remedies.append(
            f"在 {PROJECT_ROOT / 'frontend'} 运行: npm ci 后 npm run build"
        )
    if require_chromium and chromium is None:
        remedies.append(
            f"Chromium 缺失；真实登录需要它：先按 {PROJECT_ROOT / 'docs' / 'DEVELOPMENT.md'} "
            "安装可选依赖并设置 PLAYWRIGHT_BROWSERS_PATH，再在项目根运行 "
            ".\\backend\\.venv\\Scripts\\python.exe -m playwright install chromium"
        )
    if not port_free:
        remedies.append(
            f"端口 {port} 已被占用；请停止原服务或改用 --port 指定其他端口（不要强杀未知进程）"
        )
    if not db_parent_ok:
        remedies.append(f"数据库父目录不可用: {db_parent}")

    if missing:
        status = "missing_dependency"
    elif not index_ok or not assets_ok:
        status = "missing_build"
    elif require_chromium and chromium is None:
        status = "missing_browser"
    elif not port_free:
        status = "port_in_use"
    elif not db_parent_ok:
        status = "invalid_db_path"
    else:
        status = "ready"

    return {
        "status": status,
        "python": sys.version.split()[0],
        "packages": packages,
        "chromium": chromium,
        "static_dir": str(static),
        "static_index": index_ok,
        "static_assets": assets_ok,
        "db_path": str(db),
        "port": port,
        "port_free": port_free,
        "remedies": remedies,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline readiness check")
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--static-dir", type=Path, default=None)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser-required", action="store_true")
    args = parser.parse_args(argv)
    report = check_readiness(
        db_path=args.db_path,
        static_dir=args.static_dir,
        port=args.port,
        require_chromium=not args.no_browser_required,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
