"""候选召回领域包（Search-1）：类型、预算、去重合并与离线服务。

不依赖 FastAPI 与 PixivPy 对象；本包只做"从哪些来源把候选找回并留痕"，
不评判个人文风，不复制阶段 C 评分。
"""

import hashlib
import json
import re

SOURCE_KINDS = (
    "exact_tag",
    "partial_tag",
    "keyword",
    "novel_new",
    "novel_recommended",
    "author_novels",
    "series_novels",
    "novel_follow",
)

# 预算分组（契约 §7：核心 60% / 扩展 25% / 探索 15%）
CORE_SOURCES = ("exact_tag", "partial_tag", "keyword")
EXTENSION_SOURCES = (
    "author_novels",
    "series_novels",
    "novel_recommended",
    "novel_follow",
)
EXPLORE_SOURCES = ("novel_new",)

TOTAL_REQUEST_BUDGET = 8
PER_REQUEST_LIMIT = 30
MAX_UNIQUE_CANDIDATES = 200
QUERY_KEY_MAX = 200
QUERY_TEXT_MAX = 500

_QUERY_KEY_RE = re.compile(r"^[\w:-]{1,200}$", re.UNICODE)

# 文本合并来源优先级（高 → 低）
SOURCE_PRIORITY = (
    "exact_tag",
    "partial_tag",
    "keyword",
    "novel_new",
    "novel_recommended",
    "author_novels",
    "series_novels",
    "novel_follow",
)


def make_query_key(source_kind: str, text: str | None = None, entity_id: int | None = None) -> str:
    """Return a stable, storage-safe key while preserving old simple keys.

    Existing hand-authored keys such as ``exact_tag:ウマ娘`` remain compatible.
    Search text containing whitespace or punctuation is represented by a
    deterministic digest so it is not confused with the storage key grammar.
    """
    suffix = text if text is not None else (str(entity_id) if entity_id is not None else "all")
    legacy = f"{source_kind}:{suffix}"
    if _QUERY_KEY_RE.fullmatch(legacy):
        return legacy
    canonical = json.dumps(
        {"entity_id": entity_id, "source_kind": source_kind, "text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{source_kind}:{hashlib.sha256(canonical).hexdigest()}"


def budget_group(source_kind: str) -> str:
    if source_kind in CORE_SOURCES:
        return "core"
    if source_kind in EXTENSION_SOURCES:
        return "extension"
    if source_kind in EXPLORE_SOURCES:
        return "explore"
    raise ValueError(f"unknown source_kind: {source_kind!r}")
