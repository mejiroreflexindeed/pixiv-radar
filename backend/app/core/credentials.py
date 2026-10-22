"""凭据契约：固定 keyring 项 + CredentialStore 协议与内存实现。

契约见 docs/PHASE_B_CONTRACT.md。任何实现都只允许操作固定项
（service=pixiv-radar, key=pixiv-refresh-token），不得枚举其他凭据；
repr / 日志 / 异常不得包含 token。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

KEYRING_SERVICE = "pixiv-radar"
KEYRING_KEY = "pixiv-refresh-token"


class CredentialUnavailable(RuntimeError):
    """凭据后端不可用（例如安全后端缺失或为明文 fallback）时 fail closed。"""


@runtime_checkable
class CredentialStore(Protocol):
    def get(self) -> str | None: ...

    def set(self, token: str) -> None: ...

    def delete(self) -> None: ...


class InMemoryCredentialStore:
    """测试用内存实现，不落盘、不联网。"""

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    def get(self) -> str | None:
        return self._token

    def set(self, token: str) -> None:
        self._token = token

    def delete(self) -> None:
        self._token = None

    def __repr__(self) -> str:
        # repr 绝不包含 token 内容
        return f"{type(self).__name__}(token={'set' if self._token else 'unset'})"
