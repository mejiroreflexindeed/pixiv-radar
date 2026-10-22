"""Phase F 离线反馈闭环的真实本地 UI 验收。

只访问 127.0.0.1 的 fake 数据库和 React 代理；不访问 Pixiv、不读取凭据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.auth.runtime_paths import PLAYWRIGHT_BROWSERS_PATH  # noqa: E402

import os  # noqa: E402

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS_PATH)

from playwright.sync_api import sync_playwright  # noqa: E402

FRONTEND_URL = "http://127.0.0.1:5173/"
OUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "visual" / "feedback-f-1280x720.png"


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )
        page.goto(FRONTEND_URL, wait_until="networkidle")
        page.wait_for_selector("table[aria-label*='目标列表']", timeout=15000)
        target_row = page.locator("table tbody tr").filter(has_text="novel-01").first
        assert target_row.count() == 1
        target_row.click()
        page.wait_for_selector("[data-testid='feedback-panel']", timeout=10000)
        assert page.get_by_text("暂无明确反馈").count() == 1

        page.get_by_role("button", name="标记好文").click()
        page.wait_for_function(
            "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前：好文')",
            timeout=10000,
        )

        page.get_by_role("button", name="撤销当前反馈").click()
        page.wait_for_function(
            "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('暂无明确反馈')",
            timeout=10000,
        )

        state = page.evaluate(
            """
            async () => {
              const response = await fetch('/api/feedback/targets/1?limit=20');
              return await response.json();
            }
            """
        )
        event_types = [event["event_type"] for event in state["events"]]
        assert event_types[:2] == ["undo", "good"]
        assert state["summary"]["effective_feedback"] is None
        assert state["summary"]["reverted_event_count"] == 1
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert not errors, errors
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUT_PATH), full_page=True)
        print(json.dumps({
            "feedback_round_trip": True,
            "event_types": event_types,
            "effective_feedback": state["summary"]["effective_feedback"],
            "reverted_event_count": state["summary"]["reverted_event_count"],
            "horizontal_overflow": False,
            "console_errors": errors,
            "screenshot": str(OUT_PATH),
        }, ensure_ascii=False))
        page.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
