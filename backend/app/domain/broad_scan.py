"""BroadScanService：广域扫描（阶段 B 最小链路）。

约定：
- 网络阶段结束之后才开启短 SQLite 写事务（不长时间持有写锁）；
- 单事务写入目标、标签、快照与 scan run 终态，失败整体回滚；
- 重复运行幂等：targets upsert、标签替换、快照按 run 分开；
- 空结果 = succeeded 0 条；响应结构缺失 = partial；限流/认证/网络 = failed；
  任何失败都不改写已有目标状态，也不制造“已删除”结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.logging import redacted_error_text
from ..core.timeutil import utc_now_iso
from ..pixiv.adapter import PixivAdapter
from ..pixiv.exceptions import MalformedResponse, PixivAdapterError, RateLimited
from ..storage.connection import connect, transaction
from ..storage.scan_repository import ScanRepository


@dataclass(frozen=True)
class ScanOutcome:
    run_id: int
    status: str  # succeeded / partial / failed
    results_seen: int
    results_written: int
    error: str | None = None
    cooldown_seconds: float | None = None
    started_at: str = ""
    finished_at: str = ""


class BroadScanService:
    def __init__(
        self,
        adapter: PixivAdapter,
        repository: ScanRepository,
        db_path: str | Path,
    ) -> None:
        self._adapter = adapter
        self._repo = repository
        self._db_path = Path(db_path)

    def run(
        self,
        word: str,
        *,
        limit: int = 30,
        search_target: str = "partial_match_for_tags",
        sort: str = "date_desc",
    ) -> ScanOutcome:
        started_at = utc_now_iso()
        conn = connect(self._db_path)
        try:
            run_id = self._repo.create_scan_run(
                conn, layer="broad", started_at=started_at
            )

            # ---- 网络阶段（不持有写事务）----
            try:
                novels = self._adapter.search_novels(
                    word,
                    limit=limit,
                    search_target=search_target,
                    sort=sort,
                )
            except RateLimited as exc:
                return self._fail(
                    conn, run_id, "failed", started_at, exc,
                    cooldown=exc.cooldown_seconds,
                )
            except MalformedResponse as exc:
                return self._fail(conn, run_id, "partial", started_at, exc)
            except PixivAdapterError as exc:
                return self._fail(conn, run_id, "failed", started_at, exc)
            except Exception as exc:
                return self._fail(conn, run_id, "failed", started_at, exc)

            # ---- 写入阶段（单短事务，失败整体回滚）----
            finished_at = utc_now_iso()
            try:
                with transaction(conn):
                    for novel in novels:
                        self._repo.upsert_target(conn, novel, now_iso=finished_at)
                        self._repo.replace_target_tags(
                            conn, novel.novel_id, novel.tags
                        )
                        self._repo.insert_snapshot(
                            conn,
                            novel.novel_id,
                            run_id,
                            novel,
                            captured_at=finished_at,
                        )
                    self._repo.finish_scan_run(
                        conn,
                        run_id,
                        status="succeeded",
                        finished_at=finished_at,
                        results_seen=len(novels),
                        results_written=len(novels),
                    )
            except Exception as exc:
                return self._fail(conn, run_id, "failed", started_at, exc)

            return ScanOutcome(
                run_id=run_id,
                status="succeeded",
                results_seen=len(novels),
                results_written=len(novels),
                started_at=started_at,
                finished_at=finished_at,
            )
        finally:
            conn.close()

    def _fail(
        self,
        conn,
        run_id: int,
        status: str,
        started_at: str,
        exc: Exception,
        *,
        cooldown: float | None = None,
    ) -> ScanOutcome:
        finished_at = utc_now_iso()
        # 落库前脱敏：异常文本可能携带凭据（如 auth 失败回显），绝不写入数据库
        error_text = redacted_error_text(exc)
        with transaction(conn):
            self._repo.finish_scan_run(
                conn,
                run_id,
                status=status,
                finished_at=finished_at,
                error=error_text,
            )
        return ScanOutcome(
            run_id=run_id,
            status=status,
            results_seen=0,
            results_written=0,
            error=error_text,
            cooldown_seconds=cooldown,
            started_at=started_at,
            finished_at=finished_at,
        )
