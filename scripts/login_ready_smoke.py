"""登录可用版离线组合 smoke（W08）：同一条生产调用链 + fake transport。

- 只使用显式独立 fixture、内存凭据 store、fake 官方授权与 fake Pixiv API；
  不读取真实凭据、不访问真实 Pixiv、不连接或改写 data/state 正式库。
- 通过 create_app / 真实 API 路由 / 真实 service / SQLite 持久化组合验证。
- 报告明确 transport=fake、live=false；任何阶段断言失败立即非零退出，不把
  异常吞掉后输出 passed。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.auth.service import (  # noqa: E402
    AuthCapability,
    AuthService,
    ProviderLaunch,
)
from app.core.config import get_settings  # noqa: E402
from app.core.credentials import InMemoryCredentialStore  # noqa: E402
from app.main import _default_validator, create_app  # noqa: E402
from app.pixiv.request_gate import RequestGate  # noqa: E402
from app.storage.backup import BackupError, backup_database, restore_database  # noqa: E402
from app.storage.connection import connect  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = PROJECT_ROOT / "docs" / "reviews" / "login-ready-20260909-01" / "smoke-w08-01"
FAKE_TOKEN = "FAKE-OFFLINE-TOKEN-NOT-REAL"
FAKE_BODY_SENTINEL = "FAKE-正文-SENTINEL"
TAG = "ウマ娘"
NOVEL_IDS = (9001, 9002)


class CountingStore(InMemoryCredentialStore):
    """内存凭据；只记录 get/set/delete 次数，绝不输出值。"""

    def __init__(self) -> None:
        super().__init__(None)
        self.reads = 0
        self.writes = 0
        self.deletes = 0

    def get(self):
        self.reads += 1
        return super().get()

    def set(self, token: str) -> None:
        self.writes += 1
        super().set(token)

    def delete(self) -> None:
        self.deletes += 1
        super().delete()


class FakeOAuthProvider:
    """fake 官方授权：只走控制流，token 是固定离线哨兵值。"""

    available = True
    mode = "fake_oauth"

    def __init__(self, token: str) -> None:
        self._token = token
        self.starts = 0

    def start(self, request, *, on_complete, on_failure):
        self.starts += 1
        on_complete(request.state, self._token)
        return ProviderLaunch(mode="fake_oauth", browser_opened=True)

    def cancel(self, session_id: str) -> None:
        del session_id


class FakeHTTPError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}: fake offline upstream")
        self.status_code = status
        self.response = SimpleNamespace(
            status_code=status, headers={"Retry-After": "60"}
        )


def _novel(base_id: int, round_index: int):
    views = 1000 + round_index * 250
    bookmarks = 50 + round_index * 12
    return SimpleNamespace(
        id=base_id,
        title=f"fake-novel-{base_id}",
        caption="",
        user=SimpleNamespace(id=77, name="fake-author"),
        series=None,
        tags=[SimpleNamespace(name=TAG, translated_name=None)],
        text_length=len(FAKE_BODY_SENTINEL) * 20,
        total_bookmarks=bookmarks,
        total_view=views,
        total_comments=0,
        is_original=True,
        create_date="2026-09-01T00:00:00+00:00",
        x_restrict=0,
        visible=True,
        novel_ai_type=0,
    )


class FakeUpstream:
    """记录 fake 上游调用次数/顺序；不联网。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.auth_calls = 0
        self.search_calls: list[tuple[str, str]] = []
        self.text_calls = 0
        self.round = 0
        self.failure: Exception | None = None
        self.fail_targets: set[str] = set()
        self.block_search = threading.Event()
        self.entered_search = threading.Event()
        self.release_search = threading.Event()

    def factory(self, token: str):
        del token
        with self.lock:
            self.auth_calls += 1
        return FakeAppPixivAPI(self)

    def search(self, *, word: str, search_target: str, sort: str):
        del sort
        with self.lock:
            self.search_calls.append((word, search_target))
        if self.block_search.is_set():
            self.entered_search.set()
            self.release_search.wait(timeout=15)
        if self.failure is not None:
            raise self.failure
        if search_target in self.fail_targets:
            raise ConnectionError("connection reset by fake upstream")
        return SimpleNamespace(
            novels=[_novel(novel_id, self.round) for novel_id in NOVEL_IDS],
            next_url=None,
        )

    def webview(self, novel_id: int):
        with self.lock:
            self.text_calls += 1
        return {"novel_text": f"{FAKE_BODY_SENTINEL} " * 20, "id": novel_id}


class FakeAppPixivAPI:
    def __init__(self, upstream: FakeUpstream) -> None:
        self._upstream = upstream

    def set_accept_language(self, _language: str) -> None:
        return None

    def search_novel(self, **kwargs):
        return self._upstream.search(
            word=kwargs.get("word", ""),
            search_target=kwargs.get("search_target", ""),
            sort=kwargs.get("sort", ""),
        )

    def webview_novel(self, novel_id: int):
        return self._upstream.webview(novel_id)


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self.current.isoformat()

    def advance(self, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protected_snapshot() -> dict[str, object]:
    """只记录正式 data/state 的文件名/大小/hash，不连接 SQLite。"""
    state = PROJECT_ROOT / "data" / "state"
    if not state.is_dir():
        return {"exists": False, "files": {}}
    files = {}
    for path in sorted(state.iterdir()):
        if path.is_file():
            files[path.name] = {
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
    return {"exists": True, "files": files}


def _wait_terminal(client: TestClient, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/api/discovery/status").json()
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("discovery run did not terminate")


def _build_app(*, db_path: Path, store: CountingStore, gate: RequestGate):
    os.environ["RADAR_DB_PATH"] = str(db_path)
    os.environ["RADAR_DISCOVERY_MODE"] = "real"
    os.environ["RADAR_AUTH_ALLOW_REAL"] = "1"
    os.environ["RADAR_DISCOVERY_INTERVAL_MINUTES"] = "0"
    get_settings.cache_clear()
    auth_service = AuthService(
        store,
        FakeOAuthProvider(FAKE_TOKEN),
        allow_real=lambda: True,
        validator=_default_validator(gate),
        capability=lambda: AuthCapability(True, True, None),
        gate=gate,
    )
    return create_app(
        auth_service=auth_service, credential_store=store, request_gate=gate
    )


def _start_and_wait(client: TestClient) -> dict:
    response = client.post("/api/discovery/start", params={"tag": TAG})
    assert response.status_code == 200, response.text
    return _wait_terminal(client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--keep-fixture", action="store_true")
    args = parser.parse_args(argv)

    fixture = args.fixture_dir.resolve(strict=False)
    report: dict[str, object] = {
        "status": "failed",
        "transport": "fake",
        "live": False,
        "fixture": str(fixture),
        "stages": {},
        "credential_counts": {},
        "fake_upstream": {},
    }
    protected_before = _protected_snapshot()
    if fixture.exists():
        report["code"] = "fixture_already_exists"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    upstream = FakeUpstream()
    clock = FakeClock()
    store = CountingStore()
    gate = RequestGate(min_interval_seconds=0.0)
    original_runner_clock = None
    original_discovery_factory = None
    original_runtime_factory = None

    try:
        import app.discovery.runner as runner_module
        import app.pixiv.discovery_pixivpy as discovery_module
        import app.pixiv.runtime as runtime_module

        original_runner_clock = runner_module.utc_now_iso
        original_discovery_factory = discovery_module._default_api_factory
        original_runtime_factory = runtime_module._default_api_factory
        runner_module.utc_now_iso = clock
        discovery_module._default_api_factory = upstream.factory
        runtime_module._default_api_factory = upstream.factory

        fixture.mkdir(parents=True)
        db_path = fixture / "runtime" / "radar.db"
        db_path.parent.mkdir(parents=True)

        # ---- 阶段 1：全新 v6 库；GET 不读凭据、不出站 ----
        app = _build_app(db_path=db_path, store=store, gate=gate)
        with TestClient(app) as client:
            status_codes = {}
            for route in (
                "/api/health",
                "/api/auth/status",
                "/api/discovery/status",
                "/api/targets",
                "/api/dashboard/heatmap",
                "/api/content/status",
            ):
                status_codes[route] = client.get(route).status_code
            auth = client.get("/api/auth/status").json()
            assert all(code == 200 for code in status_codes.values()), status_codes
            assert store.reads == 0, "GET 阶段读取了凭据"
            assert upstream.auth_calls == 0 and upstream.search_calls == []
            assert auth["state"] == "not_configured"
            assert client.get("/api/targets").json()["total"] == 0
            report["stages"]["1_bootstrap"] = {
                "status_codes": status_codes,
                "credential_reads": store.reads,
                "upstream_calls": 0,
                "state": auth["state"],
            }

            # ---- 阶段 2：fake 官方授权完成；不自动 scan ----
            started = client.post(
                "/api/auth/start", json={"user_initiated": True}
            )
            assert started.status_code == 200, started.text
            auth = client.get("/api/auth/status").json()
            assert auth["state"] == "connected", auth
            assert store.writes == 1 and store.get() == FAKE_TOKEN
            assert upstream.search_calls == [] and upstream.text_calls == 0
            report["stages"]["2_fake_login"] = {
                "state": auth["state"],
                "credential_writes": store.writes,
                "auto_scan_started": False,
            }

            # ---- 阶段 3：用户显式发现；三次受控调用；source=pixiv ----
            terminal = _start_and_wait(client)
            assert terminal["state"] == "succeeded", terminal
            assert [call[0] for call in upstream.search_calls] == [TAG, TAG, TAG]
            assert [call[1] for call in upstream.search_calls] == [
                "exact_match_for_tags",
                "partial_match_for_tags",
                "keyword",
            ], upstream.search_calls
            assert terminal["source"] == "pixiv"
            report["stages"]["3_real_chain"] = {
                "source": terminal["source"],
                "transport": "fake",
                "executed_requests": terminal["current_run"]["executed_requests"],
                "search_calls": len(upstream.search_calls),
                "auth_calls": upstream.auth_calls,
            }

            # ---- 阶段 4：候选/详情/历史；首次观察 cold_start ----
            runs = client.get("/api/discovery/runs").json()["items"]
            assert runs and runs[0]["status"] == "succeeded", runs
            run_id = runs[0]["id"]
            candidates = client.get(
                f"/api/discovery/candidates?run_id={run_id}"
            ).json()["items"]
            assert len(candidates) == len(NOVEL_IDS), candidates
            detail = client.get(f"/api/targets/{NOVEL_IDS[0]}")
            history = client.get(f"/api/targets/{NOVEL_IDS[0]}/history")
            assert detail.status_code == 200 and history.status_code == 200
            assert detail.json()["state"] == "cold_start", detail.json()
            assert len(history.json()["items"]) == 1
            report["stages"]["4_first_observation"] = {
                "candidates": len(candidates),
                "state": detail.json()["state"],
                "history_points": len(history.json()["items"]),
                "groups": sorted({item["group"] for item in candidates}),
            }

            # ---- 阶段 5：注入时间 + 真实状态机晋级到 track ----
            for _ in range(2):
                clock.advance(10)
                upstream.round += 1
                terminal = _start_and_wait(client)
                assert terminal["state"] == "succeeded", terminal
            detail = client.get(f"/api/targets/{NOVEL_IDS[0]}").json()
            assert detail["state"] == "track", detail
            history = client.get(f"/api/targets/{NOVEL_IDS[0]}/history").json()
            events = client.get("/api/events?limit=50").json()["items"]
            assert any(
                event["type"] == "target_entered_track"
                and event["novel_id"] == NOVEL_IDS[0]
                for event in events
            ), events
            report["stages"]["5_promotion"] = {
                "state": detail["state"],
                "observation_count": detail["observation_count"],
                "history_points": len(history["items"]),
                "entered_track_event": True,
            }

            # ---- 阶段 6：track 按篇正文分析；缓存/相似零出站；无原文 ----
            before_text_calls = upstream.text_calls
            analyzed = client.post(f"/api/content/{NOVEL_IDS[0]}/analyze")
            assert analyzed.status_code == 200, analyzed.text
            payload = analyzed.json()
            assert upstream.text_calls == before_text_calls + 1
            assert "body_text" not in payload and "content_hash" not in payload
            assert FAKE_BODY_SENTINEL not in json.dumps(payload, ensure_ascii=False)
            cached = client.get(f"/api/content/{NOVEL_IDS[0]}")
            similar = client.get(f"/api/content/{NOVEL_IDS[0]}/similar")
            assert cached.status_code == 200 and similar.status_code == 200
            assert upstream.text_calls == before_text_calls + 1, "缓存/相似不应出站"
            forced = client.post(f"/api/content/{NOVEL_IDS[0]}/analyze?force=true")
            assert forced.status_code == 200
            assert upstream.text_calls == before_text_calls + 2
            assert FAKE_BODY_SENTINEL not in json.dumps(
                forced.json(), ensure_ascii=False
            )
            report["stages"]["6_content"] = {
                "analyzed": True,
                "cached_read_no_outbound": True,
                "similar_read_no_outbound": True,
                "force_refetch": True,
                "body_exposed": False,
            }

            # ---- 阶段 7：反馈闭环 + 销毁重建 + validate 复用登录 ----
            good = client.post(
                "/api/feedback",
                json={"novel_id": NOVEL_IDS[0], "event_type": "good"},
            )
            assert good.status_code == 200, good.text
            assert good.json()["summary"]["effective_feedback"] == "good"
            excluded = client.post(
                "/api/feedback",
                json={"novel_id": NOVEL_IDS[0], "event_type": "exclude"},
            )
            assert excluded.json()["summary"]["effective_feedback"] == "exclude"
            explained = client.get(
                f"/api/discovery/candidates?run_id={run_id}"
            ).json()["items"]
            target_explanations = next(
                item["explanations"]
                for item in explained
                if item["novel_id"] == NOVEL_IDS[0]
            )
            assert any("排除" in text for text in target_explanations)
            undo = client.post(
                "/api/feedback",
                json={
                    "novel_id": NOVEL_IDS[0],
                    "event_type": "undo",
                    "reverts_event_id": excluded.json()["event"]["id"],
                },
            )
            assert undo.json()["summary"]["effective_feedback"] == "good"
            report["stages"]["7_feedback"] = {
                "post_count": 3,
                "explanation_updated": True,
                "undo_restored_good": True,
            }

        # 应用销毁重建：新进程语义下凭据状态回到未验证，数据仍在
        gate2 = RequestGate(min_interval_seconds=0.0)
        app2 = _build_app(db_path=db_path, store=store, gate=gate2)
        reads_before = store.reads
        with TestClient(app2) as client:
            auth = client.get("/api/auth/status").json()
            assert auth["state"] == "not_configured", auth
            assert store.reads == reads_before, "重启后 GET 读取了凭据"
            assert client.get(
                f"/api/feedback/targets/{NOVEL_IDS[0]}"
            ).json()["summary"]["effective_feedback"] == "good"
            assert client.get(f"/api/content/{NOVEL_IDS[0]}").status_code == 200
            validated = client.post(
                "/api/auth/validate", json={"user_initiated": True}
            )
            assert validated.status_code == 200, validated.text
            assert validated.json()["state"] == "connected"
            assert store.reads == reads_before + 1
            report["stages"]["7b_restart_validate"] = {
                "state_before_validate": "not_configured",
                "credential_reads_before_validate": 0,
                "state_after_validate": "connected",
                "data_persisted": True,
            }

            # ---- 阶段 8：401 / 网络 / 取消 / 429 受控分支 ----
            upstream.failure = FakeHTTPError(401)
            terminal = _start_and_wait(client)
            assert terminal["current_run"]["error_code"] == "auth_failed", terminal
            assert gate2.state()["paused_reason"] == "auth_failed"
            preserved = client.get(
                f"/api/discovery/candidates?run_id={run_id}"
            ).json()["items"]
            assert len(preserved) == len(NOVEL_IDS)
            upstream.failure = None
            recovered = client.post(
                "/api/auth/validate", json={"user_initiated": True}
            )
            assert recovered.status_code == 200, recovered.text
            assert gate2.state()["paused_reason"] is None

            upstream.fail_targets = {"keyword"}
            terminal = _start_and_wait(client)
            assert terminal["state"] == "partial", terminal
            assert terminal["current_run"]["error_code"] == "temporary_failure"
            partial_candidates = client.get(
                f"/api/discovery/candidates?run_id={terminal['current_run']['run_id']}"
            ).json()["items"]
            assert len(partial_candidates) == len(NOVEL_IDS)
            upstream.fail_targets = set()

            upstream.block_search.set()
            started = client.post("/api/discovery/start", params={"tag": TAG})
            assert started.status_code == 200
            assert upstream.entered_search.wait(timeout=10)
            assert client.post("/api/discovery/cancel").json()["cancelled"] is True
            upstream.release_search.set()
            terminal = _wait_terminal(client)
            assert terminal["state"] == "cancelled", terminal
            cancel_state = terminal["state"]
            upstream.block_search.clear()
            upstream.release_search.clear()
            upstream.entered_search.clear()

            searches_before_429 = len(upstream.search_calls)
            upstream.failure = FakeHTTPError(429)
            terminal = _start_and_wait(client)
            assert terminal["current_run"]["error_code"].startswith("rate_limited")
            assert gate2.state()["paused_reason"] == "rate_limited"
            blocked = client.post("/api/discovery/start", params={"tag": TAG})
            assert blocked.status_code == 200
            blocked_status = _wait_terminal(client)
            assert blocked_status["current_run"]["error_code"].startswith(
                "pixiv_operation_paused"
            )
            assert len(upstream.search_calls) == searches_before_429 + 1
            upstream.failure = None
            report["stages"]["8_failures"] = {
                "auth_failed_paused": True,
                "candidates_preserved": True,
                "revalidate_resumed": True,
                "network_partial": True,
                "cancel_terminal": cancel_state,
                "rate_limited_paused": True,
                "blocked_run_no_outbound": True,
            }

        # ---- 阶段 9：隔离同形正式源只读备份 → 新目录恢复 → 新 app 复读 ----
        conn = connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        formal_source = fixture / "formal" / "data" / "state" / "radar.db"
        formal_source.parent.mkdir(parents=True)
        shutil.copyfile(db_path, formal_source)
        formal_root = fixture / "formal"
        backup_path = formal_root / "backups" / "radar.db"
        backup_path.parent.mkdir(parents=True)
        try:
            backup_database(
                formal_source, backup_path, allowed_root=formal_root
            )
            raise AssertionError("默认不得备份正式源")
        except BackupError as exc:
            assert exc.code == "formal_database_not_allowed"
        backup_result = backup_database(
            formal_source,
            backup_path,
            allowed_root=formal_root,
            allow_formal_source=True,
        )
        assert backup_result["integrity"] == "ok"
        restored_dir = formal_root / "restored"
        restore_result = restore_database(
            backup_path, restored_dir, allowed_root=formal_root
        )
        assert restore_result["integrity"] == "ok"
        gate3 = RequestGate(min_interval_seconds=0.0)
        app3 = _build_app(db_path=restored_dir / "radar.db", store=store, gate=gate3)
        with TestClient(app3) as client:
            restored_runs = client.get("/api/discovery/runs").json()["items"]
            assert restored_runs, restored_runs
            restored_candidates = client.get(
                f"/api/discovery/candidates?run_id={run_id}"
            ).json()["items"]
            restored_feedback = client.get(
                f"/api/feedback/targets/{NOVEL_IDS[0]}"
            ).json()
            restored_content = client.get(f"/api/content/{NOVEL_IDS[0]}")
            assert len(restored_candidates) == len(NOVEL_IDS)
            assert restored_feedback["summary"]["effective_feedback"] == "good"
            assert restored_content.status_code == 200
        report["stages"]["9_backup_restore"] = {
            "default_rejected": True,
            "explicit_readonly_backup": True,
            "restore_new_directory": True,
            "candidates_read": len(restored_candidates),
            "feedback_read": restored_feedback["summary"]["effective_feedback"],
            "content_cached": True,
            "schema": backup_result["schema_version"],
            "integrity": backup_result["integrity"],
            "foreign_keys": backup_result["foreign_keys"],
        }

        protected_after = _protected_snapshot()
        assert protected_after == protected_before, "正式 data/state 被改动"
        report["protected_data_state_unchanged"] = True
        report["credential_counts"] = {
            "get": store.reads,
            "set": store.writes,
            "delete": store.deletes,
        }
        report["fake_upstream"] = {
            "auth_calls": upstream.auth_calls,
            "search_calls": len(upstream.search_calls),
            "text_calls": upstream.text_calls,
            "min_interval_seconds": 0.0,
            "note": "3 秒真实间隔由 W03 独立探针覆盖",
        }
        report["status"] = "passed"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - 受控失败报告，绝不输出 passed
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["credential_counts"] = {
            "get": store.reads,
            "set": store.writes,
            "delete": store.deletes,
        }
        report["fake_upstream"] = {
            "auth_calls": upstream.auth_calls,
            "search_calls": len(upstream.search_calls),
            "text_calls": upstream.text_calls,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    finally:
        import app.discovery.runner as runner_module
        import app.pixiv.discovery_pixivpy as discovery_module
        import app.pixiv.runtime as runtime_module

        if original_runner_clock is not None:
            runner_module.utc_now_iso = original_runner_clock
        if original_discovery_factory is not None:
            discovery_module._default_api_factory = original_discovery_factory
        if original_runtime_factory is not None:
            runtime_module._default_api_factory = original_runtime_factory
        if not args.keep_fixture:
            shutil.rmtree(fixture, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
