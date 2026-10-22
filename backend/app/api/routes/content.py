"""第二层正文轻量分析 API；响应永不包含正文或凭据。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ...content.service import (
    ContentAnalysisService,
    ContentAnalysisUnavailable,
    ContentOperationConflict,
    ContentOperationPaused,
    ContentRateLimited,
    ContentTargetNotEligible,
    ContentTargetNotFound,
)
from ...pixiv.exceptions import (
    AuthFailed,
    MalformedResponse,
    NotFound,
    RateLimited,
    TemporaryFailure,
)

router = APIRouter()


def _service(request: Request) -> ContentAnalysisService:
    return request.app.state.content_analysis_service


def _payload(item: dict, *, cached: bool | None = None) -> dict:
    # content_hash 仅用于内部缓存一致性，不属于 UI 契约。
    result = {key: value for key, value in item.items() if key != "content_hash"}
    if cached is not None:
        result["cached"] = cached
    return result


@router.get("/content/status")
def content_status(request: Request) -> dict:
    service = _service(request)
    return {"available": service.available}


@router.get("/content/{novel_id}")
def get_content_analysis(novel_id: int, request: Request) -> dict:
    try:
        item = _service(request).get(novel_id)
    except ContentTargetNotFound:
        raise HTTPException(404, detail={"code": "target_not_found"}) from None
    if item is None:
        raise HTTPException(404, detail={"code": "analysis_not_found"})
    return _payload(item)


@router.get("/content/{novel_id}/similar")
def get_similar_content(
    novel_id: int,
    request: Request,
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    try:
        items = _service(request).similar(novel_id, limit=limit)
    except ContentTargetNotFound:
        raise HTTPException(404, detail={"code": "target_not_found"}) from None
    return {
        "items": items,
        "method": "char-ngram-tfidf-v1",
        "note": "text_similarity_not_quality_or_semantic_judgment",
    }


@router.post("/content/{novel_id}/analyze")
def analyze_content(
    novel_id: int,
    request: Request,
    force: bool = Query(default=False),
) -> dict:
    try:
        item, cached = _service(request).analyze(novel_id, force=force)
    except ContentTargetNotFound:
        raise HTTPException(404, detail={"code": "target_not_found"}) from None
    except ContentTargetNotEligible:
        raise HTTPException(409, detail={"code": "target_not_in_track"}) from None
    except ContentOperationConflict:
        raise HTTPException(
            409, detail={"code": "pixiv_operation_in_progress"}
        ) from None
    except ContentOperationPaused as exc:
        raise HTTPException(
            503,
            detail={
                "code": "pixiv_operation_paused",
                "reason": exc.reason,
            },
        ) from None
    except ContentRateLimited as exc:
        detail = {"code": "pixiv_rate_limited"}
        if exc.cooldown_seconds is not None:
            detail["cooldown_seconds"] = max(0.0, exc.cooldown_seconds)
        raise HTTPException(429, detail=detail) from None
    except ContentAnalysisUnavailable:
        raise HTTPException(503, detail={"code": "content_analysis_unavailable"}) from None
    except AuthFailed:
        raise HTTPException(401, detail={"code": "pixiv_login_required"}) from None
    except RateLimited as exc:
        detail = {"code": "pixiv_rate_limited"}
        if exc.cooldown_seconds is not None:
            detail["cooldown_seconds"] = max(0.0, exc.cooldown_seconds)
        raise HTTPException(429, detail=detail) from None
    except NotFound:
        raise HTTPException(404, detail={"code": "pixiv_content_not_found"}) from None
    except TemporaryFailure as exc:
        # 会话失效与共享门暂停给出准确受控码，不混进“稍后重试”。
        if "pixiv_session_invalid" in str(exc):
            raise HTTPException(
                503, detail={"code": "pixiv_session_invalid"}
            ) from None
        if "pixiv_operation_paused" in str(exc):
            reason = str(exc).split(":", 1)[1] if ":" in str(exc) else None
            raise HTTPException(
                503,
                detail={"code": "pixiv_operation_paused", "reason": reason},
            ) from None
        raise HTTPException(503, detail={"code": "pixiv_content_unavailable"}) from None
    except MalformedResponse:
        raise HTTPException(503, detail={"code": "pixiv_content_unavailable"}) from None
    return _payload(item, cached=cached)
