"""Discovery 领域类型：不依赖 FastAPI 与 PixivPy 对象。"""

from __future__ import annotations

from dataclasses import dataclass

from ..pixiv.adapter import NovelSummary
import re

from . import PER_REQUEST_LIMIT, QUERY_TEXT_MAX, SOURCE_KINDS

_QUERY_KEY_RE = re.compile(r"^[\w:-]{1,200}$", re.UNICODE)


def validate_query_key(value: str) -> str:
    """Validate the persisted query-key grammar without touching a database."""
    if not isinstance(value, str) or not _QUERY_KEY_RE.fullmatch(value):
        raise ValueError(f"invalid query_key: {value!r}")
    return value


def validate_discovery_query(query: "DiscoveryQuery") -> "DiscoveryQuery":
    if query.source_kind not in SOURCE_KINDS:
        raise ValueError(f"invalid source_kind: {query.source_kind!r}")
    validate_query_key(query.query_key)
    if query.text is not None and (
        not isinstance(query.text, str) or len(query.text) > QUERY_TEXT_MAX
    ):
        raise ValueError("query text is invalid or exceeds 500 chars")
    if query.entity_id is not None and (
        isinstance(query.entity_id, bool)
        or not isinstance(query.entity_id, int)
        or query.entity_id < 0
    ):
        raise ValueError("entity_id must be a non-negative integer")
    if query.search_target is not None and (
        not isinstance(query.search_target, str)
        or not query.search_target
        or len(query.search_target) > 100
    ):
        raise ValueError("search_target is invalid")
    if (
        isinstance(query.limit, bool)
        or not isinstance(query.limit, int)
        or not 1 <= query.limit <= PER_REQUEST_LIMIT
    ):
        raise ValueError("limit must be an integer from 1 to 30")
    if not isinstance(query.enabled, bool):
        raise ValueError("enabled must be boolean")
    return query


@dataclass(frozen=True)
class DiscoveryQuery:
    """一次来源查询的稳定配置。

    query_key 必须稳定（同内容不同时间产生相同 key）；source_kind 受控枚举；
    禁止用 callable、SQL 片段或 URL 作为配置。
    """

    query_key: str
    source_kind: str
    text: str | None = None
    entity_id: int | None = None
    search_target: str | None = None
    limit: int = PER_REQUEST_LIMIT
    enabled: bool = True



@dataclass(frozen=True)
class DiscoveryPlan:
    queries: tuple[DiscoveryQuery, ...]  # 已按执行顺序
    planned_requests: int


@dataclass(frozen=True)
class DiscoveryPage:
    items: tuple[NovelSummary, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class DiscoveryBatch:
    query: DiscoveryQuery
    page: DiscoveryPage


@dataclass(frozen=True)
class MergedSighting:
    novel_id: int
    source_kind: str
    query_key: str
    query_text: str | None
    source_rank: int
    discovered_at: str


@dataclass(frozen=True)
class MergedCandidate:
    novel: NovelSummary  # 合并后元数据
    sightings: tuple[MergedSighting, ...]


@dataclass(frozen=True)
class DiscoveryOutcome:
    run_id: int
    status: str  # succeeded / partial / failed / cancelled
    planned_requests: int
    executed_requests: int
    results_seen: int
    unique_candidates: int
    error_code: str | None = None
