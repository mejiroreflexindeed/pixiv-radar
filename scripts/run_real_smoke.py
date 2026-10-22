"""受限真实 Pixiv 冒烟入口。

必须由用户在本机显式运行，模型与自动流程不得执行：

    .\backend\.venv\Scripts\python.exe scripts\run_real_smoke.py

范围固定：标签 ウマ娘、1 页、5 结果、单并发、不取正文；
401/403/429/验证码/结构异常/凭据后端不可用 → 立即中止，不绕过。
token 只经 Windows 凭据库固定项读取，不打印、不落日志。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.credentials import CredentialUnavailable  # noqa: E402
from app.core.keyring_store import KeyringCredentialStore  # noqa: E402
from app.core.logging import redacted_error_text  # noqa: E402
from app.domain.smoke_scan import SmokeScanService  # noqa: E402
from app.storage.connection import connect  # noqa: E402
from app.storage.migrations import apply_migrations  # noqa: E402
from app.storage.scan_repository import ScanRepository  # noqa: E402


def _make_api(token: str):
    """用 refresh token 登录并返回已认证的 AppPixivAPI。"""
    from pixivpy3 import AppPixivAPI

    api = AppPixivAPI(timeout=15)
    api.set_accept_language("zh-cn")
    api.auth(refresh_token=token)
    return api


def main() -> int:
    settings = get_settings()

    conn = connect(settings.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    try:
        store = KeyringCredentialStore()
    except CredentialUnavailable as exc:
        print(f"[中止] 凭据后端不可用：{redacted_error_text(exc)}", file=sys.stderr)
        return 1

    service = SmokeScanService(
        credential_store=store,
        api_factory=_make_api,
        repository=ScanRepository(),
        db_path=settings.db_path,
    )
    result = service.run()
    print(
        f"status={result.status} run_id={result.run_id} "
        f"cooldown={result.cooldown_seconds} message={result.message}"
    )
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
