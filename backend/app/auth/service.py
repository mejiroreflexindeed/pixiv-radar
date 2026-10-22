"""Login-1 离线骨架：无敏感输出的授权状态机与提供者边界。

本模块不实现真实 Pixiv OAuth，也不打开浏览器。真实授权工具必须通过
``AuthorizationProvider`` 接入，并只把最终 refresh token 交给完成回调。
token、OAuth state 与会话标识均不得进入日志、异常、API 状态或 repr。
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from ..core.credentials import CredentialStore
from ..pixiv.exceptions import AuthFailed, RateLimited
from ..pixiv.request_gate import (
    PixivCooldownActive,
    PixivOperationCancelled,
    PixivOperationInProgress,
    PixivPaused,
    normalize_cooldown_seconds,
)


class AuthState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    AUTHORIZING = "authorizing"
    CONNECTED = "connected"
    EXPIRED_OR_REVOKED = "expired_or_revoked"
    RATE_LIMITED = "rate_limited"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    USER_ACTION_REQUIRED = "user_action_required"


@dataclass(frozen=True, repr=False)
class ProviderRequest:
    """传给授权提供者的短期会话；不得记录或序列化。"""

    session_id: str
    state: str
    expires_at: datetime


@dataclass(frozen=True)
class ProviderLaunch:
    """提供者启动结果，仅含可安全返回给 UI 的元数据。"""

    mode: str
    browser_opened: bool


@dataclass(frozen=True)
class ProviderFailure:
    """提供者回传的受控失败，不接受原始异常或响应正文。"""

    state: AuthState
    error_code: str
    cooldown_until: datetime | None = None


CompletionCallback = Callable[[str, str], None]
FailureCallback = Callable[[str, ProviderFailure], None]


@runtime_checkable
class AuthorizationProvider(Protocol):
    """真实/假授权实现的最小协议。"""

    @property
    def available(self) -> bool: ...

    @property
    def mode(self) -> str: ...

    def start(
        self,
        request: ProviderRequest,
        *,
        on_complete: CompletionCallback,
        on_failure: FailureCallback,
    ) -> ProviderLaunch: ...

    def cancel(self, session_id: str) -> None: ...


class UnavailableAuthorizationProvider:
    """生产默认值：真实登录依赖未获批准/未安装时 fail closed。"""

    available = False
    mode = "unavailable"

    def start(
        self,
        request: ProviderRequest,
        *,
        on_complete: CompletionCallback,
        on_failure: FailureCallback,
    ) -> ProviderLaunch:
        del request, on_complete, on_failure
        raise AuthOperationError(503, "login_provider_unavailable")

    def cancel(self, session_id: str) -> None:
        del session_id


class AuthOperationError(RuntimeError):
    """可安全映射到 API 的固定错误；消息中不含上游详情。"""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class AuthCapability:
    """登录/复用登录的离线能力信息；不含任何凭据。"""

    login_available: bool
    validate_available: bool
    unavailable_reason: str | None = None  # demo_mode | missing_dependency | missing_browser


@dataclass(frozen=True)
class AuthStatus:
    state: AuthState
    login_available: bool
    provider_mode: str
    can_disconnect: bool
    last_success_at: datetime | None = None
    error_code: str | None = None
    cooldown_until: datetime | None = None
    session_expires_at: datetime | None = None
    validate_available: bool = False
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "login_available": self.login_available,
            "provider_mode": self.provider_mode,
            "can_disconnect": self.can_disconnect,
            "last_success_at": _iso(self.last_success_at),
            "error_code": self.error_code,
            "cooldown_until": _iso(self.cooldown_until),
            "session_expires_at": _iso(self.session_expires_at),
            "validate_available": self.validate_available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class AuthStartResult:
    status: AuthStatus
    launch: ProviderLaunch

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.as_dict(),
            "launch": {
                "mode": self.launch.mode,
                "browser_opened": self.launch.browser_opened,
            },
        }


@dataclass(repr=False)
class _Session:
    session_id: str
    state: str
    expires_at: datetime


_ALLOWED_FAILURES: dict[AuthState, set[str]] = {
    AuthState.EXPIRED_OR_REVOKED: {"authorization_rejected"},
    AuthState.RATE_LIMITED: {"authorization_rate_limited"},
    AuthState.TEMPORARILY_UNAVAILABLE: {
        "authorization_temporarily_unavailable",
    },
    AuthState.USER_ACTION_REQUIRED: {
        "authorization_cancelled",
        "authorization_failed",
        "authorization_timeout",
        "browser_action_required",
    },
}

_ALLOWED_PROVIDER_MODES = {
    "system_browser",
    "supervised_browser",
    "paste_code",
    "fake_oauth",
}


class AuthService:
    """单用户、进程内授权会话管理器。

    只有提供者完成回调能写入凭据；HTTP API 不接收 token。每个授权会话使用
    高熵 state、五分钟默认超时并在成功/失败/取消后单次消费。
    """

    def __init__(
        self,
        credential_store: CredentialStore,
        provider: AuthorizationProvider,
        *,
        session_ttl: timedelta = timedelta(minutes=5),
        now: Callable[[], datetime] | None = None,
        allow_real: Callable[[], bool] | None = None,
        validator: Callable[[str], None] | None = None,
        capability: Callable[[], AuthCapability] | None = None,
        gate=None,
        on_session_revoked: Callable[[], None] | None = None,
    ) -> None:
        self._credential_store = credential_store
        self._provider = provider
        self._session_ttl = session_ttl
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._allow_real = allow_real or (lambda: False)
        self._validator = validator
        self._capability = capability
        self._gate = gate
        self._on_session_revoked = on_session_revoked
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._state = AuthState.NOT_CONFIGURED
        self._credential_present: bool | None = None
        self._last_success_at: datetime | None = None
        self._error_code: str | None = None
        self._cooldown_until: datetime | None = None
        # 会话世代：start/validate/disconnect/cancel 递增，迟到结果不得回写状态。
        self._generation = 0
        self._operation_in_progress = False
        # 授权期间占用共享出站门，完成后释放；解锁业务再插入。
        self._gate_lease_held = False
        # 显式验证期间占用共享门（检查先于读凭据）；由 validate 的 finally 释放。
        self._gate_validate_held = False

    def set_session_revoked_hook(self, hook: Callable[[], None] | None) -> None:
        """装配层注入：断开登录时取消本代业务（不触碰别的进程/线程）。"""
        self._on_session_revoked = hook

    def status(self) -> AuthStatus:
        """只读当前进程状态与离线能力；绝不读取凭据存储。"""
        expired_session_id: str | None = None
        with self._lock:
            if self._session is not None and self._now() >= self._session.expires_at:
                expired_session_id = self._session.session_id
                self._consume_session_locked()
                self._state = AuthState.USER_ACTION_REQUIRED
                self._error_code = "authorization_timeout"

            status = self._snapshot_locked()

        if expired_session_id is not None:
            self._safe_provider_cancel(expired_session_id)
        return self._merge_gate_cooldown(status)

    def _merge_gate_cooldown(self, status: AuthStatus) -> AuthStatus:
        """把共享门的有效 429 冷却反映到登录状态，让 UI 显示剩余时间并说明原因。

        发现/正文触发的限流同样约束登录入口；不这样做，连接状态会在冷却期内
        显示可点击但每次都被后端拒绝。到期即不再合成，由用户显式恢复。
        """
        gate = self._gate
        if gate is None:
            return status
        try:
            remaining = gate.state().get("cooldown_remaining_seconds")
        except Exception:
            return status
        if not isinstance(remaining, (int, float)) or remaining <= 0:
            return status
        return replace(
            status,
            state=AuthState.RATE_LIMITED,
            error_code=status.error_code or "authorization_rate_limited",
            cooldown_until=self._now() + timedelta(seconds=float(remaining)),
        )

    def start(self) -> AuthStartResult:
        with self._lock:
            now = self._now()
            if (
                self._state is AuthState.RATE_LIMITED
                and self._cooldown_until is not None
                and now < self._cooldown_until
            ):
                raise AuthOperationError(429, "authorization_rate_limited")
            if self._session is not None:
                raise AuthOperationError(409, "authorization_in_progress")
            if self._operation_in_progress:
                raise AuthOperationError(409, "authorization_in_progress")
            provider_available, _ = self._provider_capability()
            if not provider_available:
                raise AuthOperationError(503, "login_provider_unavailable")
            # 原子占用共享出站门：已有发现/正文在出站时返回 409，不启动 provider。
            self._acquire_gate_locked()

            session = _Session(
                session_id=secrets.token_urlsafe(24),
                state=secrets.token_urlsafe(32),
                expires_at=now + self._session_ttl,
            )
            self._session = session
            self._generation += 1
            self._state = AuthState.AUTHORIZING
            self._error_code = None
            self._cooldown_until = None

        request = ProviderRequest(
            session_id=session.session_id,
            state=session.state,
            expires_at=session.expires_at,
        )
        try:
            provider_launch = self._provider.start(
                request,
                on_complete=lambda callback_state, refresh_token: (
                    self._complete_from_provider(
                        session.session_id,
                        callback_state,
                        refresh_token,
                    )
                ),
                on_failure=lambda callback_state, failure: (
                    self._fail_from_provider(
                        session.session_id,
                        callback_state,
                        failure,
                    )
                ),
            )
        except AuthOperationError:
            self._mark_start_failed(session.session_id)
            raise
        except Exception:
            self._mark_start_failed(session.session_id)
            raise AuthOperationError(
                503, "authorization_temporarily_unavailable"
            ) from None

        # 提供者元数据同样不直接透传：模式必须在白名单中，布尔值必须真为 bool。
        _, safe_mode = self._provider_capability()
        launch = ProviderLaunch(
            mode=safe_mode,
            browser_opened=provider_launch.browser_opened is True,
        )
        with self._lock:
            return AuthStartResult(self._snapshot_locked(), launch)

    def validate(self) -> AuthStatus:
        """显式验证已保存登录（用户主动 POST）。

        - 真实门关闭：读取次数严格 0，返回受控 login_provider_unavailable；
        - 无 token：受控 pixiv_login_required，不自动打开登录浏览器；
        - 有 token：调用注入验证器（默认真实 Pixiv auth）；成功=connected，
          401/403=需要重新授权，429=冷却，网络/其他=暂时不可用；
        - 不静默清空或替换已保存的 token；迟到结果不覆盖新世代状态。
        """
        with self._lock:
            if self._session is not None or self._operation_in_progress:
                raise AuthOperationError(409, "authorization_in_progress")
            if self._state is AuthState.RATE_LIMITED and self._cooldown_until is not None:
                if self._now() < self._cooldown_until:
                    raise AuthOperationError(429, "authorization_rate_limited")
            generation = self._generation
            self._operation_in_progress = True

        # 先占用并检查共享门（busy/冷却），获准后才允许读取凭据；被拒绝也必须
        # 复位操作占用，否则后续验证/出站会被永久当作“操作进行中”。
        try:
            self._acquire_validate_gate()
            return self._validate_locked(generation)
        finally:
            self._release_validate_gate()
            with self._lock:
                self._operation_in_progress = False

    def _acquire_validate_gate(self) -> None:
        gate = self._gate
        if gate is None:
            return
        try:
            gate.acquire(user_initiated=True)
        except PixivOperationInProgress:
            raise AuthOperationError(
                409, "pixiv_operation_in_progress"
            ) from None
        except PixivCooldownActive:
            raise AuthOperationError(429, "authorization_rate_limited") from None
        except (PixivOperationCancelled, PixivPaused):
            raise AuthOperationError(
                503, "authorization_temporarily_unavailable"
            ) from None
        except Exception:
            raise AuthOperationError(
                503, "authorization_temporarily_unavailable"
            ) from None
        self._gate_validate_held = True

    def _release_validate_gate(self) -> None:
        if self._gate is None or not self._gate_validate_held:
            return
        self._gate_validate_held = False
        self._gate.release()

    def _validate_locked(self, generation: int) -> AuthStatus:
        if not self._allow_real():
            raise AuthOperationError(503, "login_provider_unavailable")

        try:
            token = self._credential_store.get()
        except Exception:
            with self._lock:
                if generation == self._generation:
                    self._state = AuthState.TEMPORARILY_UNAVAILABLE
                    self._error_code = "credential_backend_unavailable"
                return self._snapshot_locked()

        if not token:
            with self._lock:
                if generation == self._generation:
                    self._credential_present = False
                    self._state = AuthState.USER_ACTION_REQUIRED
                    self._error_code = "pixiv_login_required"
                return self._snapshot_locked()

        validator = self._validator
        failure: ProviderFailure | None = None
        try:
            if validator is not None:
                # 验证 HTTP 与授权/搜索共用最小间隔计时；获准后才调用上游。
                if self._gate is not None:
                    self._gate.before_call(user_initiated=True)
                # 等待最小间隔期间可能断开或重新授权：调用前再核验本代仍有效，
                # 断开后不得再发出任何认证 HTTP（只返回当前快照）。
                with self._lock:
                    if generation != self._generation:
                        return self._snapshot_locked()
                try:
                    validator(token)
                finally:
                    if self._gate is not None:
                        self._gate.after_call()
        except AuthOperationError:
            raise
        except PixivCooldownActive as exc:
            cooldown = normalize_cooldown_seconds(exc.remaining_seconds)
            failure = ProviderFailure(
                state=AuthState.RATE_LIMITED,
                error_code="authorization_rate_limited",
                cooldown_until=self._now() + timedelta(seconds=cooldown),
            )
        except (PixivOperationCancelled, PixivPaused):
            failure = ProviderFailure(
                state=AuthState.TEMPORARILY_UNAVAILABLE,
                error_code="authorization_temporarily_unavailable",
            )
        except RateLimited as exc:
            # 缺失/无效/超范围冷却统一回落 900 秒，且可安全序列化到 UI。
            cooldown = normalize_cooldown_seconds(exc.cooldown_seconds)
            failure = ProviderFailure(
                state=AuthState.RATE_LIMITED,
                error_code="authorization_rate_limited",
                cooldown_until=self._now() + timedelta(seconds=cooldown),
            )
        except AuthFailed:
            failure = ProviderFailure(
                state=AuthState.EXPIRED_OR_REVOKED,
                error_code="authorization_rejected",
            )
        except Exception:
            failure = ProviderFailure(
                state=AuthState.TEMPORARILY_UNAVAILABLE,
                error_code="authorization_temporarily_unavailable",
            )

        with self._lock:
            if generation != self._generation:
                # 断开/重新授权已发生：迟到结果不得恢复连接或改写状态
                return self._snapshot_locked()
            if failure is not None:
                self._state = failure.state
                self._error_code = failure.error_code
                self._cooldown_until = failure.cooldown_until
                return self._snapshot_locked()
            self._credential_present = True
            self._state = AuthState.CONNECTED
            self._last_success_at = self._now()
            self._error_code = None
            self._cooldown_until = None
            if self._gate is not None:
                # 本代显式验证成功：新会话边界让旧操作迟到结果失效，并恢复暂停。
                self._gate.begin_session()
                self._gate.resume()
            return self._snapshot_locked()

    def outbound_session_valid(self) -> bool:
        """共享门守卫：只有本代已完成真实授权/验证且未在授权中才允许出站。"""
        with self._lock:
            return (
                self._session is None
                and not self._operation_in_progress
                and self._state is AuthState.CONNECTED
            )

    def _acquire_gate_locked(self) -> None:
        """在持有认证锁时原子占用共享门（auth→gate 锁顺序全局一致）。"""
        if self._gate is None:
            return
        try:
            self._gate.acquire_authorization()
        except PixivOperationInProgress:
            raise AuthOperationError(
                409, "pixiv_operation_in_progress"
            ) from None
        except PixivCooldownActive:
            # 共享 429 冷却同时约束浏览器授权：冷却期 provider.start=0。
            raise AuthOperationError(429, "authorization_rate_limited") from None
        except Exception:
            raise AuthOperationError(
                503, "authorization_temporarily_unavailable"
            ) from None
        self._gate_lease_held = True

    def _release_gate_locked(self) -> None:
        if self._gate is None or not self._gate_lease_held:
            return
        self._gate_lease_held = False
        self._gate.release_authorization()

    def cancel(self) -> AuthStatus:
        with self._lock:
            if self._session is None:
                raise AuthOperationError(409, "no_authorization_in_progress")
            session_id = self._session.session_id
            self._consume_session_locked()
            self._generation += 1
            self._state = (
                AuthState.USER_ACTION_REQUIRED
                if self._credential_present
                else AuthState.NOT_CONFIGURED
            )
            self._error_code = "authorization_cancelled"
            status = self._snapshot_locked()

        self._safe_provider_cancel(session_id)
        return status

    def disconnect(self) -> AuthStatus:
        with self._lock:
            session_id = self._session.session_id if self._session else None
            self._generation += 1
            if self._gate is not None:
                # 断开是会话边界：先递增世代（在途旧请求之后不得再 pause/resume
                # 新会话），并打断正在等待最小间隔的调用，再释放授权门。
                self._gate.begin_session()
                self._gate.cancel()
            self._consume_session_locked()
            try:
                self._credential_store.delete()
            except Exception:
                self._state = AuthState.TEMPORARILY_UNAVAILABLE
                self._error_code = "credential_backend_unavailable"
                raise AuthOperationError(
                    503, "credential_backend_unavailable"
                ) from None

            self._credential_present = False
            self._state = AuthState.NOT_CONFIGURED
            self._last_success_at = None
            self._error_code = None
            self._cooldown_until = None
            status = self._snapshot_locked()

        if session_id is not None:
            self._safe_provider_cancel(session_id)
        if self._on_session_revoked is not None:
            try:
                # 断开立即失效本代业务：由装配层取消正在进行的发现/正文。
                self._on_session_revoked()
            except Exception:
                pass
        return status

    def _discover_credential_locked(self) -> None:
        """已弃用：GET status 不再读取凭据；保留显式调用仅用于兼容旧测试。"""
        if self._credential_present is not None:
            return
        try:
            self._credential_present = bool(self._credential_store.get())
        except Exception:
            self._credential_present = False
            self._state = AuthState.TEMPORARILY_UNAVAILABLE
            self._error_code = "credential_backend_unavailable"
            return
        if self._credential_present:
            self._state = AuthState.USER_ACTION_REQUIRED
            self._error_code = "credential_validation_required"

    def _complete_from_provider(
        self,
        session_id: str,
        callback_state: str,
        refresh_token: str,
    ) -> None:
        with self._lock:
            session = self._session
            if session is None or not secrets.compare_digest(
                session.session_id, session_id
            ):
                return
            if self._now() >= session.expires_at:
                self._consume_session_locked()
                self._state = AuthState.USER_ACTION_REQUIRED
                self._error_code = "authorization_timeout"
                return
            # 缺失/空/非字符串 state 一律按不匹配拒绝，绝不补造
            if (
                not isinstance(callback_state, str)
                or not secrets.compare_digest(session.state, callback_state)
            ):
                self._consume_session_locked()
                self._state = AuthState.USER_ACTION_REQUIRED
                self._error_code = "authorization_state_mismatch"
                return
            if not isinstance(refresh_token, str) or not refresh_token.strip():
                self._consume_session_locked()
                self._state = AuthState.USER_ACTION_REQUIRED
                self._error_code = "authorization_invalid_result"
                return
            try:
                self._credential_store.set(refresh_token)
            except Exception:
                self._consume_session_locked()
                self._credential_present = False
                self._state = AuthState.TEMPORARILY_UNAVAILABLE
                self._error_code = "credential_backend_unavailable"
                return
            self._credential_present = True
            self._state = AuthState.CONNECTED
            self._last_success_at = self._now()
            self._error_code = None
            self._cooldown_until = None
            # 先提交本代新会话身份（epoch/恢复），最后才释放授权门；否则新业务
            # 可能在门释放后取得尚未提交的旧 epoch，其真实 429 会被误丢弃。
            if self._gate is not None:
                self._gate.begin_session()
                self._gate.resume()
            self._session = None
            self._release_gate_locked()

    def _fail_from_provider(
        self,
        session_id: str,
        callback_state: str,
        failure: ProviderFailure,
    ) -> None:
        with self._lock:
            session = self._session
            if session is None or not secrets.compare_digest(
                session.session_id, session_id
            ):
                return
            if (
                not isinstance(callback_state, str)
                or not secrets.compare_digest(session.state, callback_state)
            ):
                self._consume_session_locked()
                self._state = AuthState.USER_ACTION_REQUIRED
                self._error_code = "authorization_state_mismatch"
                return
            self._consume_session_locked()
            allowed_codes = _ALLOWED_FAILURES.get(failure.state, set())
            if failure.error_code not in allowed_codes:
                self._state = AuthState.USER_ACTION_REQUIRED
                self._error_code = "authorization_failed"
                self._cooldown_until = None
                return
            self._state = failure.state
            self._error_code = failure.error_code
            self._cooldown_until = failure.cooldown_until

    def _mark_start_failed(self, session_id: str) -> None:
        with self._lock:
            if self._session is None or not secrets.compare_digest(
                self._session.session_id, session_id
            ):
                return
            self._consume_session_locked()
            self._state = AuthState.TEMPORARILY_UNAVAILABLE
            self._error_code = "authorization_temporarily_unavailable"

    def _consume_session_locked(self) -> None:
        self._session = None
        self._release_gate_locked()

    def _snapshot_locked(self) -> AuthStatus:
        provider_available, provider_mode = self._provider_capability()
        capability = self._capability_status()
        return AuthStatus(
            state=self._state,
            login_available=capability.login_available,
            provider_mode=provider_mode if provider_available else "unavailable",
            can_disconnect=bool(self._credential_present),
            last_success_at=self._last_success_at,
            error_code=self._error_code,
            cooldown_until=self._cooldown_until,
            session_expires_at=(
                self._session.expires_at if self._session is not None else None
            ),
            validate_available=capability.validate_available,
            unavailable_reason=capability.unavailable_reason,
        )

    def _capability_status(self) -> AuthCapability:
        if self._capability is not None:
            try:
                return self._capability()
            except Exception:
                return AuthCapability(False, False, "missing_dependency")
        available, _ = self._provider_capability()
        return AuthCapability(
            login_available=available,
            validate_available=available,
            unavailable_reason=None if available else "missing_dependency",
        )

    def _safe_provider_cancel(self, session_id: str) -> None:
        try:
            self._provider.cancel(session_id)
        except Exception:
            # 取消是尽力而为；会话已经在本地失效，绝不暴露提供者异常。
            return

    def _provider_capability(self) -> tuple[bool, str]:
        try:
            available = self._provider.available is True
            mode = self._provider.mode
        except Exception:
            return False, "unavailable"
        if not available or mode not in _ALLOWED_PROVIDER_MODES:
            return False, "unavailable"
        return True, mode

    def __repr__(self) -> str:
        _, provider_mode = self._provider_capability()
        return (
            f"{type(self).__name__}(state={self._state.value!r}, "
            f"provider_mode={provider_mode!r})"
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
