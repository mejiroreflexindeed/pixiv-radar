"""登录可用版真实浏览器验收（W06/W09）：新构建 + 新 fixture + 三视口。

只启动本地回环预览（演示 Mock，不访问 Pixiv、不读凭据），检查：
真实用户操作流、1280x720/900x900/1920x1080 截图、console/page error、
横向溢出、键盘选择、候选/表格选中联动详情、ECharts 真实图点点击联动。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.auth.runtime_paths import PLAYWRIGHT_BROWSERS_PATH  # noqa: E402

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS_PATH)

from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = (
    PROJECT_ROOT / "docs" / "reviews" / "login-ready-20260909-01" / "ui-w09-01"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(base_url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise AssertionError(f"preview server not ready: {last_error}")


def _start_server(fixture: Path, port: int) -> subprocess.Popen:
    log = (fixture / "preview-server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_local_preview.py"),
            "--db-path",
            str(fixture / "preview" / "radar.db"),
            "--seed",
            "--no-browser",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    process._radar_log = log  # type: ignore[attr-defined]
    return process


def _chart_points(page) -> dict:
    return page.evaluate(
        """
        () => {
          const element = document.querySelector('[data-testid="ppi-radar"]');
          const chart = element && element.__radarChart;
          const option = chart?.getOption?.() || {};
          const series = option.series?.[0] || {};
          const data = Array.isArray(series.data) ? series.data : [];
          const points = data.map((item) => {
            const value = Array.isArray(item.value) ? item.value : [];
            const pixel = chart?.convertToPixel?.({seriesIndex: 0}, value.slice(0, 2));
            const finite = Array.isArray(pixel) && pixel.length >= 2 && pixel.every(Number.isFinite);
            return {
              novel_id: item.novel_id,
              title: item?._meta?.title || '',
              pixel: finite ? pixel.slice(0, 2) : null,
              contained: finite ? Boolean(chart?.containPixel?.({seriesIndex: 0}, pixel)) : false,
            };
          });
          const dataModel = chart?.getModel?.().getSeriesByIndex?.(0)?.getData?.();
          const rendered = data.filter((_, index) => {
            const graphic = dataModel?.getItemGraphicEl?.(index);
            return graphic && graphic.ignore !== true;
          }).length;
          return {
            series_type: series.type,
            data_count: data.length,
            rendered_count: rendered,
            finite_pixel_count: points.filter((point) => point.pixel).length,
            contained_pixel_count: points.filter((point) => point.contained).length,
            points,
          };
        }
        """
    )


def _measure_candidates(page) -> dict:
    """标题可读宽度与首屏行坐标（不只看 DOM 行数）。"""
    rows = page.locator("table[aria-label='发现候选列表'] tbody tr")
    count = min(rows.count(), 6)
    measurements = []
    for index in range(count):
        row = rows.nth(index)
        box = row.bounding_box()
        title = row.locator(".title-text")
        title_box = title.bounding_box() if title.count() else None
        measurements.append(
            {
                "index": index + 1,
                "row_x": box["x"] if box else None,
                "row_bottom": (box["y"] + box["height"]) if box else None,
                "title_width": title_box["width"] if title_box else None,
            }
        )
    return {"rows": measurements}


def _measure_detail(page) -> dict:
    detail = page.locator("[data-testid='selected-target-panel']")
    metrics: dict[str, object] = {}

    def box_of(selector: str):
        locator = detail.locator(selector).first
        if locator.count() == 0:
            return None
        box = locator.bounding_box()
        return None if box is None else {"y": box["y"], "bottom": box["y"] + box["height"]}

    metrics["panel_y"] = (detail.bounding_box() or {}).get("y")
    metrics["heading"] = box_of("h2.detail-title")
    metrics["feedback"] = box_of("[data-testid='feedback-panel']")
    metrics["link"] = box_of("a[href*='pixiv.net']")
    return metrics


def _run_discovery(page) -> None:
    # 演示模式一次运行可能在第一次轮询前就结束，不能只等出现 running；
    # 以“出现新的 run_key 且已到终态”为准（慢运行时也会经过 running 再到终态）。
    before = page.evaluate(
        "fetch('/api/discovery/status').then(r => r.json()).then(s => (s.current_run && s.current_run.run_key) || null)"
    )
    page.get_by_role("button", name="运行发现").click()
    page.wait_for_function(
        "(before) => fetch('/api/discovery/status').then(r => r.json()).then(s => {"
        "  const key = (s.current_run && s.current_run.run_key) || null;"
        "  return key !== before && s.state !== 'running';"
        "})",
        arg=before,
        timeout=30000,
    )
    # 等 App 的轮询把终态反映到界面（按钮恢复、运行中提示消失）
    page.wait_for_function(
        "!document.querySelector('[data-testid=discovery-panel]')?.innerText.includes('运行中')",
        timeout=15000,
    )


def _select_first_candidate(page) -> str:
    row = page.locator("table[aria-label='发现候选列表'] tbody tr").first
    row.wait_for(timeout=15000)
    title = row.locator("td.title-cell .title-text").inner_text()
    row.click()
    page.wait_for_selector("[data-testid='history-chart']", timeout=15000)
    page.wait_for_function(
        "(title) => document.querySelector('[data-testid=selected-target-panel]')?.innerText.includes(title)",
        arg=title,
        timeout=15000,
    )
    return title


def _step(name: str) -> None:
    print(f"[ui] {name}", file=sys.stderr, flush=True)


def _run_full_flow(page, viewport: str, out_dir: Path) -> dict:
    checks: dict[str, object] = {"viewport": viewport, "full_flow": True}
    _step("full: first discovery")
    _run_discovery(page)
    page.locator("[data-testid='discovery-panel']").get_by_text(
        "部分成功", exact=True
    ).wait_for(timeout=15000)
    _step("full: first partial ok")
    panel_text = page.locator("[data-testid='discovery-panel']").inner_text()
    assert "来源：mock" in panel_text, panel_text[:400]
    checks["first_run_partial"] = "部分成功" in panel_text
    _step("full: second discovery")
    _run_discovery(page)
    page.locator("[data-testid='discovery-panel']").get_by_text(
        "成功", exact=True
    ).wait_for(timeout=30000)
    _step("full: second run ok")

    candidate_rows = page.locator(
        "table[aria-label='发现候选列表'] tbody tr"
    ).count()
    assert candidate_rows >= 5, f"普通候选不足 5 条: {candidate_rows}"
    checks["candidate_rows"] = candidate_rows

    first_title = _select_first_candidate(page)
    _step("full: candidate selected")
    detail = page.locator("[data-testid='selected-target-panel']")
    box = detail.bounding_box()
    assert box is not None and box["y"] < 720, "1280x720 首屏看不到详情"
    heading = detail.locator("h2").first.inner_text()
    assert first_title in heading, (first_title, heading)
    link = page.get_by_role("link", name=re.compile("Pixiv"))
    assert link.count() >= 1, "缺少 Pixiv 外链"
    hrefs = [link.nth(i).get_attribute("href") or "" for i in range(link.count())]
    assert any("pixiv.net/novel/show.php" in href for href in hrefs), hrefs
    checks["candidate_to_detail"] = True

    # R5：标题文字区可读宽度与首屏 5 条候选坐标（真实 bounding box，不用 DOM 数）。
    candidate_metrics = _measure_candidates(page)
    checks["candidate_metrics"] = candidate_metrics
    assert len(candidate_metrics["rows"]) >= 5, candidate_metrics
    for measurement in candidate_metrics["rows"][:5]:
        assert measurement["title_width"] is not None
        assert measurement["title_width"] >= 160, measurement
        assert measurement["row_bottom"] is not None
        assert measurement["row_bottom"] <= 720, f"首屏第 {measurement['index']} 条不在 720 内"
    detail_metrics = _measure_detail(page)
    checks["detail_metrics"] = detail_metrics
    for key in ("heading", "feedback"):
        assert detail_metrics[key] is not None, detail_metrics
        assert detail_metrics[key]["bottom"] <= 720, (key, detail_metrics)

    # good → exclude → undo → good；候选排序/解释在 POST 确认后重读
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
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：好文')",
        timeout=10000,
    )
    checks["feedback_roundtrip"] = True

    # 真实 ECharts 图点：像素命中 + 点击联动详情
    page.get_by_role("button", name="展开雷达与热力图").click()
    radar = page.locator("[data-testid='ppi-radar']")
    radar.wait_for(timeout=15000)
    page.wait_for_timeout(600)
    radar.scroll_into_view_if_needed()
    chart = _chart_points(page)
    assert chart["series_type"] == "scatter", chart
    assert chart["data_count"] > 0, chart
    assert chart["finite_pixel_count"] == chart["data_count"], chart
    assert chart["contained_pixel_count"] == chart["data_count"], chart
    assert chart["rendered_count"] > 0, chart
    point = next(
        (
            item
            for item in chart["points"]
            if item["pixel"]
            and item["title"]
            and item["title"] not in heading
        ),
        None,
    )
    assert point is not None, "没有可点击的其他雷达点"
    radar_box = radar.bounding_box()
    assert radar_box is not None
    page.mouse.click(
        radar_box["x"] + point["pixel"][0],
        radar_box["y"] + point["pixel"][1],
    )
    page.wait_for_function(
        "(title) => document.querySelector('[data-testid=selected-target-panel]')?.innerText.includes(title)",
        arg=point["title"],
        timeout=15000,
    )
    checks["radar_pixel_click"] = True

    # 我的作品：分页信息 + 键盘选择
    page.get_by_role("button", name="我的作品").click()
    page.wait_for_selector("table[aria-label*='目标列表']", timeout=15000)
    assert page.get_by_text(re.compile(r"共 \d+ 条")).count() >= 1
    target_row = page.locator("table[aria-label*='目标列表'] tbody tr").first
    target_title = target_row.locator("td").first.inner_text()
    target_row.focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "(title) => document.querySelector('[data-testid=selected-target-panel]')?.innerText.includes(title)",
        arg=target_title,
        timeout=15000,
    )
    checks["keyboard_select"] = True

    # 筛选/排序不丢选择（A5）：切换筛选后详情仍在（或明确提示在筛选外），排序后表格可用。
    selected_filter = 'track'
    page.get_by_label("状态筛选").select_option(selected_filter)
    page.wait_for_timeout(800)
    detail_text = page.locator("[data-testid='selected-target-panel']").inner_text()
    assert target_title in detail_text or "不在此筛选结果中" in detail_text, detail_text[:200]
    checks["filter_keeps_selection"] = True
    page.get_by_label("状态筛选").select_option("")
    page.wait_for_timeout(500)
    page.get_by_role("button", name=re.compile("标题")).click()
    page.wait_for_timeout(500)
    assert page.locator("table[aria-label*='目标列表'] tbody tr").count() >= 1
    assert page.get_by_text(re.compile(r"共 \d+ 条")).count() >= 1
    checks["sort_keeps_table"] = True
    page.get_by_label("标题或作者查询").fill("")
    page.wait_for_timeout(300)

    # 运行与设置：连接详情、运行历史、事件
    page.get_by_role("button", name="运行与设置").click()
    page.get_by_text("扫描历史").wait_for(timeout=10000)
    page.get_by_text("事件日志").wait_for(timeout=10000)
    checks["runs_view"] = True

    # 刷新后反馈仍在
    page.reload(wait_until="networkidle")
    page.wait_for_selector("[data-testid='discovery-panel']", timeout=15000)
    _select_first_candidate(page)
    page.wait_for_function(
        "document.querySelector('[data-testid=feedback-panel]')?.innerText.includes('当前标记：好文')",
        timeout=15000,
    )
    checks["feedback_persisted_after_reload"] = True

    page.screenshot(path=str(out_dir / f"login-ready-{viewport}-selected.png"), full_page=True)
    return checks


def _run_light_flow(page, viewport: str, out_dir: Path) -> dict:
    checks: dict[str, object] = {"viewport": viewport, "full_flow": False}
    _run_discovery(page)
    title = _select_first_candidate(page)
    page.wait_for_function(
        "(title) => document.querySelector('[data-testid=selected-target-panel]')?.innerText.includes(title)",
        arg=title,
        timeout=15000,
    )
    candidate_metrics = _measure_candidates(page)
    checks["candidate_metrics"] = candidate_metrics
    for measurement in candidate_metrics["rows"][:5]:
        assert measurement["title_width"] is not None
        assert measurement["title_width"] >= 160, measurement
    checks["candidate_to_detail"] = True
    page.screenshot(path=str(out_dir / f"login-ready-{viewport}-selected.png"), full_page=True)
    return checks


def _run_narrow_flow(page, viewport: str, out_dir: Path) -> dict:
    checks: dict[str, object] = {"viewport": viewport, "narrow": True}
    _run_discovery(page)
    title = _select_first_candidate(page)
    page.wait_for_function(
        "(title) => document.querySelector('[data-testid=selected-target-panel]')?.innerText.includes(title)",
        arg=title,
        timeout=15000,
    )
    # 选中会触发窄屏滚动到详情；先回到顶部再测量单列布局。
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(150)
    main_box = page.locator(".workspace__main").bounding_box()
    detail_box = page.locator("[data-testid='selected-target-panel']").bounding_box()
    assert main_box is not None and detail_box is not None
    # <900 单列：详情与主列同左缘且在其下方，而不是侧栏并排。
    assert abs(detail_box["x"] - main_box["x"]) < 24, (main_box, detail_box)
    assert detail_box["y"] > main_box["y"] + main_box["height"] - 40, (
        main_box,
        detail_box,
    )
    checks["single_column"] = True
    page.screenshot(path=str(out_dir / f"login-ready-{viewport}-selected.png"), full_page=True)
    page.get_by_role("button", name="返回列表").click()
    page.wait_for_function("() => document.querySelectorAll('h2.detail-title').length === 0")
    checks["back_to_list"] = True
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--keep-fixture", action="store_true")
    args = parser.parse_args(argv)

    fixture = args.fixture_dir.resolve(strict=False)
    if fixture.exists():
        print(json.dumps({"status": "failed", "code": "fixture_already_exists", "fixture": str(fixture)}, ensure_ascii=False))
        return 2
    fixture.mkdir(parents=True)
    out_dir = fixture / "artifacts"
    out_dir.mkdir()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    report: dict[str, object] = {
        "status": "failed",
        "transport": "mock",
        "url": base_url,
        "checks": [],
        "screenshots": [],
        "console_errors": [],
    }
    process = _start_server(fixture, port)
    try:
        _wait_health(base_url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width, height, mode in (
                (1280, 720, 'full'),
                (900, 900, 'light'),
                (1920, 1080, 'light'),
                (820, 900, 'narrow'),
            ):
                viewport = f"{width}x{height}"
                console_errors: list[str] = []
                page_errors: list[str] = []
                benign_errors: list[str] = []
                page = browser.new_page(viewport={"width": width, "height": height})

                def on_console(msg):
                    if msg.type != "error":
                        return
                    location = (msg.location or {}).get("url", "")
                    text = f"{msg.text} @ {location}" if location else msg.text
                    # 无缓存正文/相似的可选 GET 是受控 404，浏览器仍会记一条资源错误。
                    if re.search(r"/api/content/\d+(/similar)?$", location):
                        benign_errors.append(text)
                    else:
                        console_errors.append(text)

                page.on("console", on_console)
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.goto(base_url + "/", wait_until="networkidle")
                page.wait_for_selector("[data-testid='discovery-panel']", timeout=20000)
                assert page.locator(".demo-banner").count() == 1
                page.screenshot(
                    path=str(out_dir / f"login-ready-{viewport}-default.png"),
                    full_page=True,
                )
                if mode == 'full':
                    checks = _run_full_flow(page, viewport, out_dir)
                elif mode == 'narrow':
                    checks = _run_narrow_flow(page, viewport, out_dir)
                else:
                    checks = _run_light_flow(page, viewport, out_dir)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > window.innerWidth"
                )
                assert not overflow, f"{viewport} 横向溢出"
                assert not console_errors and not page_errors, (
                    console_errors,
                    page_errors,
                )
                checks["horizontal_overflow"] = bool(overflow)
                checks["console_errors"] = list(console_errors)
                checks["page_errors"] = list(page_errors)
                checks["benign_optional_404"] = list(benign_errors)
                report["checks"].append(checks)
                report["screenshots"].extend(
                    [
                        str(out_dir / f"login-ready-{viewport}-default.png"),
                        str(out_dir / f"login-ready-{viewport}-selected.png"),
                    ]
                )
                page.close()
            browser.close()
        report["status"] = "passed"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log = getattr(process, "_radar_log", None)
        if log is not None:
            log.close()
        if not args.keep_fixture:
            import shutil

            shutil.rmtree(fixture, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
