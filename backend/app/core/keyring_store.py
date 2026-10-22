"""Windows keyring 凭据存储实现。

只允许操作固定项（service=pixiv-radar, key=pixiv-refresh-token），
不枚举、不读取其他凭据。后端为明文 fallback 或不可用时 fail closed。

凭证写入由用户在本机终端通过 scripts/store_pixiv_token.py 完成；
本模块与调用方均不打印、不记录 token 内容。
"""

from __future__ import annotations

import keyring

from .credentials import (
    KEYRING_KEY,
    KEYRING_SERVICE,
    CredentialUnavailable,
)


class KeyringCredentialStore:
    """基于 Python keyring 的固定项凭据存储。

    默认使用真实 keyring 后端；测试可注入 fake keyring_module。
    """

    def __init__(self, keyring_module=None) -> None:
        self._kr = keyring_module if keyring_module is not None else keyring
        self._validate_backend()

    def _validate_backend(self) -> None:
        try:
            backend = self._kr.get_keyring()
        except Exception as exc:
            raise CredentialUnavailable(
                f"credential backend unavailable: {exc}"
            ) from exc

        if backend is None:
            raise CredentialUnavailable("no keyring backend available")

        module_name = type(backend).__module__.lower()
        class_name = type(backend).__name__.lower()
        # fail closed：明文/alt 后端、无后端（fail）一律拒绝
        if (
            "keyrings.alt" in module_name
            or "plaintext" in class_name
            or ".fail" in module_name
            or module_name.endswith(".fail")
        ):
            raise CredentialUnavailable(
                f"unsafe or unavailable keyring backend: {type(backend).__name__}"
            )

    def get(self) -> str | None:
        token = self._kr.get_password(KEYRING_SERVICE, KEYRING_KEY)
        return token or None

    def set(self, token: str) -> None:
        self._kr.set_password(KEYRING_SERVICE, KEYRING_KEY, token)

    def delete(self) -> None:
        try:
            self._kr.delete_password(KEYRING_SERVICE, KEYRING_KEY)
        except keyring.errors.PasswordDeleteError:
            return

    def __repr__(self) -> str:
        return f"{type(self).__name__}(service={KEYRING_SERVICE!r}, key={KEYRING_KEY!r})"


class LazyKeyringCredentialStore:
    """首次实际凭据操作时才初始化 keyring，避免应用启动枚举或读取凭据。"""

    def __init__(self) -> None:
        self._store: KeyringCredentialStore | None = None

    def _get_store(self) -> KeyringCredentialStore:
        if self._store is None:
            self._store = KeyringCredentialStore()
        return self._store

    def get(self) -> str | None:
        return self._get_store().get()

    def set(self, token: str) -> None:
        self._get_store().set(token)

    def delete(self) -> None:
        self._get_store().delete()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(initialized={self._store is not None})"
