"""由用户在本机终端运行：把 Pixiv refresh token 写入固定 keyring 凭据项。

用法（在项目根目录）：
    .\backend\.venv\Scripts\python.exe scripts\store_pixiv_token.py

token 通过 getpass 无回显输入；本脚本不打印、不记录 token 内容。
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.credentials import (  # noqa: E402
    KEYRING_KEY,
    KEYRING_SERVICE,
    CredentialUnavailable,
)
from app.core.keyring_store import KeyringCredentialStore  # noqa: E402


def main() -> int:
    try:
        store = KeyringCredentialStore()
    except CredentialUnavailable as exc:
        print(f"[失败] 凭据后端不可用：{exc}", file=sys.stderr)
        return 1

    token = getpass.getpass("请输入 Pixiv refresh token（输入不回显）：").strip()
    if not token:
        print("[失败] token 不能为空", file=sys.stderr)
        return 1

    store.set(token)
    print(f"已写入凭据项 {KEYRING_SERVICE}/{KEYRING_KEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
