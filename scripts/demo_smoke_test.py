"""Demo 一键 smoke（WP7）：全新 fixture → seed → 发现运行（partial→succeeded）→
候选/分组 → good/exclude/undo → 备份 → 恢复 → 复读 → JSON 输出 → 清理。

只使用任务专用 fixture 目录（--fixture-dir，默认不存在才继续）；
不访问 Pixiv、不触碰正式库、不启动长驻服务（使用 TestClient/直接服务调用）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.auth.service import AuthService, UnavailableAuthorizationProvider  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.credentials import InMemoryCredentialStore  # noqa: E402
from app.discovery.demo_fake import DemoFakeDiscoveryAdapter  # noqa: E402
from app.discovery.runner import DiscoveryRunner  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage.backup import backup_database, restore_database  # noqa: E402
from app.storage.connection import connect  # noqa: E402
from app.storage.discovery_repository import DiscoveryRepository  # noqa: E402
from app.storage.migrations import apply_migrations  # noqa: E402
from app.storage.scan_repository import ScanRepository  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = PROJECT_ROOT / "docs" / "reviews" / "2026-09-03-demo-smoke-01"


def _wait_terminal(runner, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.status()
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("run did not terminate")


def _seed(db_path: Path) -> None:
    import subprocess

    subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "seed_e_demo.py"),
            str(db_path),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)

    fixture = args.fixture_dir.resolve(strict=False)
    if fixture.exists():
        print(json.dumps({"status": "failed", "code": "fixture_already_exists", "fixture": str(fixture)}, ensure_ascii=False))
        return 2
    fixture.mkdir(parents=True)
    db_path = fixture / "preview" / "radar.db"

    report: dict = {}
    try:
        # 1) seed 演示数据
        _seed(db_path)

        # 2) 离线发现：首次 partial（keyword 临时失败）、重试 succeeded
        adapter = DemoFakeDiscoveryAdapter()
        runner = DiscoveryRunner(adapter, ScanRepository(), DiscoveryRepository(), db_path)
        runner.start()
        first = _wait_terminal(runner)
        assert first["state"] == "partial", first
        runner.start()
        second = _wait_terminal(runner)
        assert second["state"] == "succeeded", second
        report["discovery"] = {"first": first["state"], "retry": second["state"], "candidates": second["current_run"]["unique_candidates"]}

        # 3) 候选与排序分组（经真实 App 路由）
        get_settings.cache_clear()
        import os

        os.environ["RADAR_DB_PATH"] = str(db_path)
        get_settings.cache_clear()
        app = create_app(
            auth_service=AuthService(InMemoryCredentialStore(), UnavailableAuthorizationProvider())
        )
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            runs = client.get("/api/discovery/runs").json()["items"]
            assert len(runs) == 2
            candidates = client.get(
                f"/api/discovery/candidates?run_id={runs[0]['id']}"
            ).json()["items"]
            assert len(candidates) >= 4
            assert all("group" in c and "explanations" in c for c in candidates)
            groups = {c["group"] for c in candidates}
            report["candidate_groups"] = sorted(groups)

            # 4) 反馈 good/exclude/undo
            target_id = candidates[0]["novel_id"]
            good = client.post(
                "/api/feedback",
                json={"novel_id": target_id, "event_type": "good"},
            ).json()
            assert good["summary"]["effective_feedback"] == "good"
            exclude = client.post(
                "/api/feedback",
                json={"novel_id": target_id, "event_type": "exclude"},
            ).json()
            assert exclude["summary"]["effective_feedback"] == "exclude"
            undo = client.post(
                "/api/feedback",
                json={
                    "novel_id": target_id,
                    "event_type": "undo",
                    "reverts_event_id": exclude["event"]["id"],
                },
            ).json()
            assert undo["summary"]["effective_feedback"] == "good"
            report["feedback_roundtrip"] = True
            report["auth_status"] = client.get("/api/auth/status").json()["provider_mode"]

        # 5) 备份 → 恢复 → 复读相同候选与反馈
        backup_file = fixture / "radar-backup.db"
        backup_result = backup_database(db_path, backup_file, allowed_root=fixture)
        assert backup_result["integrity"] == "ok"
        restored_dir = fixture / "restored"
        restore_result = restore_database(backup_file, restored_dir, allowed_root=fixture)
        assert restore_result["integrity"] == "ok"

        conn = connect(restored_dir / "radar.db")
        try:
            candidate_count = conn.execute(
                "SELECT COUNT(DISTINCT novel_id) AS n FROM discovery_sightings"
            ).fetchone()["n"]
            feedback_count = conn.execute(
                "SELECT COUNT(*) AS n FROM feedback_events"
            ).fetchone()["n"]
            assert candidate_count >= 4
            assert feedback_count == 3
            report["restore"] = {
                "candidates_read": candidate_count,
                "feedback_events_read": feedback_count,
            }
        finally:
            conn.close()

        report["status"] = "passed"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        # 清理任务 fixture（绝对路径已核实；保留报告输出）
        shutil.rmtree(fixture, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
