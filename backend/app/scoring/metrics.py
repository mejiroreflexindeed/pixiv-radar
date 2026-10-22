"""阶段 C 指标引擎（纯函数）。

无数据库依赖、不调用系统时间；时间与参数全部显式注入。
契约见 docs/PHASE_C_CONTRACT.md §3。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median

from .params import PhaseCParams


@dataclass(frozen=True)
class Snapshot:
    novel_id: int
    scan_run_id: int
    captured_at: str
    total_bookmarks: int
    total_view: int
    total_comments: int


@dataclass(frozen=True)
class PairGrowth:
    hours: float
    view_velocity: float
    bookmark_velocity: float
    reason: str | None = None  # None=有效；否则为错误码


@dataclass(frozen=True)
class ThresholdResult:
    threshold: float
    source: str  # median_mad / fallback / fallback_mad_zero
    sample_count: int


@dataclass(frozen=True)
class Evaluation:
    novel_id: int
    scan_run_id: int
    exposure_eligible: bool
    smoothed_bookmark_rate: float | None
    view_velocity: float | None
    bookmark_velocity: float | None
    ewma_view_velocity: float | None
    threshold: float | None
    threshold_source: str | None
    sample_count: int | None
    result: str  # qualified / not_qualified / unknown
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class GrowthEval:
    view_velocity: float | None
    bookmark_velocity: float | None
    ewma_view_velocity: float | None
    last_pair_reason: str | None  # 最近一对的原因；None=有效
    reasons: tuple[str, ...]


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def smoothed_bookmark_rate(
    bookmarks: int | None, views: int | None, params: PhaseCParams
) -> float | None:
    if bookmarks is None or views is None or bookmarks < 0 or views < 0:
        return None
    return (bookmarks + params.beta_prior_bookmarks) / (
        views + params.beta_prior_views
    )


def pair_growth(
    prev: Snapshot, cur: Snapshot, params: PhaseCParams
) -> PairGrowth:
    try:
        delta = (_parse(cur.captured_at) - _parse(prev.captured_at)).total_seconds()
    except ValueError:
        return PairGrowth(0.0, 0.0, 0.0, reason="time_regression")
    if delta <= 0:
        return PairGrowth(0.0, 0.0, 0.0, reason="time_regression")
    if delta < params.min_pair_seconds:
        return PairGrowth(delta / 3600.0, 0.0, 0.0, reason="interval_too_short")
    d_views = cur.total_view - prev.total_view
    d_bookmarks = cur.total_bookmarks - prev.total_bookmarks
    if d_views < 0 or d_bookmarks < 0:
        return PairGrowth(
            delta / 3600.0, 0.0, 0.0, reason="count_regression"
        )
    hours = delta / 3600.0
    return PairGrowth(hours, d_views / hours, d_bookmarks / hours)


def ewma(velocities: list[float], params: PhaseCParams) -> float | None:
    if not velocities:
        return None
    value = velocities[0]
    for v in velocities[1:]:
        value = params.ewma_alpha * v + (1 - params.ewma_alpha) * value
    return value


def median_absolute_deviation(values: list[float]) -> float:
    med = median(values)
    return median([abs(x - med) for x in values])


def robust_threshold(
    signals: list[float], params: PhaseCParams
) -> ThresholdResult:
    n = len(signals)
    if n < params.robust_threshold_min_samples:
        return ThresholdResult(params.fallback_growth_threshold, "fallback", n)
    med = median(signals)
    mad = median_absolute_deviation(signals)
    if mad == 0:
        return ThresholdResult(
            params.fallback_growth_threshold, "fallback_mad_zero", n
        )
    return ThresholdResult(
        med + params.robust_threshold_k * mad, "median_mad", n
    )


def evaluate_growth(
    snapshots: list[Snapshot], params: PhaseCParams
) -> GrowthEval:
    """按时间序（captured_at, scan_run_id 升序）计算增长。

    无效对不参与 EWMA、不重置；无有效对时信号为 None。
    最近一对无效（计数倒退/时间倒退等）由调用方据此判 unknown。
    """
    valid: list[float] = []
    last_valid: PairGrowth | None = None
    reasons: set[str] = set()
    last_pair: PairGrowth | None = None
    for prev, cur in zip(snapshots, snapshots[1:]):
        pair = pair_growth(prev, cur, params)
        last_pair = pair
        if pair.reason is None:
            valid.append(pair.view_velocity)
            last_valid = pair
        else:
            reasons.add(pair.reason)
    signal = ewma(valid, params)
    return GrowthEval(
        view_velocity=last_valid.view_velocity if last_valid else None,
        bookmark_velocity=last_valid.bookmark_velocity if last_valid else None,
        ewma_view_velocity=signal,
        last_pair_reason=last_pair.reason if last_pair else None,
        reasons=tuple(sorted(reasons)),
    )


def evaluate_target(
    snapshots: list[Snapshot],
    *,
    params: PhaseCParams,
    threshold: ThresholdResult | None,
) -> Evaluation:
    """评估单个目标（snapshots 含当前快照，按时间升序）。"""
    if not snapshots:
        return Evaluation(
            novel_id=0,
            scan_run_id=0,
            exposure_eligible=False,
            smoothed_bookmark_rate=None,
            view_velocity=None,
            bookmark_velocity=None,
            ewma_view_velocity=None,
            threshold=None,
            threshold_source=None,
            sample_count=None,
            result="unknown",
            reason_codes=("missing_data",),
        )
    latest = snapshots[-1]
    quality_codes: tuple[str, ...] = ()
    if (
        latest.total_bookmarks is not None
        and latest.total_view is not None
        and latest.total_bookmarks > latest.total_view
    ):
        # 不猜测或修正上游语义，只留下可解释的数据质量 reason。
        quality_codes = ("bookmark_exceeds_views",)
    rate = smoothed_bookmark_rate(
        latest.total_bookmarks, latest.total_view, params
    )
    if rate is None:
        code = (
            "count_regression"
            if (latest.total_bookmarks < 0 or latest.total_view < 0)
            else "missing_data"
        )
        return Evaluation(
            novel_id=latest.novel_id,
            scan_run_id=latest.scan_run_id,
            exposure_eligible=False,
            smoothed_bookmark_rate=None,
            view_velocity=None,
            bookmark_velocity=None,
            ewma_view_velocity=None,
            threshold=None,
            threshold_source=None,
            sample_count=None,
            result="unknown",
            reason_codes=(code,),
        )
    if latest.total_view < params.min_views:
        return Evaluation(
            novel_id=latest.novel_id,
            scan_run_id=latest.scan_run_id,
            exposure_eligible=False,
            smoothed_bookmark_rate=rate,
            view_velocity=None,
            bookmark_velocity=None,
            ewma_view_velocity=None,
            threshold=None,
            threshold_source=None,
            sample_count=None,
            result="unknown",
            reason_codes=quality_codes + ("insufficient_exposure",),
        )
    growth = evaluate_growth(snapshots, params)
    if growth.ewma_view_velocity is None:
        codes = quality_codes + ("no_valid_growth",) + growth.reasons
        return Evaluation(
            novel_id=latest.novel_id,
            scan_run_id=latest.scan_run_id,
            exposure_eligible=True,
            smoothed_bookmark_rate=rate,
            view_velocity=None,
            bookmark_velocity=None,
            ewma_view_velocity=None,
            threshold=None,
            threshold_source=None,
            sample_count=None,
            result="unknown",
            reason_codes=codes,
        )
    # 最近一对无效（计数/时间倒退等）：当前数据点不可信 → unknown，
    # EWMA 仍基于此前的有效对记录（供可解释性），但不得据此晋级/降级。
    if growth.last_pair_reason is not None:
        codes = quality_codes + (growth.last_pair_reason,) + growth.reasons
        return Evaluation(
            novel_id=latest.novel_id,
            scan_run_id=latest.scan_run_id,
            exposure_eligible=True,
            smoothed_bookmark_rate=rate,
            view_velocity=growth.view_velocity,
            bookmark_velocity=growth.bookmark_velocity,
            ewma_view_velocity=growth.ewma_view_velocity,
            threshold=None,
            threshold_source=None,
            sample_count=None,
            result="unknown",
            reason_codes=codes,
        )
    assert threshold is not None
    result = "qualified" if growth.ewma_view_velocity >= threshold.threshold else "not_qualified"
    return Evaluation(
        novel_id=latest.novel_id,
        scan_run_id=latest.scan_run_id,
        exposure_eligible=True,
        smoothed_bookmark_rate=rate,
        view_velocity=growth.view_velocity,
        bookmark_velocity=growth.bookmark_velocity,
        ewma_view_velocity=growth.ewma_view_velocity,
        threshold=threshold.threshold,
        threshold_source=threshold.source,
        sample_count=threshold.sample_count,
        result=result,
        reason_codes=quality_codes + growth.reasons,
    )
