"""进程内低频发现调度器。

默认不创建。真实入口显式给出周期后才启动，并先等待一个完整周期；因此应用启动、
迁移和页面加载仍然不会读取凭据或联网。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from collections.abc import Callable

from ..discovery.runner import DiscoveryBusyError, DiscoveryRunner


class DiscoveryScheduler:
    def __init__(
        self,
        runner: DiscoveryRunner,
        *,
        interval_seconds: float,
        tag: str,
        can_run: Callable[[], bool] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._runner = runner
        self._interval = float(interval_seconds)
        self._tag = tag
        self._can_run = can_run
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_run_at: str | None = None
        self._last_attempt_at: str | None = None
        self._last_error_code: str | None = None
        self._skipped_reason: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        with self._lock:
            self._next_run_at = self._future_iso()
        self._thread = threading.Thread(
            target=self._loop,
            name="pixiv-radar-discovery-scheduler",
            daemon=True,
        )
        self._thread.start()

    def _future_iso(self) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=self._interval)
        ).isoformat()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            now = datetime.now(timezone.utc).isoformat()
            error_code = None
            skipped_reason = None
            if self._can_run is not None:
                try:
                    ready = self._can_run()
                except Exception:
                    ready = False
                if not ready:
                    skipped_reason = "paused_or_not_verified"
            if skipped_reason is None:
                try:
                    self._runner.start(tag=self._tag)
                except DiscoveryBusyError:
                    error_code = "discovery_run_in_progress"
                except ValueError:
                    error_code = "invalid_discovery_tag"
                except Exception:
                    error_code = "scheduler_start_failed"
            with self._lock:
                self._last_attempt_at = now
                self._last_error_code = error_code
                self._skipped_reason = skipped_reason
                self._next_run_at = self._future_iso()

    def stop(self, *, timeout: float = 5.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            self._next_run_at = None
        return thread is None or not thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            gate = None
            try:
                gate = (self._runner.status() or {}).get("gate")
            except Exception:
                gate = None
            paused = bool(gate and gate.get("paused_reason"))
            return {
                "enabled": True,
                "tag": self._tag,
                "interval_minutes": self._interval / 60.0,
                "next_run_at": self._next_run_at,
                "last_attempt_at": self._last_attempt_at,
                "last_error_code": self._last_error_code,
                "skipped_reason": self._skipped_reason,
                "paused": paused,
                "paused_reason": gate.get("paused_reason") if gate else None,
            }
