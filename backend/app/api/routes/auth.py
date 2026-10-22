"""本地登录助手 API：只返回无敏感值状态。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...auth.service import AuthOperationError, AuthService

router = APIRouter(prefix="/auth")


class StartAuthRequest(BaseModel):
    user_initiated: Literal[True]


class ValidateAuthRequest(BaseModel):
    user_initiated: Literal[True]


class DisconnectAuthRequest(BaseModel):
    confirm: Literal[True]


def _service(request: Request) -> AuthService:
    return request.app.state.auth_service


def _run(operation):
    try:
        return operation()
    except AuthOperationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code},
        ) from None


@router.get("/status")
def get_auth_status(request: Request) -> dict[str, object]:
    return _service(request).status().as_dict()


@router.post("/start")
def start_auth(
    request: Request,
    body: StartAuthRequest,
) -> dict[str, object]:
    del body  # Literal[True] 已确保这是用户显式触发的 POST。
    return _run(_service(request).start).as_dict()


@router.post("/validate")
def validate_auth(
    request: Request,
    body: ValidateAuthRequest,
) -> dict[str, object]:
    """用户显式验证已保存登录；这是唯一会读取凭据的 GET/POST 边界之一。"""
    del body  # Literal[True] 已确保这是用户显式触发的 POST。
    return _run(_service(request).validate).as_dict()


@router.post("/cancel")
def cancel_auth(request: Request) -> dict[str, object]:
    return _run(_service(request).cancel).as_dict()


@router.post("/disconnect")
def disconnect_auth(
    request: Request,
    body: DisconnectAuthRequest,
) -> dict[str, object]:
    del body  # Literal[True] 强制调用方显式确认。
    return _run(_service(request).disconnect).as_dict()
