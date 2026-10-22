# -*- mode: python ; coding: utf-8 -*-
"""Pixiv 小说雷达 EXE 打包 spec（PyInstaller 6，onedir + 无控制台窗口）。

- 入口：scripts/desktop_launcher.py（--worker 自调度 gppt 登录 worker）。
- 包含：前端 dist、playwright 包（含 node 驱动）、gppt/install_playwright/pyotp、
  keyring Windows 后端与 win32ctypes。
- Chromium 浏览器体积过大，不进 PyInstaller，由 build_release.py 在装配阶段
  复制到发布目录 .runtime/playwright/。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
FRONT_DIST = ROOT / "frontend" / "dist"
SCRIPTS = ROOT / "scripts"
BACKEND = ROOT / "backend"

datas = [(str(FRONT_DIST), "frontend/dist")]
binaries = []
hiddenimports = [
    "seed_e_demo",
    "gppt",
    "gppt.api",
    "gppt.browser",
    "gppt.oauth",
    "gppt.token",
    "gppt.consts",
    "install_playwright",
    "pyotp",
    "cloudscraper",
    "pixivpy3",
    "keyring.backends.Windows",
    "keyring.backends.fail",
    "keyring.backends.null",
    "win32ctypes.pywin32.win32cred",
    "jaraco.classes",
    "jaraco.context",
    "jaraco.functools",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

datas += copy_metadata("keyring")

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries += pw_binaries
hiddenimports += pw_hidden

a = Analysis(  # noqa: F821
    [str(SCRIPTS / "desktop_launcher.py")],
    pathex=[str(SCRIPTS), str(BACKEND)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "iniconfig", "pluggy"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PixivRadar",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PixivRadar",
)
