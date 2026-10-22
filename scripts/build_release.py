"""装配 Pixiv 小说雷达 EXE 测试包。

流程：
1. 校验前端构建存在；
2. PyInstaller 构建 onedir（release/_dist/PixivRadar）；
3. 装配 release/Pixiv雷达测试包-<日期>/：
   - Pixiv雷达.exe（无控制台）+ _internal/
   - .runtime/playwright/chromium-<rev>/chrome-win64（登录用浏览器）
   - 使用说明.txt
4. 计算 exe/manual/zip 的 sha256，写 release-manifest.json；
5. 可选打 zip（tar -a）。

不包含任何用户数据、凭据、.env、venv 或 git 目录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
SPEC = ROOT / "packaging" / "pixiv_radar_desktop.spec"
FRONT_DIST = ROOT / "frontend" / "dist"
CHROMIUM_SRC = ROOT / ".runtime" / "playwright"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_pyinstaller() -> Path:
    dist = ROOT / "release" / "_dist"
    work = ROOT / "release" / "_work"
    subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            str(SPEC),
        ],
        cwd=ROOT,
        check=True,
    )
    built = dist / "PixivRadar"
    if not (built / "PixivRadar.exe").is_file():
        raise RuntimeError(f"PyInstaller 输出缺失: {built}")
    return built


def pick_chromium() -> Path:
    revisions = sorted(
        (path for path in CHROMIUM_SRC.glob("chromium-*") if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    for revision in revisions:
        if (revision / "chrome-win64" / "chrome.exe").is_file():
            return revision
    raise RuntimeError("未找到已安装的 Playwright Chromium")


def assemble(package_dir: Path, built: Path) -> dict:
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)
    for item in built.iterdir():
        target = package_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    exe_src = package_dir / "PixivRadar.exe"
    exe_dst = package_dir / "Pixiv雷达.exe"
    exe_src.rename(exe_dst)

    chromium = pick_chromium()
    runtime_target = package_dir / ".runtime" / "playwright" / chromium.name
    shutil.copytree(chromium, runtime_target)

    manual_src = ROOT / "packaging" / "使用说明.txt"
    shutil.copy2(manual_src, package_dir / "使用说明.txt")
    return {
        "exe": exe_dst,
        "chromium": runtime_target,
        "manual": package_dir / "使用说明.txt",
        "package_dir": package_dir,
    }


def make_zip(package_dir: Path) -> Path:
    """用 Python zipfile 打包：非 ASCII 名自动写 UTF-8 标记，资源管理器解压不乱码。"""
    zip_path = package_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package_dir.parent))
    if not zip_path.is_file():
        raise RuntimeError("zip 打包失败")
    return zip_path


def build_manifest(package_dir: Path, zip_path: Path | None) -> dict:
    exe = package_dir / "Pixiv雷达.exe"
    manual = package_dir / "使用说明.txt"
    chromium = next(
        (path for path in (package_dir / ".runtime" / "playwright").glob("chromium-*")),
        None,
    )
    return {
        "package": str(package_dir.relative_to(ROOT)),
        "exe_sha256": sha256_file(exe),
        "exe_bytes": exe.stat().st_size,
        "manual_sha256": sha256_file(manual),
        "zip": None if zip_path is None else str(zip_path.relative_to(ROOT)),
        "zip_sha256": None if zip_path is None else sha256_file(zip_path),
        "zip_bytes": None if zip_path is None else zip_path.stat().st_size,
        "chromium": None if chromium is None else chromium.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true", help="跳过 PyInstaller（复用上次输出）")
    parser.add_argument("--zip-only", action="store_true", help="只重打 zip 与清单")
    parser.add_argument("--skip-zip", action="store_true")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    if not (FRONT_DIST / "index.html").is_file():
        raise SystemExit("前端构建缺失：请先在 frontend 运行 npm run build")

    name = args.output_dir or f"Pixiv雷达测试包-{date.today().strftime('%Y%m%d')}"
    package_dir = ROOT / "release" / name

    if args.zip_only:
        if not (package_dir / "Pixiv雷达.exe").is_file():
            raise SystemExit(f"发布目录不存在或不完整: {package_dir}")
        zip_path = None if args.skip_zip else make_zip(package_dir)
        manifest = build_manifest(package_dir, zip_path)
    else:
        built = (
            ROOT / "release" / "_dist" / "PixivRadar"
            if args.skip_build
            else run_pyinstaller()
        )
        assemble(package_dir, built)
        zip_path = None if args.skip_zip else make_zip(package_dir)
        manifest = build_manifest(package_dir, zip_path)

    manifest_path = package_dir.parent / f"{package_dir.name}-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("manifest:", manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
