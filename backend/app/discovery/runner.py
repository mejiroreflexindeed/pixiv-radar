"""DiscoveryRunner：单用户显式"运行一次发现"的内存编排器。

- 同一时间最多一个运行中的 run（重复 start 返回冲突）；
- start/cancel/status 供只读 API 使用；run 在后台线程执行 DiscoveryService；
- 取消通过 threading.Event 传递，服务在下一个来源请求前停止并把 run 标为
  cancelled（不留永久 running）；
- 线程异常兜底：run 终态标记 failed 并记录受控 error code。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from ..core.timeutil import utc_now_iso
from ..pixiv.request_gate import (
    PixivCooldownActive,
    PixivOperationInProgress,
    PixivPaused,
)
from ..storage.connection import connect, transaction
from ..storage.discovery_repository import DiscoveryRepository
from ..storage.scan_repository import ScanRepository
from . import make_query_key
from .service import DiscoveryService
from .types import DiscoveryQuery


class DiscoveryBusyError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("discovery_run_in_progress")
        self.code = "discovery_run_in_progress"


def demo_queries() -> list[DiscoveryQuery]:
    """固定 Demo 查询配置：标签、角色名、关系词、新作、推荐、关注、作者、系列。"""
    return [
        DiscoveryQuery(make_query_key("exact_tag", "ウマ娘"), "exact_tag", text="ウマ娘"),
        DiscoveryQuery(make_query_key("partial_tag", "メジロアルダン"), "partial_tag", text="メジロアルダン"),
        DiscoveryQuery(make_query_key("keyword", "トレーナー"), "keyword", text="トレーナー"),
        DiscoveryQuery(make_query_key("novel_new"), "novel_new"),
        DiscoveryQuery(make_query_key("novel_recommended"), "novel_recommended"),
        DiscoveryQuery(make_query_key("novel_follow"), "novel_follow"),
        DiscoveryQuery(make_query_key("author_novels", entity_id=9), "author_novels", entity_id=9),
        DiscoveryQuery(make_query_key("series_novels", entity_id=5), "series_novels", entity_id=5),
    ]


def real_queries(tag: str | None) -> list[DiscoveryQuery]:
    """真实模式的保守默认查询：仅三种带标签约束的搜索来源。

    不默认调用全站新作、推荐、关注、作者或系列端点，避免把无关内容混入候选，
    也把每次用户触发的真实请求固定在三次以内。
    """
    value = (tag or "").strip()
    if not value or len(value) > 100:
        raise ValueError("discovery tag must contain 1..100 characters")
    return [
        DiscoveryQuery(make_query_key("exact_tag", value), "exact_tag", text=value),
        DiscoveryQuery(make_query_key("partial_tag", value), "partial_tag", text=value),
        DiscoveryQuery(make_query_key("keyword", value), "keyword", text=value),
    ]


class DiscoveryRunner:
    def __init__(
        self,
        adapter,
        scan_repo: ScanRepository,
        discovery_repo: DiscoveryRepository,
        db_path: str | Path,
        *,
        adapter_factory: Callable[[], object] | None = None,
        query_factory: Callable[[str | None], list[DiscoveryQuery]] | None = None,
        source_name: str | None = None,
        gate=None,
    ) -> None:
        self._adapter = adapter
        self._adapter_factory = adapter_factory
        self._query_factory = query_factory or (lambda tag: demo_queries())
        self.source_name = source_name or getattr(adapter, "source_name", "mock")
        self._gate = gate
        self._scan = scan_repo
        self._discovery = discovery_repo
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: dict | None = None
        # 惰性恢复：构造期不触碰数据库（导入 create_app 时 DB 可能尚未迁移）
        self._recovered = False

    def _ensure_recovered(self) -> None:
        if self._recovered:
            return
        try:
            # 缺失数据库只是“暂时无可恢复对象”，不能永久标记为已恢复；
            # 预览/应用生命周期随后可能创建并迁移该库，此时仍需检查遗留 run。
            self._recovered = self._recover_stale_runs()
        except Exception:
            # 表尚未创建（迁移未跑）等场景：无可恢复对象，静默跳过
            self._recovered = False

    def _recover_stale_runs(self) -> bool:
        """把 DB 中状态为 running 的发现 run 收敛为 failed(interrupted)。

        数据库文件不存在时直接跳过：绝不因恢复检查而创建空库文件
        （未显式指定 DB 时不得在正式路径留下空文件）。

        返回是否已对一个现存且可读的数据库完成检查。数据库暂不存在时返回
        False，使后续交互仍会重试恢复。
        """
        if not self._db_path.exists():
            return False
        conn = connect(self._db_path)
        try:
            stale = conn.execute(
                "SELECT id FROM discovery_runs WHERE status = 'running'"
            ).fetchall()
            for row in stale:
                with transaction(conn):
                    self._discovery.finish_run(
                        conn,
                        row["id"],
                        status="failed",
                        finished_at=utc_now_iso(),
                        error_code="interrupted",
                    )
        finally:
            conn.close()
        return True

    def start(self, *, tag: str | None = None) -> tuple[str, str]:
        """返回 (run_key, started_at)。运行中重复启动抛 DiscoveryBusyError。"""
        queries = self._query_factory(tag)
        now = utc_now_iso()
        with self._lock:
            if self._current is not None and self._current["status"] == "running":
                raise DiscoveryBusyError()
        self._ensure_recovered()
        with self._lock:
            # 恢复检查在锁外可能与 UI/调度器的另一次 start 交错；必须在真正
            # 创建 run 前再次检查，避免两个调用同时越过第一道门禁。
            if self._current is not None and self._current["status"] == "running":
                raise DiscoveryBusyError()
            run_key = f"{self.source_name}-{uuid.uuid4().hex[:12]}"
            self._cancel_event = threading.Event()
            self._current = {
                "run_key": run_key,
                "status": "running",
                "started_at": now,
                "error_code": None,
                "planned_requests": len(queries),
                "executed_requests": 0,
            }
        self._thread = threading.Thread(
            target=self._work, args=(run_key, now, queries), daemon=True
        )
        self._thread.start()
        return run_key, now

    def _work(
        self, run_key: str, now: str, queries: list[DiscoveryQuery]
    ) -> None:
        try:
            adapter = (
                self._adapter_factory()
                if self._adapter_factory is not None
                else self._adapter
            )
            service = DiscoveryService(
                adapter, self._scan, self._discovery, self._db_path
            )
            progress = lambda count: self._update_progress(run_key, count)  # noqa: E731
            if self._gate is not None:
                with self._gate.operation() as gate:
                    # 持门后捕获世代：旧 run 的迟到失败不得暂停新登录。
                    epoch = gate.session_epoch()
                    outcome = service.run(
                        run_key=run_key,
                        queries=queries,
                        now_iso=now,
                        cancel_event=self._cancel_event,
                        on_progress=progress,
                    )
                    # 401/403/429 结果在仍持有本次门时提交，随后才释放。
                    self._pause_after_failure(outcome.error_code, epoch)
            else:
                outcome = service.run(
                    run_key=run_key,
                    queries=queries,
                    now_iso=now,
                    cancel_event=self._cancel_event,
                    on_progress=progress,
                )
            finished_at = utc_now_iso()
            with self._lock:
                self._current = {
                    "run_key": run_key,
                    "run_id": outcome.run_id,
                    "status": outcome.status,
                    "started_at": now,
                    "finished_at": finished_at,
                    "executed_requests": outcome.executed_requests,
                    "results_seen": outcome.results_seen,
                    "unique_candidates": outcome.unique_candidates,
                    "error_code": outcome.error_code,
                }
        except PixivOperationInProgress:
            self._finish_blocked(run_key, now, "pixiv_operation_in_progress")
        except PixivPaused as exc:
            self._finish_blocked(
                run_key, now, f"pixiv_operation_paused:{exc.reason or 'unknown'}"
            )
        except PixivCooldownActive:
            self._finish_blocked(run_key, now, "rate_limited")
        except Exception:
            self._finish_blocked(run_key, now, "discovery_runner_error")

    def _update_progress(self, run_key: str, executed: int) -> None:
        with self._lock:
            if (
                self._current is not None
                and self._current.get("run_key") == run_key
                and self._current.get("status") == "running"
            ):
                self._current["executed_requests"] = int(executed)

    def _pause_after_failure(
        self, error_code: str | None, epoch: int | None = None
    ) -> None:
        """401/403/429 后暂停本进程出站，直到用户重新验证并明确恢复。

        ``epoch`` 为本 run 开始时的共享会话世代；会话已重建时旧失败不再提交。
        """
        if self._gate is None or error_code is None:
            return
        if error_code == "rate_limited" or error_code.startswith("rate_limited:"):
            cooldown = None
            if ":" in error_code:
                try:
                    cooldown = float(error_code.split(":", 1)[1])
                except ValueError:
                    cooldown = None
            self._gate.pause(
                "rate_limited", cooldown_seconds=cooldown, epoch=epoch
            )
        elif error_code == "auth_failed":
            self._gate.pause("auth_failed", epoch=epoch)

    def _finish_blocked(self, run_key: str, now: str, error_code: str) -> None:
        with self._lock:
            self._current = {
                "run_key": run_key,
                "status": "failed",
                "started_at": now,
                "finished_at": utc_now_iso(),
                "error_code": error_code,
            }

    def cancel(self) -> bool:
        with self._lock:
            if self._current is None or self._current["status"] != "running":
                return False
            self._cancel_event.set()
            if self._gate is not None:
                self._gate.cancel()
            return True

    def status(self) -> dict:
        with self._lock:
            current = dict(self._current) if self._current is not None else None
        self._ensure_recovered()
        gate_state = self._gate.state() if self._gate is not None else None
        return {
            "state": current["status"] if current is not None else "idle",
            "current_run": current,
            "source": self.source_name,
            "gate": gate_state,
        }

    def shutdown(self, *, timeout: float = 20.0) -> bool:
        """只取消并等待本 runner 自己的线程；不操作其他进程或线程。"""
        self.cancel()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        return thread is None or not thread.is_alive()
