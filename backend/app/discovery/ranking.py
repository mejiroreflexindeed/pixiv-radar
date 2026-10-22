"""可解释三段式排序（WP2，纯函数，不改迁移、不引入学习模型）。

三段：① 硬过滤（非法 id/空壳/不可见/非法计数，显示原因）；
② 发现优先级（新鲜度、相对收藏信号、近几次增长、来源多样性、冷启动观察、
个人反馈修正——全部为可解释字段）；③ 分组显示（小众新发现 / 稳定追踪 /
热门参考 / 待观察）。热度不是质量；click/dwell 不参与显式偏好修正；
数据不足不伪造结论。排序与输入顺序无关。
"""

from __future__ import annotations

from dataclasses import dataclass

GROUP_ORDER = ("small_gem", "stable_track", "popular_reference", "observe", "filtered")

GROUP_LABELS = {
    "small_gem": "小众新发现",
    "stable_track": "稳定追踪",
    "popular_reference": "热门参考",
    "observe": "待观察",
    "filtered": "已过滤",
}

MIN_SNAPSHOTS_TO_COMPARE = 3
POPULAR_VIEWS = 1000
GEM_MIN_RATE = 0.02
GEM_MIN_VELOCITY = 1.0
RATE_PRIOR = 200


@dataclass(frozen=True)
class RankedCandidate:
    novel_id: int
    group: str
    group_reason: str
    explanations: tuple[str, ...]
    score_fields: dict
    filtered_reason: str | None


def _sorted_snapshots(snapshots: list[dict]) -> list[dict]:
    return sorted(snapshots, key=lambda s: (s["captured_at"], s.get("scan_run_id", 0)))


def _hours_between(first: dict, last: dict) -> float:
    from datetime import datetime

    try:
        delta = (
            datetime.fromisoformat(last["captured_at"])
            - datetime.fromisoformat(first["captured_at"])
        ).total_seconds()
    except ValueError:
        return 0.0
    return delta / 3600.0


def _rank_one(candidate: dict) -> RankedCandidate:
    novel_id = int(candidate["novel_id"])
    title = str(candidate.get("title") or "").strip()
    bookmarks = int(candidate.get("bookmarks") or 0)
    views = int(candidate.get("views") or 0)
    visible = bool(candidate.get("visible", True))
    snapshots = _sorted_snapshots(list(candidate.get("snapshots") or []))
    source_kinds = sorted(set(candidate.get("source_kinds") or []))
    feedback = candidate.get("feedback")  # effective_feedback | None

    # ① 硬过滤（显示原因）
    if novel_id <= 0:
        return RankedCandidate(
            novel_id, "filtered", "非法目标 ID",
            (), {}, "invalid_id",
        )
    if not title:
        return RankedCandidate(
            novel_id, "filtered", "空壳候选（无标题）",
            (), {}, "empty_title",
        )
    if not visible:
        return RankedCandidate(
            novel_id, "filtered", "不可见候选",
            (), {}, "invisible",
        )
    if bookmarks < 0 or views < 0:
        return RankedCandidate(
            novel_id, "filtered", "非法计数",
            (), {}, "invalid_counts",
        )

    rate = bookmarks / (views + RATE_PRIOR)
    n = len(snapshots)
    velocity: float | None = None
    if n >= 2:
        hours = _hours_between(snapshots[0], snapshots[-1])
        if hours > 0:
            velocity = (snapshots[-1]["views"] - snapshots[0]["views"]) / hours

    # ② 可解释字段
    explanations: list[str] = []
    if source_kinds:
        explanations.append(
            f"{len(source_kinds)} 个来源发现（{'、'.join(source_kinds)}）"
        )
    explanations.append(f"收藏/阅读比 {rate:.4f}")
    if velocity is not None:
        explanations.append(
            f"近 {n} 次快照阅读 {snapshots[0]['views']}→{snapshots[-1]['views']}"
            f"（每小时 {velocity:+.2f}）"
        )
    if feedback == "good":
        explanations.append("已标记好文（显式反馈，排序解释优先）")
    elif feedback == "exclude":
        explanations.append("已标记排除（显式反馈，排序解释降低）")

    # ③ 分组
    if n < MIN_SNAPSHOTS_TO_COMPARE:
        group = "observe"
        group_reason = f"数据不足（{n}/{MIN_SNAPSHOTS_TO_COMPARE} 次快照），先观察不比较"
    elif views >= POPULAR_VIEWS:
        group = "popular_reference"
        group_reason = "高阅读量热门参考（热度不是质量）"
    elif rate >= GEM_MIN_RATE and (velocity or 0) >= GEM_MIN_VELOCITY:
        group = "small_gem"
        group_reason = "小众新发现：阅读量不高但相对收藏与增长信号较强"
    else:
        group = "stable_track"
        group_reason = "稳定追踪：已越过观察期，未命中小众/热门分组"

    score_fields = {
        "rate": round(rate, 4),
        "velocity": round(velocity, 2) if velocity is not None else None,
        "snapshots": n,
        "sources": len(source_kinds),
        "feedback": feedback,
    }
    return RankedCandidate(
        novel_id, group, group_reason, tuple(explanations), score_fields, None
    )


def rank_candidates(candidates: list[dict]) -> list[RankedCandidate]:
    """确定性排序：分组顺序 → 反馈修正 → 相对信号 → 增速 → novel_id。"""
    ranked = [_rank_one(c) for c in candidates]
    ranked.sort(
        key=lambda r: (
            GROUP_ORDER.index(r.group),
            -({"good": 1, "exclude": -1}.get(r.score_fields.get("feedback"), 0)),
            -(r.score_fields.get("rate") or 0),
            -(r.score_fields.get("velocity") or 0),
            r.novel_id,
        )
    )
    return ranked
