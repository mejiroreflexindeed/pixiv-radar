"""H0 单服务构建预览的真实 Chromium 验收。"""

from __future__ import annotations

import argparse
import json
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("initial", "restart"), required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8000/")
    parser.add_argument("--artifact-suffix", default="")
    parser.add_argument("--viewport-width", type=int, default=1280)
    parser.add_argument("--viewport-height", type=int, default=720)
    args = parser.parse_args(argv)
    errors: list[str] = []
    suffix = f"-{args.artifact_suffix}" if args.artifact_suffix else ""
    screenshot = OUT_DIR / (
        f"local-preview-h0-{args.mode}{suffix}-"
        f"{args.viewport_width}x{args.viewport_height}.png"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": args.viewport_width, "height": args.viewport_height}
        )
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_selector("table[aria-label*='目标列表']", timeout=15000)
        assert page.get_by_text("演示数据：当前显示 Mock 扫描结果，非真实 Pixiv 数据。", exact=True).count() == 1
        target_row = page.locator("table tbody tr").filter(has_text="novel-01").first
        assert target_row.count() == 1
        target_row.click()
        page.wait_for_selector("[data-testid='feedback-panel']", timeout=10000)
        if args.mode == "initial":
            page.get_by_role("button", name="标记好文").click()
            page.wait_for_function(
                "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：好文')",
                timeout=10000,
            )
        else:
            page.wait_for_function(
                "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：好文')",
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
        assert event_types[0] == "good"
        assert state["summary"]["effective_feedback"] == "good"
        assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert not errors, errors
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
        report = {
            "mode": args.mode,
            "single_service": True,
            "loopback": True,
            "feedback_persisted": True,
            "event_types": event_types,
            "effective_feedback": state["summary"]["effective_feedback"],
            "horizontal_overflow": False,
            "console_errors": errors,
            "screenshot": str(screenshot),
        }
        print(json.dumps(report, ensure_ascii=False))
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
