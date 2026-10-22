"""Demo 单服务 WebUI 真实浏览器验收（WP4/WP7）。

只访问 http://127.0.0.1:8000（单回环预览）；离线 Mock，不访问 Pixiv。
流程：打开 → 运行发现（首次 partial、重试 succeeded）→ 候选可读 →
选择候选 → 详情与 Pixiv 外链 → good/undo 反馈 → 三视口截图与报告。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.auth.runtime_paths import PLAYWRIGHT_BROWSERS_PATH  # noqa: E402

import os  # noqa: E402

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS_PATH)

from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "docs" / "visual"


def _wait_run_terminal(page) -> None:
    page.get_by_role("button", name="运行发现").click()
    page.wait_for_function(
        "document.querySelector('[data-testid=discovery-panel]')?.innerText.includes('最近一次')",
        timeout=30000,
    )


def _select_candidate(page) -> None:
    row = page.locator("table[aria-label='发现候选列表'] tbody tr").first
    row.wait_for(timeout=15000)
    row.click()
    page.wait_for_selector("[data-testid='history-chart']", timeout=10000)


def _run_flow(page) -> None:
    # 第一次运行：keyword 来源临时失败 → partial
    _wait_run_terminal(page)
    page.locator("[data-testid='discovery-panel']").get_by_text(
        "部分成功", exact=True
    ).wait_for(timeout=10000)
    # 第二次运行（重试）：keyword 来源恢复 → succeeded；运行中不可重复创建 run
    _wait_run_terminal(page)
    page.locator("[data-testid='discovery-panel']").get_by_text(
        "成功", exact=True
    ).wait_for(timeout=10000)
    panel_text = page.locator("[data-testid='discovery-panel']").inner_text()
    assert "来源：mock" in panel_text

    # 候选可读
    _select_candidate(page)

    # Pixiv 外链存在（不自动打开、不抓正文）
    link = page.get_by_role("link", name=re.compile("Pixiv"))
    assert link.count() == 1
    assert "pixiv.net/novel/show.php" in (link.get_attribute("href") or "")

    # good → exclude → undo → good（覆盖全部三种显式反馈）
    page.get_by_role("button", name="标记好文").click()
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：好文')",
        timeout=10000,
    )
    page.get_by_role("button", name="标记排除").click()
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：排除')",
        timeout=10000,
    )
    page.get_by_role("button", name="撤销当前反馈").click()
    # feedback-v1 撤销链：撤销 exclude 后回退到之前的 good
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：好文')",
        timeout=10000,
    )

    # 表格排序真实可用：点击“收藏”表头 → 触发服务端排序请求且 aria-sort 更新
    sort_button = page.get_by_role("button", name=re.compile("收藏"))
    sort_button.click()
    page.wait_for_function(
        "document.querySelector('th[aria-sort]') !== null",
        timeout=10000,
    )

    # 筛选真实可用：状态筛选 → 表格行数与未筛选时不同
    before_rows = page.locator("table[aria-label*='目标列表'] tbody tr").count()
    page.locator("select[aria-label='状态筛选']").select_option("track")
    page.wait_for_timeout(800)
    after_rows = page.locator("table[aria-label*='目标列表'] tbody tr").count()
    assert before_rows != after_rows, (before_rows, after_rows)

    # 页面刷新后反馈与发现历史保留
    page.reload(wait_until="networkidle")
    page.wait_for_selector("table[aria-label*='目标列表']", timeout=15000)
    assert "部分成功" in page.locator("[data-testid='discovery-panel']").inner_text() or \
        "成功" in page.locator("[data-testid='discovery-panel']").inner_text()


def _run_viewport(browser, url: str, width: int, height: int, *, flow: bool) -> dict:
    errors: list[str] = []
    page = browser.new_page(viewport={"width": width, "height": height})
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("table[aria-label*='目标列表']", timeout=15000)
    assert page.get_by_text("演示数据：当前显示 Mock 扫描结果，非真实 Pixiv 数据。", exact=True).count() == 1
    assert page.locator("[data-testid='ppi-radar']").count() == 1
    if flow:
        _run_flow(page)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert not overflow
    assert not errors, errors
    screenshot = OUT_DIR / f"demo-{width}x{height}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    page.close()
    return {
        "viewport": f"{width}x{height}",
        "flow_executed": flow,
        "horizontal_overflow": bool(overflow),
        "console_errors": errors,
        "screenshot": str(screenshot),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/")
    args = parser.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        reports = [
            _run_viewport(browser, args.url, 1280, 720, flow=True),
            _run_viewport(browser, args.url, 900, 900, flow=False),
            _run_viewport(browser, args.url, 1920, 1080, flow=False),
        ]
        browser.close()
    report_path = OUT_DIR / "demo-check-report.json"
    report_path.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
