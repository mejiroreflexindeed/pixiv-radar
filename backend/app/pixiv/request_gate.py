"""应用内真实出站控制器：单并发、最小间隔、取消与冷却。

一个应用进程共享一个实例；发现三页搜索、已保存登录验证与正文 fetch 都经过它。
3 秒间隔是本地保守默认值，不代表 Pixiv 官方允许的频率。测试可注入单调时钟与
可取消等待，不产生真实长 sleep。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Iterator

DEFAULT_MIN_INTERVAL_SECONDS = 3.0
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 900.0  # 429 缺失 cooldown 时的本地默认
MAX_COOLDOWN_SECONDS = 86400.0  # 超出视为不可用值，回落默认


def normalize_cooldown_seconds(
    value: object,
    *,
    default: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    max_seconds: float = MAX_COOLDOWN_SECONDS,
) -> float:
    """统一的有限非负冷却归一化：有效值保持（含 0），其余回落默认 900 秒。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    seconds = float(value)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return default
    if seconds < 0 or seconds > max_seconds:
        return default
    return seconds


class PixivOperationInProgress(RuntimeError):
    code = "pixiv_operation_in_progress"


class PixivOperationCancelled(RuntimeError):
    code = "pixiv_operation_cancelled"


class PixivSessionInvalid(RuntimeError):
    """当前进程没有可用于出站的已连接会话（授权中/已断开/未验证）。"""

    code = "pixiv_session_invalid"


class PixivCooldownActive(RuntimeError):
    code = "pixiv_cooldown_active"

    def __init__(
        self, cooldown_until: float | None, remaining_seconds: float | None = None
    ) -> None:
        super().__init__(self.code)
        self.cooldown_until = cooldown_until
        self.remaining_seconds = remaining_seconds


class PixivPaused(RuntimeError):
    code = "pixiv_operation_paused"

    def __init__(self, reason: str | None) -> None:
        super().__init__(self.code)
        self.reason = reason


class RequestGate:
    """真实出站操作的统一门：一次一个、间隔、取消、冷却与暂停。"""

    def __init__(
        self,
        *,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] | None = None,
        wait: Callable[[float, threading.Event], bool] | None = None,
    ) -> None:
        self._min_interval = float(min_interval_seconds)
        self._clock = clock or time.monotonic
        self._wait = wait or (lambda seconds, cancel: cancel.wait(seconds))
        self._lock = threading.Lock()
        self._busy = False
        self._cancel = threading.Event()
        self._last_call_at: float | None = None
        self._cooldown_until: float | None = None
        self._paused_reason: str | None = None
        # 共享出站会话守卫：由装配层绑定到 AuthService；None 表示不做会话检查。
        self._session_guard: Callable[[], bool] | None = None
        # 会话世代：新授权/验证成功或断开时递增；旧操作的迟到提交带世代校验。
        self._session_epoch = 0

    # ---- 会话世代（迟到结果不得改写新会话状态） ----

    def session_epoch(self) -> int:
        with self._lock:
            return self._session_epoch

    def begin_session(self) -> int:
        """标记会话边界：断开/新登录/重新验证成功后，旧操作的 pause/resume 失效。"""
        with self._lock:
            self._session_epoch += 1
            return self._session_epoch

    # ---- 会话守卫（授权/断开与真实出站的统一生命周期） ----

    def set_session_guard(self, guard: Callable[[], bool] | None) -> None:
        with self._lock:
            self._session_guard = guard

    def _check_session(self) -> None:
        with self._lock:
            guard = self._session_guard
        if guard is None:
            return
        try:
            valid = bool(guard())
        except Exception:
            valid = False
        if not valid:
            raise PixivSessionInvalid()

    def acquire_authorization(self) -> None:
        """授权开始前原子占用整扇门：已有业务出站或有效冷却时受控拒绝。

        授权流程本身会出站（打开官方页面/提交回调），所以共享 429 冷却必须
        同时约束浏览器授权和已保存登录验证；冷却结束只允许用户显式恢复。
        """
        with self._lock:
            if self._busy:
                raise PixivOperationInProgress()
            now = self._clock()
            if self._cooldown_until is not None and now < self._cooldown_until:
                raise PixivCooldownActive(
                    self._cooldown_until, self._cooldown_until - now
                )
            self._busy = True
            self._cancel.clear()

    def release_authorization(self) -> None:
        self.release()

    # ---- 操作生命周期 ----

    def acquire(self, *, user_initiated: bool = False) -> None:
        with self._lock:
            if self._busy:
                raise PixivOperationInProgress()
            # 用户显式"验证已保存登录"是恢复暂停的唯一入口，必须能在暂停后
            # 仍然发起；自动发现/正文调用不允许借用户操作绕过暂停。
            if self._paused_reason is not None and not user_initiated:
                raise PixivPaused(self._paused_reason)
            now = self._clock()
            if self._cooldown_until is not None and now < self._cooldown_until:
                raise PixivCooldownActive(
                    self._cooldown_until, self._cooldown_until - now
                )
            self._busy = True
            self._cancel.clear()

    def release(self) -> None:
        with self._lock:
            self._busy = False

    @contextmanager
    def operation(self, *, user_initiated: bool = False) -> Iterator["RequestGate"]:
        self.acquire(user_initiated=user_initiated)
        try:
            yield self
        finally:
            self.release()

    # ---- 每次调用前 ----

    def before_call(self, *, user_initiated: bool = False) -> None:
        """在真正发出一条 HTTP 之前调用：会话、取消、暂停、冷却、最小间隔。

        会话/取消/暂停在等待前、等待后与真正发出前都会检查；即使没有等待路径
        也不能漏检。最小间隔从"上一次调用完成时刻"起算（``after_call`` 记录）。
        用户显式验证（user_initiated）是建立会话的入口，不做会话守卫但仍受冷却约束。
        """
        with self._lock:
            if not self._busy:
                raise RuntimeError("request gate not acquired")
        if not user_initiated:
            self._check_session()
        self._check_blocked(user_initiated=user_initiated)
        with self._lock:
            last = self._last_call_at
        if last is not None:
            remaining = self._min_interval - (self._clock() - last)
            if remaining > 0:
                if self._wait(remaining, self._cancel):
                    raise PixivOperationCancelled()
                if not user_initiated:
                    self._check_session()
                self._check_blocked(user_initiated=user_initiated)
        if not user_initiated:
            self._check_session()
        self._check_blocked(user_initiated=user_initiated)
        with self._lock:
            self._last_call_at = self._clock()

    def after_call(self) -> None:
        """每次上游调用结束（成功或失败）在 finally 调用，记录完成时刻。"""
        with self._lock:
            self._last_call_at = self._clock()

    def check_ready(self, *, user_initiated: bool = False) -> None:
        """只做会话/取消/暂停/冷却检查，不等待、不记录调用时刻。

        适配器在读取凭据之前调用它，确保冷却或会话失效时既不出站也不读凭据。
        """
        with self._lock:
            if not self._busy:
                raise RuntimeError("request gate not acquired")
        if not user_initiated:
            self._check_session()
        self._check_blocked(user_initiated=user_initiated)

    def _check_blocked(self, *, user_initiated: bool = False) -> None:
        if self._cancel.is_set():
            raise PixivOperationCancelled()
        with self._lock:
            paused = self._paused_reason
            cooldown = self._cooldown_until
        if paused is not None and not user_initiated:
            raise PixivPaused(paused)
        now = self._clock()
        if cooldown is not None and now < cooldown:
            raise PixivCooldownActive(cooldown, cooldown - now)

    # ---- 取消 / 暂停 / 恢复 ----

    def cancel(self) -> None:
        self._cancel.set()

    def pause(
        self,
        reason: str,
        *,
        cooldown_seconds: float | None = None,
        epoch: int | None = None,
    ) -> bool:
        """暂停出站；rate_limited 的冷却做统一归一化后使用本地时钟记录。

        带 epoch 的提交只在仍属于同一共享会话世代时生效，避免旧请求的迟到
        失败暂停刚刚重建的新登录。
        """
        with self._lock:
            if epoch is not None and epoch != self._session_epoch:
                return False
            self._paused_reason = reason
            if reason == "rate_limited":
                seconds = normalize_cooldown_seconds(cooldown_seconds)
                self._cooldown_until = self._clock() + seconds
            elif cooldown_seconds is not None:
                seconds = normalize_cooldown_seconds(cooldown_seconds)
                self._cooldown_until = self._clock() + seconds
            self._cancel.set()
            return True

    def resume(self, *, epoch: int | None = None) -> bool:
        """清除暂停原因；未到期的 429 冷却不提前清除（到时自然失效）。"""
        with self._lock:
            if epoch is not None and epoch != self._session_epoch:
                return False
            self._paused_reason = None
            return True

    def state(self) -> dict:
        with self._lock:
            now = self._clock()
            cooldown = self._cooldown_until
            return {
                "busy": self._busy,
                "paused_reason": self._paused_reason,
                "cooldown_until": cooldown,
                "cooldown_remaining_seconds": (
                    max(0.0, cooldown - now) if cooldown is not None else None
                ),
                "min_interval_seconds": self._min_interval,
            }


def to_pixiv_error(exc: Exception) -> Exception:
    """把门错误映射为适配器层受控异常（供 discovery/content 适配器使用）。"""
    from .exceptions import RateLimited, TemporaryFailure

    if isinstance(exc, PixivCooldownActive):
        remaining = exc.remaining_seconds
        cooldown = (
            float(remaining)
            if isinstance(remaining, (int, float))
            and remaining == remaining
            and remaining not in (float("inf"), float("-inf"))
            and remaining >= 0
            else None
        )
        return RateLimited("pixiv cooldown active", cooldown_seconds=cooldown)
    if isinstance(exc, PixivOperationCancelled):
        return TemporaryFailure("pixiv_operation_cancelled")
    if isinstance(exc, PixivSessionInvalid):
        return TemporaryFailure("pixiv_session_invalid")
    if isinstance(exc, PixivPaused):
        return TemporaryFailure(f"pixiv_operation_paused:{exc.reason or 'unknown'}")
    return TemporaryFailure("pixiv_operation_failed")
