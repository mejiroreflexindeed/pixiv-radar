"""Pixiv 适配器统一异常。

语义见 docs/PHASE_B_CONTRACT.md：
- AuthFailed: 401/403、凭据无效；
- RateLimited: 429，携带 cooldown_seconds，调用方直接中止不长时间重试；
- TemporaryFailure: 网络错误 / 5xx；
- MalformedResponse: 响应缺关键字段或结构异常；
- NotFound: 明确的 404 / 不存在语义。
"""

from __future__ import annotations


class PixivAdapterError(Exception):
    """适配器层统一异常基类。"""


class AuthFailed(PixivAdapterError):
    """登录失败或凭据无效（401/403 类）。"""


class RateLimited(PixivAdapterError):
    """触发限流（429）。"""

    def __init__(
        self,
        message: str = "rate limited",
        *,
        cooldown_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds


class TemporaryFailure(PixivAdapterError):
    """网络错误或 5xx，可重试但默认不长时间自动重试。"""


class MalformedResponse(PixivAdapterError):
    """响应缺少关键字段或结构异常。"""


class NotFound(PixivAdapterError):
    """目标明确不存在（404 / 已删除语义）。"""
