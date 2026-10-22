"""ScanCoordinator：阶段 C 扫描编排。

约定：
- 网络 fetch 在事务外；评估与全部写入在一个短事务内（任一失败整体回滚）；
- run_key 幂等：重复 key 不产生第二个 run，已终态直接返回既有结果；
- 租约：标签级，未过期不可抢占；失败即中止；
- 429/认证/网络/畸形响应 → failed/partial + 事件 + 标签状态（含 cooldown），
  绝不改写目标状态；
- 快照/指标/转换/事件全部幂等（恢复重放安全）。
"""

from __future__ import annotations

import uuid
import time
from collections.abc import Callable
from pathlib import Path

from ..core.logging import redacted_error_text
from ..core.timeutil import add_seconds_iso, utc_now_iso
from ..pixiv.adapter import PixivAdapter
from ..pixiv.exceptions import (
    AuthFailed,
    MalformedResponse,
    NotFound,
    PixivAdapterError,
    RateLimited,
    TemporaryFailure,
)
from ..scoring.metrics import (
    Snapshot,
    ThresholdResult,
    evaluate_target,
    robust_threshold,
)
from ..scoring.params import DEFAULT_PARAMS, PhaseCParams
from ..scoring.state_machine import TargetStateInput, decide_state
from ..storage.connection import connect, transaction
from ..storage.phase_c_repository import PhaseCRepository
from ..storage.scan_repository import ScanRepository
from .broad_scan import ScanOutcome

_ERROR_CODES = {
    AuthFailed: "auth_failed",
    RateLimited: "rate_limited",
    TemporaryFailure: "temporary_failure",
    MalformedResponse: "malformed_response",
    NotFound: "not_found",
}

_DATA_QUALITY_REASONS = {
    "bookmark_exceeds_views",
    "count_regression",
    "time_regression",
    "interval_too_short",
}

DEFAULT_LEASE_TTL_SECONDS = 300.0
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300.0


class ScanCoordinator:
    def __init__(
        self,
        adapter: PixivAdapter,
        scan_repo: ScanRepository,
        phase_repo: PhaseCRepository,
        db_path: str | Path,
        *,
        params: PhaseCParams | None = None,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._adapter = adapter
        self._scan = scan_repo
        self._phase = phase_repo
        self._db_path = Path(db_path)
        self._params = params or DEFAULT_PARAMS
        self._lease_ttl = lease_ttl_seconds
        self._monotonic = monotonic_clock or time.monotonic

    # ---- 入口 ----

    def run(
        self,
        tag: str,
        *,
        run_key: str,
        now_iso: str | None = None,
        source: str = "mock",
        limit: int = 5,
        owner: str | None = None,
    ) -> ScanOutcome:
        now_iso = now_iso or utc_now_iso()
        owner = owner or uuid.uuid4().hex
        resource = f"tag_scan:{tag}"
        lease_started = self._monotonic()

        conn = connect(self._db_path)
        try:
            acquired = self._phase.acquire(
                conn,
                resource,
                owner,
                now_iso=now_iso,
                expires_at=add_seconds_iso(now_iso, self._lease_ttl),
            )
            if not acquired:
                return ScanOutcome(
                    run_id=0,
                    status="failed",
                    results_seen=0,
                    results_written=0,
                    error="lease_held",
                    started_at=now_iso,
                    finished_at=now_iso,
                )
            try:
                return self._run_holding_lease(
                    conn,
                    tag,
                    run_key,
                    now_iso,
                    source,
                    limit,
                    resource,
                    owner,
                    lease_started,
                )
            finally:
                self._phase.release(conn, resource, owner)
        finally:
            conn.close()

    # ---- 主流程 ----

    def _run_holding_lease(
        self,
        conn,
        tag: str,
        run_key: str,
        now_iso: str,
        source: str,
        limit: int,
        resource: str,
        owner: str,
        lease_started: float,
    ) -> ScanOutcome:
        run_id, created = self._phase.create_run(
            conn, run_key=run_key, source=source, layer="broad", started_at=now_iso
        )
        run = self._phase.get_run(conn, run_id)
        # 幂等：run_key 已存在且终态 → 直接返回既有结果，不再 fetch
        if not created and run["finished_at"] is not None:
            return ScanOutcome(
                run_id=run_id,
                status=run["status"],
                results_seen=run["results_seen"],
                results_written=run["results_written"],
                error=run["error"],
                started_at=run["started_at"],
                finished_at=run["finished_at"],
            )

        # ---- 网络阶段（事务外）----
        try:
            novels = self._adapter.search_novels(tag, limit=limit)
        except RateLimited as exc:
            if not self._renew_after_network(
                conn, resource, owner, now_iso, lease_started
            ):
                return self._lease_lost_outcome(run, now_iso)
            return self._abort(
                conn, run_id, "failed", tag, now_iso, exc,
                code="rate_limited", event_type="scan_rate_limited",
                cooldown=exc.cooldown_seconds,
            )
        except MalformedResponse as exc:
            if not self._renew_after_network(
                conn, resource, owner, now_iso, lease_started
            ):
                return self._lease_lost_outcome(run, now_iso)
            return self._abort(
                conn, run_id, "partial", tag, now_iso, exc,
                code="malformed_response", event_type="scan_partial",
            )
        except PixivAdapterError as exc:
            if not self._renew_after_network(
                conn, resource, owner, now_iso, lease_started
            ):
                return self._lease_lost_outcome(run, now_iso)
            return self._abort(
                conn, run_id, "failed", tag, now_iso, exc,
                code=_ERROR_CODES.get(type(exc), "temporary_failure"),
            )
        except Exception as exc:
            if not self._renew_after_network(
                conn, resource, owner, now_iso, lease_started
            ):
                return self._lease_lost_outcome(run, now_iso)
            return self._abort(
                conn, run_id, "failed", tag, now_iso, exc,
                code="internal_error",
            )

        # 网络阶段可能接近 TTL；写入前必须续租并再次证明 owner 身份。
        if not self._renew_after_network(
            conn, resource, owner, now_iso, lease_started
        ):
            return self._lease_lost_outcome(run, now_iso)

        # ---- 评估 + 写入（每个 item 一个短事务，可在进程中断后恢复）----
        try:
            self._write_run(conn, run_id, tag, now_iso, novels)
        except Exception as exc:
            return self._abort(
                conn, run_id, "failed", tag, now_iso, exc, code="internal_error"
            )
        finished = self._phase.get_run(conn, run_id)
        return ScanOutcome(
            run_id=run_id,
            status=finished["status"],
            results_seen=finished["results_seen"],
            results_written=finished["results_written"],
            error=finished["error"],
            started_at=finished["started_at"],
            finished_at=finished["finished_at"],
        )

    def _renew_after_network(
        self,
        conn,
        resource: str,
        owner: str,
        started_at_iso: str,
        lease_started: float,
    ) -> bool:
        elapsed = max(0.0, self._monotonic() - lease_started)
        lease_now = add_seconds_iso(started_at_iso, elapsed)
        return self._phase.renew(
            conn,
            resource,
            owner,
            now_iso=lease_now,
            expires_at=add_seconds_iso(lease_now, self._lease_ttl),
        )

    @staticmethod
    def _lease_lost_outcome(run: dict, now_iso: str) -> ScanOutcome:
        # 不再拥有资源时不得修改 run/tag/target；保留 running 供新 owner 恢复。
        return ScanOutcome(
            run_id=run["id"],
            status="failed",
            results_seen=run["results_seen"],
            results_written=run["results_written"],
            error="lease_lost",
            started_at=run["started_at"],
            finished_at=now_iso,
        )

    def _write_run(self, conn, run_id: int, tag: str, now_iso: str, novels) -> None:
        completed_items = self._phase.list_run_items(conn, run_id)
        completed_ids = {
            item["novel_id"]
            for item in completed_items
            if item["status"] == "succeeded"
        }
        # 防御上游重复 ID；恢复时已成功项绝不重新评估或推进状态。
        current_by_id = {novel.novel_id: novel for novel in novels}
        pending_novels = [
            novel
            for novel_id, novel in current_by_id.items()
            if novel_id not in completed_ids
        ]

        # Pass A：阈值无关评估，收集有效增长信号
        prelim = []
        for novel in pending_novels:
            history = self._snapshot_history(conn, novel.novel_id, exclude_run=run_id)
            current = Snapshot(
                novel.novel_id,
                run_id,
                now_iso,
                novel.total_bookmarks,
                novel.total_view,
                novel.total_comments,
            )
            ev = evaluate_target(
                history + [current],
                params=self._params,
                threshold=ThresholdResult(0.0, "probe", 0),
            )
            prelim.append((novel, history, current, ev))
        signals = [
            metric["ewma_view_velocity"]
            for metric in self._phase.list_run_metrics(conn, run_id)
            if metric["novel_id"] in completed_ids
            and metric["ewma_view_velocity"] is not None
        ] + [
            ev.ewma_view_velocity
            for (_, _, _, ev) in prelim
            if ev.ewma_view_velocity is not None
        ]
        threshold = robust_threshold(signals, self._params)

        seen_ids = completed_ids | set(current_by_id)

        # Pass C：每个目标在独立短事务内原子写入；已完成目标不会进入 prelim。
        for novel, history, current, _ in prelim:
            with transaction(conn):
                self._scan.upsert_target(conn, novel, now_iso=now_iso)
                self._scan.replace_target_tags(conn, novel.novel_id, novel.tags)
                self._scan.insert_snapshot(
                    conn, novel.novel_id, run_id, novel, captured_at=now_iso
                )
                ev = evaluate_target(
                    history + [current], params=self._params, threshold=threshold
                )

                state_row = self._phase.get_target_state(conn, novel.novel_id)
                current_state = (
                    TargetStateInput(
                        state_row["state"],
                        state_row["observation_count"],
                        state_row["promotion_streak"],
                        state_row["demotion_streak"],
                    )
                    if state_row
                    else None
                )
                decision = decide_state(
                    current_state,
                    ev.result,
                    params=self._params,
                    observation_increment=1 if ev.exposure_eligible else 0,
                )

                self._phase.insert_metric(
                    conn,
                    novel.novel_id,
                    run_id,
                    exposure_eligible=ev.exposure_eligible,
                    smoothed_bookmark_rate=ev.smoothed_bookmark_rate,
                    view_velocity=ev.view_velocity,
                    bookmark_velocity=ev.bookmark_velocity,
                    ewma_view_velocity=ev.ewma_view_velocity,
                    threshold=ev.threshold,
                    threshold_source=ev.threshold_source,
                    sample_count=ev.sample_count,
                    result=ev.result,
                    reason_codes=list(ev.reason_codes),
                    created_at=now_iso,
                )
                self._phase.upsert_target_state(
                    conn,
                    novel.novel_id,
                    state=decision.new_state,
                    observation_count=decision.observation_count,
                    promotion_streak=decision.promotion_streak,
                    demotion_streak=decision.demotion_streak,
                    last_evaluated_run_id=run_id,
                    updated_at=now_iso,
                )
                for t in decision.transitions:
                    self._phase.insert_transition(
                        conn,
                        novel_id=novel.novel_id,
                        from_state=t.from_state,
                        to_state=t.to_state,
                        reason_code=t.reason_code,
                        evidence=t.evidence,
                        scan_run_id=run_id,
                        created_at=now_iso,
                    )
                for e in decision.events:
                    self._phase.insert_event(
                        conn,
                        event_key=f"{e.type}:{novel.novel_id}:{run_id}",
                        type_=e.type,
                        severity=e.severity,
                        payload=e.payload,
                        novel_id=novel.novel_id,
                        scan_run_id=run_id,
                        created_at=now_iso,
                    )
                if (
                    ev.result == "unknown"
                    and set(ev.reason_codes) & _DATA_QUALITY_REASONS
                ):
                    self._phase.insert_event(
                        conn,
                        event_key=f"data_quality_unknown:{novel.novel_id}:{run_id}",
                        type_="data_quality_unknown",
                        severity="warning",
                        payload={"reasons": list(ev.reason_codes)},
                        novel_id=novel.novel_id,
                        scan_run_id=run_id,
                        created_at=now_iso,
                    )
                self._phase.upsert_run_item(
                    conn,
                    run_id,
                    novel.novel_id,
                    status="succeeded",
                    attempt=1,
                    completed_at=now_iso,
                )
                succeeded_count = sum(
                    item["status"] == "succeeded"
                    for item in self._phase.list_run_items(conn, run_id)
                )
                self._phase.update_run_progress(
                    conn,
                    run_id,
                    results_seen=len(seen_ids),
                    results_written=succeeded_count,
                )

        succeeded_ids = {
            item["novel_id"]
            for item in self._phase.list_run_items(conn, run_id)
            if item["status"] == "succeeded"
        }
        with transaction(conn):
            self._phase.finish_run(
                conn,
                run_id,
                status="succeeded",
                finished_at=now_iso,
                results_seen=len(seen_ids),
                results_written=len(succeeded_ids),
            )
            self._phase.upsert_tag_state(
                conn,
                tag,
                last_success_at=now_iso,
                last_error_code=None,
                cooldown_until=None,
            )

    def _abort(
        self,
        conn,
        run_id: int,
        status: str,
        tag: str,
        now_iso: str,
        exc: Exception,
        *,
        code: str,
        event_type: str | None = None,
        cooldown: float | None = None,
    ) -> ScanOutcome:
        error_text = redacted_error_text(exc)
        existing = self._phase.get_run(conn, run_id)
        results_seen = existing["results_seen"] if existing else 0
        results_written = existing["results_written"] if existing else 0
        with transaction(conn):
            self._phase.finish_run(
                conn,
                run_id,
                status=status,
                finished_at=now_iso,
                error=error_text,
                results_seen=results_seen,
                results_written=results_written,
            )
            if code == "rate_limited":
                self._phase.upsert_tag_state(
                    conn,
                    tag,
                    last_error_code="rate_limited",
                    cooldown_until=add_seconds_iso(
                        now_iso, cooldown or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                    ),
                )
            else:
                self._phase.upsert_tag_state(conn, tag, last_error_code=code)
            if event_type:
                payload: dict = {"code": code}
                if cooldown is not None:
                    payload["cooldown_seconds"] = cooldown
                self._phase.insert_event(
                    conn,
                    event_key=f"{event_type}:{run_id}",
                    type_=event_type,
                    severity="warning",
                    payload=payload,
                    scan_run_id=run_id,
                    created_at=now_iso,
                )
        return ScanOutcome(
            run_id=run_id,
            status=status,
            results_seen=results_seen,
            results_written=results_written,
            error=error_text,
            cooldown_seconds=cooldown,
            started_at=now_iso,
            finished_at=now_iso,
        )

    # ---- 历史读取 ----

    def _snapshot_history(
        self, conn, novel_id: int, *, exclude_run: int, limit: int = 200
    ) -> list[Snapshot]:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT novel_id, scan_run_id, captured_at,
                       total_bookmarks, total_view, total_comments
                  FROM metric_snapshots
                 WHERE novel_id = ? AND scan_run_id != ?
                 ORDER BY captured_at DESC, scan_run_id DESC
                 LIMIT ?
            ) recent
            ORDER BY captured_at ASC, scan_run_id ASC
            """,
            (novel_id, exclude_run, limit),
        ).fetchall()
        return [
            Snapshot(
                row["novel_id"],
                row["scan_run_id"],
                row["captured_at"],
                row["total_bookmarks"],
                row["total_view"],
                row["total_comments"],
            )
            for row in rows
        ]
