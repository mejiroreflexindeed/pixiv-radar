"""指标引擎纯函数测试（WP3）。"""

import pytest

from app.scoring.metrics import (
    Evaluation,
    Snapshot,
    ThresholdResult,
    evaluate_growth,
    evaluate_target,
    ewma,
    median_absolute_deviation,
    pair_growth,
    robust_threshold,
    smoothed_bookmark_rate,
)
from app.scoring.params import DEFAULT_PARAMS, PhaseCParams

P = DEFAULT_PARAMS


def snap(novel_id, run_id, captured_at, bookmarks, views, comments=0):
    return Snapshot(novel_id, run_id, captured_at, bookmarks, views, comments)


# ---- 平滑收藏率 ----


def test_smoothed_rate_beta_prior():
    assert smoothed_bookmark_rate(10, 300, P) == pytest.approx(12 / 500)


def test_smoothed_rate_negative_or_missing_is_none():
    assert smoothed_bookmark_rate(-1, 300, P) is None
    assert smoothed_bookmark_rate(10, -1, P) is None
    assert smoothed_bookmark_rate(None, 300, P) is None


def test_small_denominator_not_extreme():
    # 1 阅读 1 收藏 → 3/201 ≈ 1.49%，不会成为 100%
    rate = smoothed_bookmark_rate(1, 1, P)
    assert rate < 0.02


# ---- 相邻对 ----


def test_pair_growth_normal():
    pair = pair_growth(
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T02:00:00+00:00", 12, 500),
        P,
    )
    assert pair.reason is None
    assert pair.hours == pytest.approx(2.0)
    assert pair.view_velocity == pytest.approx(100.0)
    assert pair.bookmark_velocity == pytest.approx(1.0)


def test_pair_same_timestamp_time_regression():
    pair = pair_growth(
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T00:00:00+00:00", 12, 500),
        P,
    )
    assert pair.reason == "time_regression"


def test_pair_count_regression():
    pair = pair_growth(
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T01:00:00+00:00", 8, 290),
        P,
    )
    assert pair.reason == "count_regression"


def test_pair_interval_too_short():
    params = PhaseCParams(min_pair_seconds=3600.0)
    pair = pair_growth(
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T00:30:00+00:00", 12, 500),
        params,
    )
    assert pair.reason == "interval_too_short"


def test_default_minimum_interval_rejects_sub_minute_noise():
    pair = pair_growth(
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T00:00:59+00:00", 12, 500),
        P,
    )
    assert P.min_pair_seconds == 60.0
    assert pair.reason == "interval_too_short"


# ---- EWMA ----


def test_ewma_seed_and_update():
    assert ewma([100.0], P) == pytest.approx(100.0)
    value = ewma([100.0, 200.0], P)
    assert value == pytest.approx(0.35 * 200.0 + 0.65 * 100.0)
    assert ewma([], P) is None


# ---- 稳健门限 ----


def test_threshold_fallback_when_few_samples():
    result = robust_threshold([10.0, 20.0], P)
    assert result.source == "fallback"
    assert result.threshold == P.fallback_growth_threshold
    assert result.sample_count == 2


def test_threshold_median_mad():
    signals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    result = robust_threshold(signals, P)
    assert result.source == "median_mad"
    assert result.threshold == pytest.approx(5.5 + 2.0 * 2.5)
    assert result.sample_count == 10


def test_threshold_mad_zero_uses_fallback():
    signals = [5.0] * 10
    result = robust_threshold(signals, P)
    assert result.source == "fallback_mad_zero"
    assert result.threshold == P.fallback_growth_threshold


def test_mad_calculation():
    assert median_absolute_deviation([1.0, 2.0, 3.0]) == pytest.approx(1.0)


# ---- 目标评估 ----


def test_evaluate_empty_snapshots_missing_data():
    ev = evaluate_target([], params=P, threshold=None)
    assert ev.result == "unknown"
    assert ev.reason_codes == ("missing_data",)


def test_evaluate_insufficient_exposure():
    ev = evaluate_target(
        [snap(1, 1, "2026-08-30T00:00:00+00:00", 5, 50)],
        params=P, threshold=None,
    )
    assert ev.result == "unknown"
    assert ev.reason_codes == ("insufficient_exposure",)
    assert ev.exposure_eligible is False


def test_evaluate_negative_counts():
    ev = evaluate_target(
        [snap(1, 1, "2026-08-30T00:00:00+00:00", -1, 300)],
        params=P, threshold=None,
    )
    assert ev.result == "unknown"
    assert ev.reason_codes == ("count_regression",)


def test_bookmark_exceeds_views_is_recorded_without_clamping():
    history = [
        snap(1, 1, "2026-08-30T00:00:00+00:00", 301, 300),
        snap(1, 2, "2026-08-30T01:00:00+00:00", 501, 500),
    ]
    ev = evaluate_target(
        history, params=P, threshold=ThresholdResult(5.0, "fallback", 1)
    )
    assert ev.result == "qualified"
    assert "bookmark_exceeds_views" in ev.reason_codes
    assert ev.smoothed_bookmark_rate == pytest.approx(503 / 700)


def test_evaluate_single_snapshot_no_growth():
    ev = evaluate_target(
        [snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300)],
        params=P, threshold=None,
    )
    assert ev.result == "unknown"
    assert "no_valid_growth" in ev.reason_codes
    assert ev.exposure_eligible is True


def test_evaluate_qualified_and_not_qualified():
    history = [
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T01:00:00+00:00", 12, 500),
    ]
    threshold_high = ThresholdResult(1000.0, "fallback", 1)
    threshold_low = ThresholdResult(5.0, "fallback", 1)
    ev_high = evaluate_target(history, params=P, threshold=threshold_high)
    ev_low = evaluate_target(history, params=P, threshold=threshold_low)
    assert ev_high.result == "not_qualified"
    assert ev_low.result == "qualified"
    assert ev_low.ewma_view_velocity == pytest.approx(200.0)
    assert ev_low.view_velocity == pytest.approx(200.0)


def test_evaluate_mixed_invalid_pairs_skip_in_ewma():
    history = [
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T01:00:00+00:00", 9, 290),  # 倒退 → 无效
        snap(1, 3, "2026-08-30T02:00:00+00:00", 11, 310),  # 相对 r2 有效
    ]
    growth = evaluate_growth(history, P)
    # 只有 (r2->r3) 一个有效对：velocity = (310-290)/1 = 20
    assert growth.ewma_view_velocity == pytest.approx(20.0)
    assert "count_regression" in growth.reasons
    assert growth.last_pair_reason is None  # 最近一对有效


def test_evaluate_growth_regression_only_is_unknown():
    history = [
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T01:00:00+00:00", 9, 290),
    ]
    ev = evaluate_target(
        history, params=P, threshold=ThresholdResult(5.0, "fallback", 1)
    )
    assert ev.result == "unknown"
    assert "no_valid_growth" in ev.reason_codes
    assert "count_regression" in ev.reason_codes


def test_latest_pair_regression_is_unknown_even_with_earlier_valid():
    history = [
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T01:00:00+00:00", 12, 500),  # 有效对
        snap(1, 3, "2026-08-30T02:00:00+00:00", 11, 490),  # 最近一对倒退
    ]
    ev = evaluate_target(
        history, params=P, threshold=ThresholdResult(5.0, "fallback", 1)
    )
    assert ev.result == "unknown"
    assert "count_regression" in ev.reason_codes
    # EWMA 仍记录自此前有效对（可解释性），但不参与晋级判定
    assert ev.ewma_view_velocity == pytest.approx(200.0)
    assert ev.threshold is None


def test_latest_pair_time_regression_is_unknown():
    history = [
        snap(1, 1, "2026-08-30T00:00:00+00:00", 10, 300),
        snap(1, 2, "2026-08-30T01:00:00+00:00", 12, 500),
        snap(1, 3, "2026-08-30T01:00:00+00:00", 13, 520),  # 时间倒退
    ]
    ev = evaluate_target(
        history, params=P, threshold=ThresholdResult(5.0, "fallback", 1)
    )
    assert ev.result == "unknown"
    assert "time_regression" in ev.reason_codes
