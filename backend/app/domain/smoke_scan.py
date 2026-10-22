"""受限真实 Pixiv 冒烟扫描（阶段 B 第 7 节入口）。

硬边界：
- 固定标签 ウマ娘、单页（search_novel 只调用一次）、最多 5 条结果、单并发；
- 绝不请求正文；
- 凭据缺失 / 后端不可用 / 401/403/429 / 验证码 / 结构异常 → 立即中止；
- 429 记录 cooldown 不重试。

本服务只有在用户明确确认并亲自写入凭据后、由用户在本机显式运行；
模型与自动流程不得执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.logging import redacted_error_text
from ..pixiv.exceptions import RateLimited
from ..pixiv.pixivpy_adapter import PixivPyAdapter, classify_pixiv_exception
from ..storage.scan_repository import ScanRepository
from .broad_scan import BroadScanService

SMOKE_TAG = "ウマ娘"
SMOKE_LIMIT = 5


@dataclass(frozen=True)
class SmokeResult:
    status: str  # "succeeded" / "aborted"
    run_id: int | None = None
    cooldown_seconds: float | None = None
    message: str = ""


class SmokeScanService:
    """真实冒烟：凭据 → auth → 单页搜索 → BroadScanService 落库。"""

    def __init__(
        self,
        credential_store,
        api_factory,
        repository: ScanRepository,
        db_path: str | Path,
    ) -> None:
        self._store = credential_store
        self._api_factory = api_factory
        self._repo = repository
        self._db_path = Path(db_path)

    def run(self) -> SmokeResult:
        try:
            token = self._store.get()
        except Exception as exc:
            return SmokeResult(
                status="aborted",
                message=f"凭据读取失败：{redacted_error_text(exc)}",
            )
        if not token:
            return SmokeResult(
                status="aborted",
                message="未找到 refresh token，请先运行 scripts/store_pixiv_token.py",
            )

        try:
            api = self._api_factory(token)
        except Exception as exc:
            # auth 失败（401/403/429/验证码等）→ 立即中止，不产生任何扫描写入
            classified = classify_pixiv_exception(exc)
            return SmokeResult(
                status="aborted",
                cooldown_seconds=(
                    classified.cooldown_seconds
                    if isinstance(classified, RateLimited)
                    else None
                ),
                message=redacted_error_text(classified),
            )

        service = BroadScanService(
            adapter=PixivPyAdapter(api),
            repository=self._repo,
            db_path=self._db_path,
        )
        outcome = service.run(SMOKE_TAG, limit=SMOKE_LIMIT)
        if outcome.status == "succeeded":
            return SmokeResult(
                status="succeeded",
                run_id=outcome.run_id,
                message="ok",
            )
        return SmokeResult(
            status="aborted",
            run_id=outcome.run_id,
            cooldown_seconds=outcome.cooldown_seconds,
            message=outcome.error or outcome.status,
        )
