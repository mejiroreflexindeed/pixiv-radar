"""GpptSupervisedAuthorizationProvider（可选模块）。

把 Login-1A 的 ``AuthorizationProvider`` 接到隔离 worker 子进程里的受监督
可见浏览器登录（gppt 空凭据模式：密码/2FA 只在 Pixiv 页面输入）。

安全边界：
- 本模块在缺少 gppt 依赖或浏览器 runtime 时仍可 import；``available`` 返回
  False，UI 保持诚实禁用。**绝不 import gppt**（兼容补丁只存在于 worker 子进程）。
- 启动只来自 ``POST /api/auth/start``；页面加载/轮询绝不自动打开浏览器。
- worker 必须显式空凭据 + headless=False；``GPPT_CONFIG_DIR`` 固定到工作区
  空目录（纵深防御）；真实登录路径由 ``RADAR_AUTH_ALLOW_REAL=1`` 门禁保护，
  OFFLINE 恒为 0。
- worker 结果只含固定状态码；上游异常正文、OAuth 响应、页面 URL 一律不进
  API/UI/日志。
- 完成回调必须回显 Login-1A 的 state，由 AuthService 做最终单次校验。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Callable

from ..runtime_paths import PLAYWRIGHT_BROWSERS_PATH
from ..service import (
    AuthOperationError,
    AuthState,
    AuthorizationProvider,
    ProviderFailure,
    ProviderLaunch,
    ProviderRequest,
)
from ..worker import WorkerResult, WorkerSupervisor, real_login_gate_enabled

DEFAULT_WORKER_COMMAND = [sys.executable, "-m", "app.auth.worker_entry", "--mode", "gppt"]

_WORKER_FAILURE_MAP: dict[str, tuple[AuthState, str]] = {
    "authorization_cancelled": (
        AuthState.USER_ACTION_REQUIRED, "authorization_cancelled",
    ),
    "authorization_timeout": (
        AuthState.USER_ACTION_REQUIRED, "authorization_timeout",
    ),
    "browser_closed": (
        AuthState.USER_ACTION_REQUIRED, "browser_action_required",
    ),
    "browser_runtime_unavailable": (
        AuthState.USER_ACTION_REQUIRED, "browser_action_required",
    ),
    "real_login_not_approved": (
        AuthState.USER_ACTION_REQUIRED, "authorization_failed",
    ),
    "worker_crashed": (
        AuthState.USER_ACTION_REQUIRED, "authorization_failed",
    ),
    "worker_start_failed": (
        AuthState.TEMPORARILY_UNAVAILABLE, "authorization_temporarily_unavailable",
    ),
    "internal": (AuthState.USER_ACTION_REQUIRED, "authorization_failed"),
}


def gppt_installed() -> bool:
    """只查模块是否存在，不 import（避免触发 Windows 兼容缺陷）。"""
    try:
        return importlib.util.find_spec("gppt") is not None
    except (ImportError, ValueError):
        return False


def chromium_available() -> bool:
    base = PLAYWRIGHT_BROWSERS_PATH
    if not base.is_dir():
        return False
    return any(
        (revision / "chrome-win64" / "chrome.exe").is_file()
        for revision in base.glob("chromium-*")
    )


class GpptSupervisedAuthorizationProvider:
    """可见受监督浏览器登录提供者（supervised_browser）。"""

    mode = "supervised_browser"

    def __init__(
        self,
        worker_command: list[str] | None = None,
        *,
        timeout_seconds: float = 300.0,
        request_extra: dict | None = None,
    ) -> None:
        self._command = (
            list(worker_command) if worker_command is not None
            else DEFAULT_WORKER_COMMAND
        )
        self._timeout = timeout_seconds
        # 测试注入口：附加到 worker 请求的非敏感字段（如 fake 行为），
        # 仅离线测试使用；生产路径不设置。
        self._request_extra = dict(request_extra or {})
        self._lock = threading.Lock()
        self._supervisor: WorkerSupervisor | None = None
        self._session_id: str | None = None

    @property
    def available(self) -> bool:
        # 依赖存在 + Chromium 存在 + 真实登录门禁明确开启，三者缺一不可；
        # 门禁单一来源为 env RADAR_AUTH_ALLOW_REAL，默认关闭。
        return (
            gppt_installed()
            and chromium_available()
            and real_login_gate_enabled()
        )

    def start(
        self,
        request: ProviderRequest,
        *,
        on_complete: Callable[[str, str], None],
        on_failure: Callable[[str, ProviderFailure], None],
    ) -> ProviderLaunch:
        if not self.available:
            raise AuthOperationError(503, "login_provider_unavailable")
        with self._lock:
            if self._supervisor is not None and self._supervisor.running:
                raise AuthOperationError(409, "authorization_in_progress")
            supervisor = WorkerSupervisor(
                self._command,
                timeout_seconds=self._timeout,
                # worker 收到与 available 判断一致的门禁值（同一来源）
                allow_real=real_login_gate_enabled(),
            )
            request_payload = {
                "session_id": request.session_id,
                "state": request.state,
                "expires_at": request.expires_at.isoformat(),
            }
            request_payload.update(self._request_extra)
            pid = supervisor.start(request_payload)
            if pid == 0:
                raise AuthOperationError(503, "authorization_temporarily_unavailable")
            self._supervisor = supervisor
            self._session_id = request.session_id

        threading.Thread(
            target=self._wait_and_dispatch,
            args=(supervisor, request, on_complete, on_failure),
            daemon=True,
        ).start()
        # 受监督浏览器：gppt 空凭据模式必然打开可见窗口
        return ProviderLaunch(mode=self.mode, browser_opened=True)

    def cancel(self, session_id: str) -> None:
        with self._lock:
            if self._session_id != session_id:
                return  # 只取消当前会话
            supervisor = self._supervisor
        if supervisor is not None:
            supervisor.cancel()

    # ---- 内部 ----

    def _wait_and_dispatch(
        self,
        supervisor: WorkerSupervisor,
        request: ProviderRequest,
        on_complete: Callable[[str, str], None],
        on_failure: Callable[[str, ProviderFailure], None],
    ) -> None:
        result = supervisor.wait()
        with self._lock:
            if self._supervisor is supervisor:
                self._supervisor = None
                self._session_id = None

        if result.status == "completed":
            # 只把 worker 实际返回的 state 交给 AuthService（缺失/空/错误值
            # 全部由 AuthService 的 state 校验拒绝，不补造、不替换）。
            on_complete(result.callback_state, result.refresh_token or "")
            return
        if result.status == "selfcheck_ok":
            return
        state, code = _WORKER_FAILURE_MAP.get(
            result.error_code or "internal",
            (AuthState.USER_ACTION_REQUIRED, "authorization_failed"),
        )
        on_failure(request.state, ProviderFailure(state=state, error_code=code))
