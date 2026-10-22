"""固定预算规划器测试（契约 §7：8/30/200 上限、60/25/15 舍入与转赠）。"""

import pytest

from app.discovery import (
    MAX_UNIQUE_CANDIDATES,
    PER_REQUEST_LIMIT,
    TOTAL_REQUEST_BUDGET,
    make_query_key,
)
from app.discovery.budget import plan_budget
from app.discovery.types import DiscoveryQuery


def q(source_kind, text=None, entity_id=None, enabled=True):
    return DiscoveryQuery(
        query_key=make_query_key(source_kind, text, entity_id),
        source_kind=source_kind,
        text=text,
        entity_id=entity_id,
        enabled=enabled,
    )


def test_total_never_exceeds_budget():
    queries = [q("exact_tag", f"t{i}") for i in range(20)]
    plan = plan_budget(queries)
    assert plan.planned_requests <= TOTAL_REQUEST_BUDGET
    assert plan.planned_requests == TOTAL_REQUEST_BUDGET


def test_per_request_and_unique_limits_are_constants():
    assert PER_REQUEST_LIMIT == 30
    assert MAX_UNIQUE_CANDIDATES == 200


def test_ratio_rounding_floor_and_remainder():
    # 4 core + 3 extension + 3 explore 全有可用查询
    queries = (
        [q("exact_tag", f"c{i}") for i in range(4)]
        + [q("author_novels", entity_id=100 + i) for i in range(3)]
        + [q("novel_new") for _ in range(3)]
    )
    plan = plan_budget(queries)
    kinds = [x.source_kind for x in plan.queries]
    # floor: core 4 / extension 2 / explore 1 = 7；余数 1 按优先级：core 已满
    # → extension 3；explore 保持 1
    assert kinds.count("exact_tag") == 4
    assert kinds.count("author_novels") == 3
    assert kinds.count("novel_new") == 1
    assert plan.planned_requests == 8


def test_group_shortage_transfers_by_priority():
    # 无 core 查询：8 全部转给 extension/explore（扩展优先）
    queries = [q("author_novels", entity_id=i) for i in range(10)]
    plan = plan_budget(queries)
    assert plan.planned_requests == 8
    assert all(x.source_kind == "author_novels" for x in plan.queries)


def test_disabled_queries_excluded():
    queries = [q("exact_tag", f"t{i}", enabled=(i % 2 == 0)) for i in range(10)]
    plan = plan_budget(queries)
    assert all(x.enabled for x in plan.queries)
    assert plan.planned_requests <= TOTAL_REQUEST_BUDGET


def test_deterministic_output():
    queries = [q("exact_tag", f"t{i}") for i in range(9)]
    a = plan_budget(queries)
    b = plan_budget(queries)
    assert a == b


def test_unknown_source_kind_rejected():
    with pytest.raises(ValueError):
        plan_budget([q("evil_source")])


@pytest.mark.parametrize("bad", [True, -1, 9, 1.5, "8"])
def test_total_budget_is_strictly_bounded(bad):
    with pytest.raises(ValueError):
        plan_budget([], total=bad)


@pytest.mark.parametrize("bad", [True, 0, -1, 31, 1.5, "30"])
def test_query_limit_is_strictly_bounded(bad):
    query = DiscoveryQuery(
        query_key="exact_tag:test", source_kind="exact_tag", limit=bad
    )
    with pytest.raises(ValueError):
        plan_budget([query])


def test_query_key_hashes_unsafe_search_text_stably():
    a = make_query_key("exact_tag", "foo bar / 日文")
    b = make_query_key("exact_tag", "foo bar / 日文")
    assert a == b
    assert " " not in a and "/" not in a
    assert a.startswith("exact_tag:")
