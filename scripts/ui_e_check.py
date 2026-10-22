"""阶段 E-R2 本地可视化验收脚本。

使用工作区 .runtime/playwright 的 Chromium 只访问 http://127.0.0.1:5173
（前端）与代理后端；不访问 Pixiv。除元素存在外，按 R1 契约独立计算
（不复用前端 TS 实现）雷达可区分性指标；同时检查真实 ECharts 实例的
series 数据、像素坐标、坐标包含关系和已绘制 display list。
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.auth.runtime_paths import PLAYWRIGHT_BROWSERS_PATH  # noqa: E402

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS_PATH)

from playwright.sync_api import sync_playwright  # noqa: E402

FRONTEND_URL = "http://127.0.0.1:5173/"
BACKEND_URL = "http://127.0.0.1:8000"
OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "visual"


def _ratio(ewma, threshold):
    if ewma is None or threshold is None:
        return None
    if not math.isfinite(ewma) or not math.isfinite(threshold) or threshold <= 0:
        return None
    return max(ewma, 0.0) / max(threshold, 1e-9)


def _p95(values: list[float]) -> float:
    sorted_values = sorted(values)
    rank = (len(sorted_values) - 1) * 0.95
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (rank - lo) * (sorted_values[hi] - sorted_values[lo])


def _strength(ratio: float, scale_max: float) -> float:
    denom = math.log1p(max(scale_max, 2.0))
    return min(max(math.log1p(ratio) / denom, 0.0), 1.0)


def radar_metrics() -> dict:
    with urllib.request.urlopen(
        f"{BACKEND_URL}/api/targets?limit=100", timeout=10
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    ratios = [_ratio(i["latest_ewma"], i["latest_threshold"]) for i in payload["items"]]
    finite = [r for r in ratios if r is not None]
    scale_max = max(2.0, _p95(finite)) if finite else 2.0
    radii = []
    high_samples = []
    for item, ratio in zip(payload["items"], ratios):
        if ratio is None:
            radii.append(96)
            continue
        strength = _strength(ratio, scale_max)
        radius = 12 + 80 * (1 - strength)
        radii.append(round(radius, 6))
        if ratio > 2:
            high_samples.append(
                {
                    "novel_id": item["novel_id"],
                    "ratio": round(ratio, 4),
                    "strength": round(strength, 4),
                    "radius": round(radius, 4),
                }
            )
    ratio_pair = [
        sample for sample in high_samples
        if sample["ratio"] in (5.0, 6.4)
    ]
    return {
        "finite_radar_points": len(finite),
        "distinct_radius_count": len(set(radii)),
        "min_radius": min(radii) if radii else None,
        "max_radius": max(radii) if radii else None,
        "scale_max": round(scale_max, 4),
        "high_signal_samples": sorted(
            high_samples, key=lambda s: -s["ratio"]
        )[:6],
        "ratio_5_vs_6_4": sorted(ratio_pair, key=lambda s: s["ratio"]),
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    metrics = radar_metrics()
    assert len(metrics["ratio_5_vs_6_4"]) == 2
    assert len({sample["radius"] for sample in metrics["ratio_5_vs_6_4"]}) == 2
    report: dict = {
        "radar_metrics": metrics,
        "checks": [],
        "screenshots": [],
        "console_errors": [],
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for width, height, name in (
            (1280, 720, "1280x720"),
            (1920, 1080, "1920x1080"),
            (900, 900, "900px"),
        ):
            page = browser.new_page(viewport={"width": width, "height": height})
            console_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error"
                else None,
            )
            page.goto(FRONTEND_URL, wait_until="networkidle")
            page.wait_for_selector("table[aria-label*='目标列表']", timeout=15000)

            assert page.locator(".demo-banner").count() >= 1, "缺少演示数据横幅"
            radar = page.locator("[data-testid='ppi-radar']")
            assert radar.count() == 1
            assert page.locator("[data-testid='score-heatmap']").count() == 1
            assert page.locator("table tbody tr").count() >= 12, "表格行数不足"

            page.locator("table tbody tr").first.click()
            page.wait_for_selector("[data-testid='history-chart']", timeout=10000)
            first_row_title = page.locator("table tbody tr").first.locator("td").first.inner_text()
            detail_headings = page.locator("h2").all_text_contents()
            assert any(first_row_title in heading for heading in detail_headings), "表格选择未联动详情"
            radar.scroll_into_view_if_needed()

            chart_metrics = page.evaluate(
                """
                () => {
                  const element = document.querySelector('[data-testid="ppi-radar"]');
                  const chart = element && element.__radarChart;
                  const option = chart?.getOption?.() || {};
                  const series = option.series?.[0] || {};
                  const data = Array.isArray(series.data) ? series.data : [];
                  const points = data.map((item, index) => {
                    const value = Array.isArray(item.value) ? item.value : [];
                    const pixel = chart?.convertToPixel?.({seriesIndex: 0}, value.slice(0, 2));
                    const finite = Array.isArray(pixel) && pixel.length >= 2 && pixel.every(Number.isFinite);
                    return {
                      index,
                      novel_id: item.novel_id,
                      value: value.slice(0, 3),
                      pixel: finite ? pixel.slice(0, 2) : null,
                      contained: finite ? Boolean(chart?.containPixel?.({seriesIndex: 0}, pixel)) : false,
                    };
                  });
                  const displayList = chart?.getZr?.().storage.getDisplayList?.() || [];
                  // ECharts 6 canvas displayables do not expose a stable
                  // series marker. The series data model does expose the
                  // actual graphic element for every item after rendering.
                  const dataModel = chart?.getModel?.().getSeriesByIndex?.(0)?.getData?.();
                  const graphics = data.map((_, index) => dataModel?.getItemGraphicEl?.(index));
                  const renderedPointCount = graphics.filter((item) => item && item.ignore !== true).length;
                  return {
                    series_type: series.type,
                    series_data_count: data.length,
                    points,
                    finite_pixel_count: points.filter((point) => point.pixel).length,
                    contained_pixel_count: points.filter((point) => point.contained).length,
                    rendered_point_count: renderedPointCount,
                    rendered_graphic_types: graphics.map((item) => item?.type || null),
                    display_list_count: displayList.length,
                  };
                }
                """
            )
            assert chart_metrics["series_type"] == "scatter"
            assert chart_metrics["series_data_count"] > 0, "ECharts series 没有候选点"
            assert chart_metrics["finite_pixel_count"] == chart_metrics["series_data_count"]
            assert chart_metrics["contained_pixel_count"] == chart_metrics["series_data_count"]
            if chart_metrics["rendered_point_count"] <= 0:
                print("ECHARTS_DIAGNOSTIC=" + json.dumps(chart_metrics, ensure_ascii=False))
                raise AssertionError("ECharts display list 没有实际绘制点")

            click_point = next(
                (point for point in chart_metrics["points"] if point["novel_id"] == 12),
                None,
            )
            assert click_point and click_point["pixel"], "缺少可点击的实际雷达点"
            box = radar.bounding_box()
            assert box is not None
            page.mouse.click(
                box["x"] + click_point["pixel"][0],
                box["y"] + click_point["pixel"][1],
            )
            page.wait_for_timeout(250)
            selected_rows = page.locator("table tbody tr[aria-selected='true']")
            assert selected_rows.count() == 1
            assert "novel-12" in selected_rows.inner_text()
            detail_headings = page.locator("h2").all_text_contents()
            assert any("novel-12" in heading for heading in detail_headings), "雷达点击未联动详情"

            page.locator("select[aria-label='状态筛选']").select_option("track")
            page.wait_for_timeout(800)
            rows_track = page.locator("table tbody tr").count()

            overflow = page.evaluate(
                "document.documentElement.scrollWidth > window.innerWidth"
            )

            screenshot = OUT_DIR / f"radar-r2-{name}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            report["screenshots"].append(str(screenshot))
            report["checks"].append(
                {
                    "viewport": name,
                    "demo_banner": True,
                    "track_rows": rows_track,
                    "horizontal_overflow": bool(overflow),
                    "console_errors": list(console_errors),
                    "echarts": chart_metrics,
                    "table_to_detail_link": True,
                    "radar_click_to_detail_link": True,
                }
            )
            report["console_errors"].extend(console_errors)
            page.close()
        browser.close()

    (OUT_DIR / "check-report-r2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
