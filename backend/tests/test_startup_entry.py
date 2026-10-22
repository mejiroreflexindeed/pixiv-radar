"""W01 启动入口测试：就绪检查、preflight、两个 .cmd 入口（全部离线）。"""

from __future__ import annotations

import importlib.util
import socket
import sys
import types
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
REVIEW_DIR = PROJECT_ROOT / "docs" / "reviews" / "login-ready-20260909-01"
FIXTURE_ROOT = REVIEW_DIR / "startup-entry-fixtures"


@pytest.fixture
def workspace_tmp():
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = FIXTURE_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    workdir.mkdir()
    try:
        yield workdir
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"startup_{name}", SCRIPTS_DIR / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_uvicorn(monkeypatch):
    fake = types.ModuleType("uvicorn")
    fake.Config = lambda *a, **k: None
    fake.run = lambda *a, **k: None

    class _Server:
        def __init__(self, *a, **k):
            self.started = False
            self.should_exit = False

        def run(self):
            self.started = True

    fake.Server = _Server
    monkeypatch.setitem(sys.modules, "uvicorn", fake)


def _make_dist(base: Path) -> Path:
    dist = base / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_readiness_ready_with_real_workspace():
    check = _load("check_readiness")
    report = check.check_readiness(port=18081)
    assert report["status"] in ("ready", "missing_browser"), report
    assert report["static_index"] is True
    assert report["static_assets"] is True
    assert "remedies" in report


def test_readiness_missing_build(monkeypatch, workspace_tmp):
    check = _load("check_readiness")
    report = check.check_readiness(
        static_dir=workspace_tmp / "missing-dist", port=18082
    )
    assert report["status"] == "missing_build"
    assert any("npm run build" in r for r in report["remedies"])


@pytest.mark.parametrize("blocked", [False, True])
def test_readiness_new_clone_data_directory(workspace_tmp, blocked):
    check = _load("check_readiness")
    data = workspace_tmp / "data"
    if blocked:
        data.write_text("existing file", encoding="utf-8")
    report = check.check_readiness(
        db_path=data / "state" / "radar.db",
        static_dir=_make_dist(workspace_tmp),
        port=0,
        require_chromium=False,
    )
    assert report["status"] == ("invalid_db_path" if blocked else "ready")
    assert not (data / "state").exists()


def test_readiness_missing_browser(monkeypatch, workspace_tmp):
    check = _load("check_readiness")
    monkeypatch.setattr(check, "RUNTIME_BROWSERS", workspace_tmp / "no-browser")
    report = check.check_readiness(
        static_dir=PROJECT_ROOT / "frontend" / "dist", port=18083
    )
    assert report["status"] == "missing_browser"
    assert any("playwright install chromium" in r for r in report["remedies"])


def test_readiness_missing_dependency(monkeypatch):
    check = _load("check_readiness")
    monkeypatch.setattr(
        check, "REQUIRED_PACKAGES", ("fastapi", "definitely-not-installed-xyz")
    )
    report = check.check_readiness(port=18084)
    assert report["status"] == "missing_dependency"


def test_readiness_port_in_use(workspace_tmp):
    check = _load("check_readiness")
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        report = check.check_readiness(port=port)
        assert report["status"] == "port_in_use"
        assert any("已被占用" in r for r in report["remedies"])
    finally:
        holder.close()


def test_preview_entry_path_with_spaces_and_chinese(workspace_tmp, monkeypatch):
    _fake_uvicorn(monkeypatch)
    preview = _load("run_local_preview")
    work = workspace_tmp / "路径 含空格"
    work.mkdir()
    dist = _make_dist(work)
    db = work / "state" / "preview.db"
    code = preview.main(
        [
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--port", "18085",
            "--no-browser",
        ]
    )
    assert code == 0
    assert not db.exists()  # 无 --seed 时零写入


def test_preview_entry_missing_assets_fails_before_seed(workspace_tmp, monkeypatch):
    _fake_uvicorn(monkeypatch)
    preview = _load("run_local_preview")
    dist = workspace_tmp / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("x", encoding="utf-8")
    db = workspace_tmp / "preview" / "radar.db"
    code = preview.main(
        [
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--port", "18086",
            "--seed",
            "--no-browser",
        ]
    )
    assert code == 2
    assert not db.exists()


def test_preview_entry_port_occupied(workspace_tmp, monkeypatch):
    _fake_uvicorn(monkeypatch)
    preview = _load("run_local_preview")
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "preview" / "radar.db"
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        code = preview.main(
            [
                "--db-path", str(db),
                "--static-dir", str(dist),
                "--port", str(port),
                "--no-browser",
            ]
        )
        assert code == 2
        assert not db.exists()
    finally:
        holder.close()


def test_preview_entry_from_other_cwd(workspace_tmp, monkeypatch):
    _fake_uvicorn(monkeypatch)
    preview = _load("run_local_preview")
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "preview" / "radar.db"
    other = workspace_tmp / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    code = preview.main(
        [
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--port", "18087",
            "--no-browser",
        ]
    )
    assert code == 0


def test_real_app_requires_explicit_flag(workspace_tmp, monkeypatch):
    _fake_uvicorn(monkeypatch)
    real = _load("run_real_app")
    code = real.main(
        ["--db-path", str(workspace_tmp / "x.db"), "--no-browser"]
    )
    assert code == 2


def test_real_app_rejects_demo_db(workspace_tmp, monkeypatch):
    _fake_uvicorn(monkeypatch)
    real = _load("run_real_app")
    dist = _make_dist(workspace_tmp)
    code = real.main(
        [
            "--enable-real",
            "--db-path", str(workspace_tmp / "radar_demo.db"),
            "--static-dir", str(dist),
            "--no-browser",
        ]
    )
    assert code == 2


def test_real_app_rejects_missing_build(workspace_tmp, monkeypatch):
    _fake_uvicorn(monkeypatch)
    real = _load("run_real_app")
    code = real.main(
        [
            "--enable-real",
            "--db-path", str(workspace_tmp / "state" / "radar.db"),
            "--static-dir", str(workspace_tmp / "missing-dist"),
            "--no-browser",
        ]
    )
    assert code == 2
    assert not (workspace_tmp / "state" / "radar.db").exists()


def test_cmd_launchers_exist_and_point_to_entries():
    # R17：两个启动入口统一为 UTF-8 无 BOM + CRLF（与 chcp 65001 一致）
    real_cmd = (PROJECT_ROOT / "启动 Pixiv 小说雷达.cmd").read_text(encoding="utf-8")
    demo_cmd = (PROJECT_ROOT / "查看离线演示.cmd").read_text(encoding="utf-8")
    assert "run_real_app.py" in real_cmd
    assert "--enable-real" in real_cmd
    assert "run_local_preview.py" in demo_cmd
    assert "local-preview" in demo_cmd
    assert "--seed" in demo_cmd  # 仅首次（不存在时）使用
    for name in ("启动 Pixiv 小说雷达.cmd", "查看离线演示.cmd", "备份我的数据.cmd"):
        raw = (PROJECT_ROOT / name).read_bytes()
        raw.decode("utf-8")  # 严格解码：编码与 chcp 65001 一致
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert raw.count(b"\r\n") > 0 and b"\n" not in raw.replace(b"\r\n", b"")
