"""Login-1B 运行时路径常量：所有 gppt/Playwright 副作用约束在工作区 .runtime 内。"""

from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_DIR = WORKSPACE_ROOT / ".runtime"
PLAYWRIGHT_BROWSERS_PATH = RUNTIME_DIR / "playwright"
GPPT_CONFIG_DIR = RUNTIME_DIR / "gppt-empty"
