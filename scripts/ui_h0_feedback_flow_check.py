"""H0 单服务预览的反馈全流程真实 UI 验收（A/B 切换、加载更多、排除/撤销/好文）。

只访问 127.0.0.1 的 fake 演示库；不访问 Pixiv、不读取凭据。
结束后目标 1 的最终状态为“好文”，供 restart/restore 复验使用。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.auth.runtime_paths import PLAYWRIGHT_BROWSERS_PATH  # noqa: E402

import os  # noqa: E402

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS_PATH)

from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "docs" / "visual"


def _run_one(
    browser,
    url: str,
    width: int,
    height: int,
    artifact: str,
) -> dict:
    errors: list[str] = []
    page = browser.new_page(viewport={"width": width, "height": height})
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("table[aria-label*='目标列表']", timeout=15000)
    assert page.get_by_text("演示数据：当前显示 Mock 扫描结果，非真实 Pixiv 数据。", exact=True).count() == 1
    assert page.locator("[data-testid='ppi-radar']").count() == 1

    # 选中 novel-01 → 反馈面板出现；预置了 8 条点击事件 → 首屏 5 条 + 加载更多
    row_1 = page.locator("table tbody tr").filter(has_text="novel-01").first
    assert row_1.count() == 1
    row_1.click()
    page.wait_for_selector("[data-testid='feedback-panel']", timeout=10000)
    page.get_by_role("button", name="加载更多审计记录").click()
    page.wait_for_function(
        "document.querySelectorAll('[data-testid=feedback-panel] ul li').length === 8",
        timeout=10000,
    )

    # A/B 切换：novel-02 面板状态独立，再切回 novel-01 事件仍为 8 条
    row_2 = page.locator("table tbody tr").filter(has_text="novel-02").first
    assert row_2.count() == 1
    row_2.click()
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('暂无明确反馈')",
        timeout=10000,
    )
    page.locator("table tbody tr").filter(has_text="novel-01").first.click()
    # 切回 novel-01：分页正确重置为首屏 5 条，加载更多按钮可用（不混入旧分页）
    page.wait_for_function(
        "document.querySelectorAll('[data-testid=feedback-panel] ul li').length === 5",
        timeout=10000,
    )
    assert page.get_by_role("button", name="加载更多审计记录").count() == 1

    # 排除 → 撤销 → 好文（最终状态保持好文）
    page.get_by_role("button", name="标记排除").click()
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：排除')",
        timeout=10000,
    )
    page.get_by_role("button", name="撤销当前反馈").click()
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('暂无明确反馈')",
        timeout=10000,
    )
    page.get_by_role("button", name="标记好文").click()
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：好文')",
        timeout=10000,
    )

    # 实际 API 持久化：目标 1 事件序列含 exclude/undo/good 且全部 novel_id=1
    state = page.evaluate(
        """
        async () => {
          const response = await fetch('/api/feedback/targets/1?limit=50');
          return await response.json();
        }
        """
    )
    event_types = [event["event_type"] for event in state["events"]]
    assert event_types[:3] == ["good", "undo", "exclude"], event_types
    assert all(event["novel_id"] == 1 for event in state["events"])
    assert state["summary"]["effective_feedback"] == "good"
    # novel-02 未被污染
    state_2 = page.evaluate(
        """
        async () => {
          const response = await fetch('/api/feedback/targets/2?limit=20');
          return await response.json();
        }
        """
    )
    assert state_2["events"] == []
    # 标签说明存在
    assert page.get_by_text("偏好标签（基于反馈事件）").count() == 1
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert not errors, errors
    # 404 与缺失资源不得冒充首页成功（独立 HTTP 探测，不污染页面控制台）
    def _status(path: str) -> int:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=10)
            return 200
        except urllib.error.HTTPError as exc:
            return exc.code

    api_404 = _status("/api/does-not-exist")
    asset_404 = _status("/missing-asset-xyz.js")
    assert api_404 == 404
    assert asset_404 == 404
    # 缺失资源不得返回首页内容
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/missing-asset-xyz.js", timeout=10)
        body = b""
    except urllib.error.HTTPError as exc:
        body = exc.read()
    assert b"<!doctype html" not in body.lower()
    screenshot = OUT_DIR / f"feedback-night-{artifact}-{width}x{height}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    page.close()
    return {
        "viewport": f"{width}x{height}",
        "event_types": event_types,
        "effective_feedback": state["summary"]["effective_feedback"],
        "novel_2_untouched": True,
        "api_404_status": api_404,
        "missing_asset_404_status": asset_404,
        "horizontal_overflow": False,
        "console_errors": errors,
        "screenshot": str(screenshot),
    }


def _run_layout(browser, url: str, width: int, height: int, artifact: str) -> dict:
    """只读布局检查（不修改反馈状态），供第二个视口使用。"""
    errors: list[str] = []
    page = browser.new_page(viewport={"width": width, "height": height})
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("table[aria-label*='目标列表']", timeout=15000)
    assert page.get_by_text("演示数据：当前显示 Mock 扫描结果，非真实 Pixiv 数据。", exact=True).count() == 1
    assert page.locator("[data-testid='ppi-radar']").count() == 1
    assert page.locator("[data-testid='score-heatmap']").count() == 1
    page.locator("table tbody tr").filter(has_text="novel-01").first.click()
    page.wait_for_selector("[data-testid='feedback-panel']", timeout=10000)
    # 只读加载更多：首屏 5 条，点击后追加（不关心总数，只验证分页可用且无重复崩溃）
    page.get_by_role("button", name="加载更多审计记录").click()
    page.wait_for_function(
        "document.querySelectorAll('[data-testid=feedback-panel] ul li').length > 5",
        timeout=10000,
    )
    assert page.get_by_text("偏好标签（基于反馈事件）").count() == 1
    assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert not errors, errors
    screenshot = OUT_DIR / f"feedback-night-{artifact}-{width}x{height}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    page.close()
    return {
        "viewport": f"{width}x{height}",
        "read_only_layout": True,
        "horizontal_overflow": False,
        "console_errors": errors,
        "screenshot": str(screenshot),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/")
    parser.add_argument("--artifact", default="night")
    args = parser.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        flow_report = _run_one(browser, args.url, 1280, 720, args.artifact)
        layout_report = _run_layout(browser, args.url, 900, 900, args.artifact)
        browser.close()
    print(json.dumps([flow_report, layout_report], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
