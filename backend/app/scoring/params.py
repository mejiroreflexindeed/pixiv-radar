"""阶段 C 参数（不可变，默认值见 docs/PHASE_C_CONTRACT.md §2）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhaseCParams:
    min_views: int = 200
    cold_start_min_observations: int = 3
    beta_prior_bookmarks: float = 2.0
    beta_prior_views: float = 200.0
    ewma_alpha: float = 0.35
    robust_threshold_min_samples: int = 10
    robust_threshold_k: float = 2.0
    fallback_growth_threshold: float = 5.0
    promotion_streak: int = 2
    demotion_streak: int = 3
    # 小于一分钟的相邻采样默认视为噪音；仍可在测试/后续配置中覆盖。
    min_pair_seconds: float = 60.0


DEFAULT_PARAMS = PhaseCParams()
