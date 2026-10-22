"""固定请求预算规划器（纯函数，确定性）。

契约见 docs/PHASE_E_R1_SEARCH_1_CONTRACT.md §7：
最多 8 次来源请求、每次 ≤30 结果、单次运行 ≤200 唯一候选；
组比例核心 60% / 扩展 25% / 探索 15%；舍入与转赠规则冻结并测试。
"""

from __future__ import annotations

from . import (
    TOTAL_REQUEST_BUDGET,
    budget_group,
)
from .types import DiscoveryPlan, DiscoveryQuery
from .types import validate_discovery_query

_GROUP_ORDER = ("core", "extension", "explore")
_GROUP_RATIO = {"core": 0.60, "extension": 0.25, "explore": 0.15}


def plan_budget(
    queries: list[DiscoveryQuery],
    *,
    total: int = TOTAL_REQUEST_BUDGET,
) -> DiscoveryPlan:
    """按固定比例与优先级选择启用的查询，总量不超过 total。"""
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 8:
        raise ValueError("total must be an integer from 0 to 8")
    for query in queries:
        validate_discovery_query(query)
    enabled = [q for q in queries if q.enabled]

    groups: dict[str, list[DiscoveryQuery]] = {"core": [], "extension": [], "explore": []}
    for query in enabled:
        groups[budget_group(query.source_kind)].append(query)

    # 各组 floor 配额（不超过该组可用查询数）
    quotas: dict[str, int] = {}
    for name in _GROUP_ORDER:
        quotas[name] = min(len(groups[name]), int(total * _GROUP_RATIO[name]))

    # 余数按优先级逐组 +1（同优先级顺序转赠，不临时无限扩容）
    remaining = total - sum(quotas.values())
    for name in _GROUP_ORDER:
        while remaining > 0 and quotas[name] < len(groups[name]):
            quotas[name] += 1
            remaining -= 1

    selected: list[DiscoveryQuery] = []
    for name in _GROUP_ORDER:
        selected.extend(groups[name][: quotas[name]])

    return DiscoveryPlan(
        queries=tuple(selected),
        planned_requests=len(selected),
    )
