"""Discovery API：只读 runs/candidates + 显式 start/cancel/status。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ...core.config import get_settings
from ...discovery.ranking import GROUP_LABELS, rank_candidates
from ...discovery.runner import DiscoveryBusyError
from ...storage.connection import connect
from ...storage.discovery_repository import DiscoveryRepository
from ...storage.feedback_repository import FeedbackRepository

router = APIRouter()

_repo = DiscoveryRepository()
_feedback_repo = FeedbackRepository()


def _runner(request: Request):
    runner = getattr(request.app.state, "discovery_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="discovery runner unavailable")
    return runner


@router.get("/discovery/runs")
def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        items = _repo.list_runs(conn, limit=limit, offset=offset)
    finally:
        conn.close()
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/discovery/candidates")
def list_candidates(
    run_id: int = Query(ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    novel_id: int | None = Query(default=None, ge=1),
) -> dict:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        run = _repo.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="discovery run not found")
        # WP2 可解释排序：对整次 run 的候选确定性排序后再分页
        all_items = _repo.list_candidates(
            conn, run_id=run_id, limit=1000, offset=0
        )
        novel_ids = [item["novel_id"] for item in all_items]
        inputs = _repo.list_ranking_inputs(
            conn, run_id=run_id, novel_ids=novel_ids
        )
        feedback_map = _feedback_repo.get_summaries_batch(conn, novel_ids=novel_ids)
        ranking_payloads = []
        for item in all_items:
            payload = dict(inputs.get(item["novel_id"], {}))
            payload.update(
                {
                    "bookmarks": item.get("total_bookmarks", 0),
                    "views": item.get("total_view", 0),
                    "feedback": feedback_map.get(item["novel_id"]),
                }
            )
            ranking_payloads.append(payload)
        ranked = rank_candidates(ranking_payloads)
        ranked_by_id = {r.novel_id: r for r in ranked}
        items_by_id = {item["novel_id"]: item for item in all_items}
        ordered = [ranked_by_id[r.novel_id].novel_id for r in ranked]
        # A5：详情按同 novel_id 补充本轮候选原因（不在本轮时返回空列表）
        if novel_id is not None:
            ordered = [candidate_id for candidate_id in ordered if candidate_id == novel_id]
        total = len(ordered)
    finally:
        conn.close()

    result_items = []
    for novel_id in ordered[offset : offset + limit]:
        item = items_by_id.get(novel_id)
        ranked_item = ranked_by_id.get(novel_id)
        if item is None:
            continue
        result_items.append(
            {
                **item,
                "group": ranked_item.group if ranked_item else "observe",
                "group_label": GROUP_LABELS.get(
                    ranked_item.group if ranked_item else "observe", ""
                ),
                "group_reason": ranked_item.group_reason if ranked_item else "",
                "explanations": (
                    list(ranked_item.explanations) if ranked_item else []
                ),
                "score_fields": ranked_item.score_fields if ranked_item else {},
                "filtered_reason": (
                    ranked_item.filtered_reason if ranked_item else None
                ),
            }
        )
    return {
        "items": result_items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/discovery/start")
def start_discovery(
    request: Request,
    tag: str = Query(default="ウマ娘", min_length=1, max_length=100),
) -> dict:
    runner = _runner(request)
    try:
        run_key, started_at = runner.start(tag=tag)
    except DiscoveryBusyError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code}
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "invalid_discovery_tag"}
        ) from exc
    return {
        "run_key": run_key,
        "started_at": started_at,
        "source": runner.source_name,
    }


@router.post("/discovery/cancel")
def cancel_discovery(request: Request) -> dict:
    runner = _runner(request)
    return {"cancelled": runner.cancel()}


@router.get("/discovery/status")
def discovery_status(request: Request) -> dict:
    runner = _runner(request)
    result = runner.status()
    scheduler = getattr(request.app.state, "discovery_scheduler", None)
    result["schedule"] = (
        scheduler.status()
        if scheduler is not None
        else {
            "enabled": False,
            "tag": None,
            "interval_minutes": None,
            "next_run_at": None,
            "last_attempt_at": None,
            "last_error_code": None,
        }
    )
    return result
