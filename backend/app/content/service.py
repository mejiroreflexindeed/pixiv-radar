"""按需正文分析服务：网络和计算不持有 SQLite 写事务。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from ..core.timeutil import utc_now_iso
from ..pixiv.adapter import PixivAdapter
from ..pixiv.exceptions import AuthFailed, RateLimited
from ..pixiv.request_gate import (
    PixivCooldownActive,
    PixivOperationInProgress,
    PixivPaused,
)
from ..storage.connection import connect, transaction
from ..storage.content_repository import ContentRepository
from .analyzer import ANALYZER_VERSION, analyze_content
from .tfidf import cosine_similarity, fit_transform

_MAX_BODY_CHARS = 5_000_000
_SIMILARITY_TEXT_CHARS = 100_000
_SIMILARITY_CANDIDATES = 200


class ContentTargetNotFound(LookupError):
    pass


class ContentTargetNotEligible(RuntimeError):
    pass


class ContentAnalysisUnavailable(RuntimeError):
    pass


class ContentOperationPaused(RuntimeError):
    """共享门因 401/403/429 暂停时的受控拒绝；不发起任何出站。"""

    code = "pixiv_operation_paused"

    def __init__(self, reason: str | None) -> None:
        super().__init__(self.code)
        self.reason = reason


class ContentOperationConflict(RuntimeError):
    code = "pixiv_operation_in_progress"


class ContentRateLimited(RuntimeError):
    code = "pixiv_rate_limited"

    def __init__(self, cooldown_seconds: float | None) -> None:
        super().__init__(self.code)
        self.cooldown_seconds = cooldown_seconds


class ContentAnalysisService:
    def __init__(
        self,
        repository: ContentRepository,
        db_path: str | Path,
        *,
        adapter_factory: Callable[[], PixivAdapter] | None = None,
        gate=None,
    ) -> None:
        self._repository = repository
        self._db_path = db_path
        self._adapter_factory = adapter_factory
        self._gate = gate

    @property
    def available(self) -> bool:
        return self._adapter_factory is not None

    def get(self, novel_id: int) -> dict | None:
        conn = connect(self._db_path)
        try:
            state = self._repository.get_target_state(conn, novel_id)
            if state is None:
                raise ContentTargetNotFound
            return self._repository.get_analysis(conn, novel_id)
        finally:
            conn.close()

    def analyze(self, novel_id: int, *, force: bool = False) -> tuple[dict, bool]:
        conn = connect(self._db_path)
        try:
            state = self._repository.get_target_state(conn, novel_id)
            if state is None:
                raise ContentTargetNotFound
            if state != "track":
                raise ContentTargetNotEligible
            existing = self._repository.get_analysis(conn, novel_id)
            if (
                not force
                and existing is not None
                and existing.get("analyzer_version") == ANALYZER_VERSION
            ):
                return existing, True
        finally:
            conn.close()

        if self._adapter_factory is None:
            raise ContentAnalysisUnavailable
        adapter = self._adapter_factory()
        if self._gate is not None:
            try:
                with self._gate.operation() as gate:
                    # 持门后捕获世代：acquire 与调用之间不会拿到旧世代。
                    epoch = gate.session_epoch()
                    try:
                        fetched = adapter.novel_text(novel_id)
                    except AuthFailed:
                        # 401/403 与发现一致：在仍持有本次门时提交暂停，再释放，
                        # 避免下一次授权在冷却提交前抢先开始。
                        gate.pause("auth_failed", epoch=epoch)
                        raise
                    except RateLimited as exc:
                        gate.pause(
                            "rate_limited",
                            cooldown_seconds=exc.cooldown_seconds,
                            epoch=epoch,
                        )
                        raise
            except PixivOperationInProgress:
                raise ContentOperationConflict() from None
            except PixivPaused as exc:
                raise ContentOperationPaused(exc.reason) from None
            except PixivCooldownActive as exc:
                raise ContentRateLimited(exc.remaining_seconds) from None
        else:
            fetched = adapter.novel_text(novel_id)
        if fetched.novel_id != novel_id:
            raise ContentAnalysisUnavailable("content_id_mismatch")
        body = fetched.text
        if not isinstance(body, str) or len(body) > _MAX_BODY_CHARS:
            raise ContentAnalysisUnavailable("content_invalid")

        features = analyze_content(body)
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        now = utc_now_iso()

        # 网络与分析结束后才开启短写事务；写入前重查资格，避免状态变化竞态。
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                state = self._repository.get_target_state(conn, novel_id)
                if state is None:
                    raise ContentTargetNotFound
                if state != "track":
                    raise ContentTargetNotEligible
                self._repository.upsert(
                    conn,
                    novel_id=novel_id,
                    content_hash=content_hash,
                    body_text=body,
                    fetched_at=now,
                    source_revision=None,
                    features=features,
                    analyzed_at=now,
                )
            result = self._repository.get_analysis(conn, novel_id)
        finally:
            conn.close()
        assert result is not None
        return result, False

    def similar(self, novel_id: int, *, limit: int) -> list[dict]:
        """对本地已分析正文做小规模 TF-IDF 相似度，不访问 Pixiv。"""
        conn = connect(self._db_path)
        try:
            state = self._repository.get_target_state(conn, novel_id)
            if state is None:
                raise ContentTargetNotFound
            source = self._repository.get_document(conn, novel_id)
            if source is None:
                return []
            candidates = self._repository.list_similarity_candidates(
                conn,
                exclude_novel_id=novel_id,
                limit=_SIMILARITY_CANDIDATES,
            )
        finally:
            conn.close()
        if not candidates:
            return []
        documents = [source["body_text"][:_SIMILARITY_TEXT_CHARS]] + [
            item["body_text"][:_SIMILARITY_TEXT_CHARS] for item in candidates
        ]
        vectors = fit_transform(documents)
        source_vector = vectors[0]
        results: list[dict] = []
        for item, vector in zip(candidates, vectors[1:]):
            score = cosine_similarity(source_vector, vector)
            if score <= 0:
                continue
            results.append(
                {
                    "novel_id": item["novel_id"],
                    "title": item["title"],
                    "similarity": round(score, 4),
                    "structure_score": item["structure_score"],
                    "is_clutter": bool(item["is_clutter"]),
                    "is_suspect": bool(item["is_suspect"]),
                    "analyzed_at": item["analyzed_at"],
                }
            )
        results.sort(key=lambda item: (-item["similarity"], item["novel_id"]))
        return results[:limit]
