"""DiscoveryService：离线多来源召回（Search-1）。

调用图见 docs/PHASE_E_R1_SEARCH_1_CONTRACT.md §10：
1. 预算规划（纯函数）；
2. 网络阶段：PixivDiscoveryAdapter.fetch_page × N（不持有写事务）；
3. 单短事务：候选 targets/tags/snapshots + 全部 sightings + run 终态；
4. 返回 DiscoveryOutcome。

错误语义：429/AuthFailed 立即停止（零成功=failed，已有成功=partial）；
TemporaryFailure/MalformedResponse 记录受控码后继续后续来源（最终 partial）；
空列表=成功零结果。run_key 重放不再调用 adapter、不重复 sightings。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..core.timeutil import utc_now_iso
from ..pixiv.adapter import NovelSummary
from ..pixiv.exceptions import (
    AuthFailed,
    MalformedResponse,
    PixivAdapterError,
    RateLimited,
    TemporaryFailure,
)
from ..storage.connection import connect, transaction
from ..storage.discovery_repository import DiscoveryRepository
from ..storage.phase_c_repository import PhaseCRepository
from ..storage.scan_repository import ScanRepository
from ..scoring.metrics import Snapshot, ThresholdResult, evaluate_target, robust_threshold
from ..scoring.params import DEFAULT_PARAMS
from ..scoring.state_machine import TargetStateInput, decide_state
from . import MAX_UNIQUE_CANDIDATES, PER_REQUEST_LIMIT
from .adapter import PixivDiscoveryAdapter
from .budget import plan_budget
from .merge import merge_batches, select_candidates
from .types import DiscoveryBatch, DiscoveryOutcome, DiscoveryQuery, validate_discovery_query


class DiscoveryService:
    def __init__(
        self,
        adapter: PixivDiscoveryAdapter,
        scan_repo: ScanRepository,
        discovery_repo: DiscoveryRepository,
        db_path: str | Path,
    ) -> None:
        self._adapter = adapter
        self._scan = scan_repo
        self._discovery = discovery_repo
        self._phase = PhaseCRepository()
        self._db_path = Path(db_path)

    def run(
        self,
        *,
        run_key: str,
        queries: list[DiscoveryQuery],
        now_iso: str | None = None,
        cancel_event=None,
        on_progress: Callable[[int], None] | None = None,
    ) -> DiscoveryOutcome:
        now = now_iso or utc_now_iso()
        # This is deliberately before both the budget planner and create_run:
        # invalid input must not call an adapter or leave a running row.
        self._validate_run_input(run_key, queries)
        plan = plan_budget(queries)

        conn = connect(self._db_path)
        try:
            run_id, created = self._discovery.create_run(
                conn,
                run_key=run_key,
                started_at=now,
                planned_requests=plan.planned_requests,
            )
            existing = self._discovery.get_run(conn, run_id)
            # run_key 重放：已终态直接返回既有结果，不再调用 adapter
            if not created and existing is not None and existing["finished_at"] is not None:
                return DiscoveryOutcome(
                    run_id=run_id,
                    status=existing["status"],
                    planned_requests=existing["planned_requests"],
                    executed_requests=existing["executed_requests"],
                    results_seen=existing["results_seen"],
                    unique_candidates=existing["unique_candidates"],
                    error_code=existing["error_code"],
                )

            # ---- 网络阶段（无写事务）----
            batches: list[DiscoveryBatch] = []
            executed = 0
            successful_requests = 0
            stop_code: str | None = None
            stop_now = False
            for query in plan.queries:
                # 显式取消：在下一个来源请求之前检查，立即停止（不留永久 running）
                if cancel_event is not None and cancel_event.is_set():
                    stop_code = "cancelled"
                    stop_now = True
                    break
                # Count an attempted adapter call even when it raises.
                executed += 1
                if on_progress is not None:
                    try:
                        on_progress(executed)
                    except Exception:
                        pass
                try:
                    page = self._adapter.fetch_page(query, None)
                except RateLimited as exc:
                    cooldown = exc.cooldown_seconds
                    if (
                        isinstance(cooldown, (int, float))
                        and cooldown == cooldown
                        and cooldown not in (float("inf"), float("-inf"))
                        and cooldown > 0
                    ):
                        stop_code = f"rate_limited:{float(cooldown):.0f}"
                    else:
                        stop_code = "rate_limited"
                    stop_now = True
                    break
                except AuthFailed:
                    stop_code = "auth_failed"
                    stop_now = True
                    break
                except MalformedResponse:
                    stop_code = "malformed_response"
                    continue
                except TemporaryFailure:
                    stop_code = "temporary_failure"
                    continue
                except PixivAdapterError:
                    stop_code = "temporary_failure"
                    continue
                successful_requests += 1
                # A non-compliant adapter cannot bypass the page hard limit.
                items = tuple(page.items[: min(query.limit, PER_REQUEST_LIMIT)])
                batches.append(
                    DiscoveryBatch(
                        query=query,
                        page=type(page)(items=items, next_cursor=page.next_cursor),
                    )
                )

            results_seen = sum(len(batch.page.items) for batch in batches)

            if stop_code == "cancelled":
                final_status = "cancelled"
            elif stop_code is not None and successful_requests == 0:
                final_status = "failed"
            elif stop_now or stop_code is not None:
                final_status = "partial"
            else:
                final_status = "succeeded"

            # ---- 合并（纯函数；按 novel_id 排序，确定性）----
            try:
                candidates = merge_batches(batches, discovered_at=now)
                candidates = select_candidates(candidates, MAX_UNIQUE_CANDIDATES)
            except Exception:
                return self._mark_failed(
                    conn,
                    run_id=run_id,
                    now=now,
                    planned_requests=plan.planned_requests,
                    executed=executed,
                    results_seen=results_seen,
                )

            try:
                # 复用本次发现已经取得的元数据做阶段 C 本地评估；不增加 API 请求。
                # 计算在写事务外完成，事务内只保存快照、指标和状态变化。
                evaluations, threshold = self._evaluate_candidates(
                    conn, candidates, now=now
                )
                # ---- 单短事务落库（无"有 sighting 无 target"孤儿态）----
                # The paired scan run uses the same truthful terminal status;
                # scan_runs already accepts the Search-1 status vocabulary.
                with transaction(conn):
                    snapshot_run_id, _ = self._phase.create_run(
                        conn,
                        run_key=f"discovery:{run_key}",
                        source=getattr(self._adapter, "source_name", "mock"),
                        layer="discovery",
                        started_at=now,
                    )
                    for candidate in candidates:
                        novel: NovelSummary = candidate.novel
                        self._scan.upsert_target(conn, novel, now_iso=now)
                        self._scan.replace_target_tags(conn, novel.novel_id, novel.tags)
                        self._scan.insert_snapshot(
                            conn, novel.novel_id, snapshot_run_id, novel, captured_at=now
                        )
                        evaluation = evaluate_target(
                            [
                                *evaluations[novel.novel_id],
                                Snapshot(
                                    novel.novel_id,
                                    snapshot_run_id,
                                    now,
                                    novel.total_bookmarks,
                                    novel.total_view,
                                    novel.total_comments,
                                ),
                            ],
                            params=DEFAULT_PARAMS,
                            threshold=threshold,
                        )
                        state_row = self._phase.get_target_state(
                            conn, novel.novel_id
                        )
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
                            evaluation.result,
                            params=DEFAULT_PARAMS,
                            observation_increment=(
                                1 if evaluation.exposure_eligible else 0
                            ),
                        )
                        self._phase.insert_metric(
                            conn,
                            novel.novel_id,
                            snapshot_run_id,
                            exposure_eligible=evaluation.exposure_eligible,
                            smoothed_bookmark_rate=evaluation.smoothed_bookmark_rate,
                            view_velocity=evaluation.view_velocity,
                            bookmark_velocity=evaluation.bookmark_velocity,
                            ewma_view_velocity=evaluation.ewma_view_velocity,
                            threshold=evaluation.threshold,
                            threshold_source=evaluation.threshold_source,
                            sample_count=evaluation.sample_count,
                            result=evaluation.result,
                            reason_codes=list(evaluation.reason_codes),
                            created_at=now,
                        )
                        self._phase.upsert_target_state(
                            conn,
                            novel.novel_id,
                            state=decision.new_state,
                            observation_count=decision.observation_count,
                            promotion_streak=decision.promotion_streak,
                            demotion_streak=decision.demotion_streak,
                            last_evaluated_run_id=snapshot_run_id,
                            updated_at=now,
                        )
                        for transition in decision.transitions:
                            self._phase.insert_transition(
                                conn,
                                novel_id=novel.novel_id,
                                from_state=transition.from_state,
                                to_state=transition.to_state,
                                reason_code=transition.reason_code,
                                evidence=transition.evidence,
                                scan_run_id=snapshot_run_id,
                                created_at=now,
                            )
                        for event in decision.events:
                            self._phase.insert_event(
                                conn,
                                event_key=(
                                    f"{event.type}:{novel.novel_id}:"
                                    f"{snapshot_run_id}"
                                ),
                                type_=event.type,
                                severity=event.severity,
                                payload=event.payload,
                                novel_id=novel.novel_id,
                                scan_run_id=snapshot_run_id,
                                created_at=now,
                            )
                        self._phase.upsert_run_item(
                            conn,
                            snapshot_run_id,
                            novel.novel_id,
                            status="succeeded",
                            attempt=1,
                            completed_at=now,
                        )
                        for sighting in candidate.sightings:
                            self._discovery.insert_sighting(
                                conn,
                                discovery_run_id=run_id,
                                novel_id=sighting.novel_id,
                                source_kind=sighting.source_kind,
                                query_key=sighting.query_key,
                                query_text=sighting.query_text,
                                source_rank=sighting.source_rank,
                                discovered_at=sighting.discovered_at,
                            )
                    self._phase.finish_run(
                        conn,
                        snapshot_run_id,
                        status=final_status,
                        finished_at=now,
                        results_seen=results_seen,
                        results_written=len(candidates),
                    )
                    self._discovery.finish_run(
                        conn,
                        run_id,
                        status=final_status,
                        finished_at=now,
                        executed_requests=executed,
                        results_seen=results_seen,
                        unique_candidates=len(candidates),
                        error_code=stop_code,
                    )
            except Exception:
                return self._mark_failed(
                    conn,
                    run_id=run_id,
                    now=now,
                    planned_requests=plan.planned_requests,
                    executed=executed,
                    results_seen=results_seen,
                )
            return DiscoveryOutcome(
                run_id=run_id,
                status=final_status,
                planned_requests=plan.planned_requests,
                executed_requests=executed,
                results_seen=results_seen,
                unique_candidates=len(candidates),
                error_code=stop_code,
            )
        finally:
            conn.close()

    def _evaluate_candidates(self, conn, candidates, *, now: str):
        """返回 novel_id→既有快照及本批动态门限（纯本地读取/计算）。"""
        histories: dict[int, list[Snapshot]] = {}
        signals: list[float] = []
        probe_threshold = ThresholdResult(0.0, "probe", 0)
        for candidate in candidates:
            novel = candidate.novel
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT novel_id, scan_run_id, captured_at,
                           total_bookmarks, total_view, total_comments
                      FROM metric_snapshots
                     WHERE novel_id = ?
                     ORDER BY captured_at DESC, scan_run_id DESC
                     LIMIT 200
                ) recent
                ORDER BY captured_at ASC, scan_run_id ASC
                """,
                (novel.novel_id,),
            ).fetchall()
            history = [
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
            histories[novel.novel_id] = history
            probe = evaluate_target(
                [
                    *history,
                    Snapshot(
                        novel.novel_id,
                        0,
                        now,
                        novel.total_bookmarks,
                        novel.total_view,
                        novel.total_comments,
                    ),
                ],
                params=DEFAULT_PARAMS,
                threshold=probe_threshold,
            )
            if probe.ewma_view_velocity is not None:
                signals.append(probe.ewma_view_velocity)
        return histories, robust_threshold(signals, DEFAULT_PARAMS)

    @staticmethod
    def _validate_run_input(run_key: str, queries: list[DiscoveryQuery]) -> None:
        if not isinstance(run_key, str) or not run_key:
            raise ValueError("run_key must be a non-empty string")
        by_key: dict[str, tuple[object, ...]] = {}
        for query in queries:
            validate_discovery_query(query)
            fingerprint = (
                query.source_kind,
                query.text,
                query.entity_id,
                query.search_target,
                query.limit,
                query.enabled,
            )
            previous = by_key.setdefault(query.query_key, fingerprint)
            if previous != fingerprint:
                raise ValueError("query_key maps to conflicting query configuration")

    def _mark_failed(
        self,
        conn,
        *,
        run_id: int,
        now: str,
        planned_requests: int,
        executed: int,
        results_seen: int,
    ) -> DiscoveryOutcome:
        """Finalize a created run after merge/persistence rollback."""
        try:
            with transaction(conn):
                self._discovery.finish_run(
                    conn,
                    run_id,
                    status="failed",
                    finished_at=now,
                    executed_requests=executed,
                    results_seen=results_seen,
                    unique_candidates=0,
                    error_code="persistence_failed",
                )
        except Exception as finalize_error:
            raise RuntimeError("discovery run could not be finalized") from finalize_error
        return DiscoveryOutcome(
            run_id=run_id,
            status="failed",
            planned_requests=planned_requests,
            executed_requests=executed,
            results_seen=results_seen,
            unique_candidates=0,
            error_code="persistence_failed",
        )
