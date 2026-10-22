from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import sys
import types
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.service import AuthService, UnavailableAuthorizationProvider
from app.core.config import get_settings
from app.core.credentials import InMemoryCredentialStore
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REVIEW_DIR = PROJECT_ROOT / "docs" / "reviews" / "2026-09-02-fh0"
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


@pytest.fixture
def workspace_tmp():
    """预览脚本只接受工作区内路径：用工作区内隔离目录做 fixture 并清理。"""
    root = REVIEW_DIR / "preview-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    workdir = root / f"night-{uuid.uuid4().hex[:8]}"
    workdir.mkdir()
    try:
        yield workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _install_fake_uvicorn(monkeypatch):
    """记录 Config 调用并模拟 Server.run；不真正监听端口。"""
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.calls = []

    class _Config:
        def __init__(self, application, **kwargs):
            fake_uvicorn.calls.append((application, kwargs))

    class _Server:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False

        def run(self):
            self.started = True

    fake_uvicorn.Config = _Config
    fake_uvicorn.Server = _Server
    fake_uvicorn.run = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    return fake_uvicorn


def _load_preview_script(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "preview_probe", PROJECT_ROOT / "scripts" / "run_local_preview.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _install_fake_uvicorn(monkeypatch)
    return module


def _load_real_script(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "real_app_probe", PROJECT_ROOT / "scripts" / "run_real_app.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake_uvicorn = _install_fake_uvicorn(monkeypatch)
    return module, fake_uvicorn


def _make_dist(base: Path) -> Path:
    dist = base / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>preview</title>", encoding="utf-8"
    )
    (dist / "assets" / "app.js").write_text("console.log('preview')", encoding="utf-8")
    return dist


def test_static_preview_serves_only_built_frontend_and_uses_offline_auth(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "preview" / "radar.db"))
    get_settings.cache_clear()
    dist = _make_dist(tmp_path)
    app = create_app(
        auth_service=AuthService(
            InMemoryCredentialStore(), UnavailableAuthorizationProvider()
        ),
        static_dir=dist,
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert "preview" in client.get("/").text
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/auth/status").json()["provider_mode"] == "unavailable"
        assert client.get("/api/does-not-exist").status_code == 404
        assert client.get("/backend/app/main.py").status_code == 404
    get_settings.cache_clear()


def test_static_preview_requires_a_build_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "preview" / "radar.db"))
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="请先运行 npm run build"):
        create_app(static_dir=Path(tmp_path / "missing-dist"))
    get_settings.cache_clear()


def test_preview_invalid_build_fails_before_seed(workspace_tmp, monkeypatch):
    preview = _load_preview_script(monkeypatch)
    target = workspace_tmp / "preview" / "radar.db"
    code = preview.main(
        [
            "--db-path", str(target),
            "--static-dir", str(workspace_tmp / "missing-dist"),
            "--seed",
        ]
    )
    assert code == 2
    assert not target.exists(), "被拒绝的启动在 seed 前不得创建数据库"


def test_preview_missing_assets_fails_before_seed(workspace_tmp, monkeypatch):
    preview = _load_preview_script(monkeypatch)
    dist = workspace_tmp / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>x</title>", encoding="utf-8"
    )
    target = workspace_tmp / "preview" / "radar.db"
    code = preview.main(
        [
            "--db-path", str(target),
            "--static-dir", str(dist),
            "--seed",
        ]
    )
    assert code == 2
    assert not target.exists()


def test_preview_does_not_reseed_existing_db(workspace_tmp, monkeypatch):
    preview = _load_preview_script(monkeypatch)
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "preview" / "radar.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"existing-user-preview")
    code = preview.main(
        [
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--seed",
        ]
    )
    assert code == 2
    assert db.read_bytes() == b"existing-user-preview"  # 未重 seed、未覆盖


def test_preview_restart_without_seed_preserves_db(workspace_tmp, monkeypatch):
    preview = _load_preview_script(monkeypatch)
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "preview" / "radar.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"existing-user-preview")
    code = preview.main(
        [
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--no-browser",
        ]
    )
    assert code == 0
    assert db.read_bytes() == b"existing-user-preview"


def test_preview_occupied_port_exits_without_killing(workspace_tmp, monkeypatch):
    preview = _load_preview_script(monkeypatch)
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
            ]
        )
        assert code == 2
        assert not db.exists()  # 端口占用在 seed 之前退出
    finally:
        holder.close()


def test_preview_build_touches_no_real_keyring(workspace_tmp, monkeypatch):
    import app.core.keyring_store as keyring_store

    def explode(*args, **kwargs):
        raise AssertionError("real keyring must not be touched in preview")

    monkeypatch.setattr(keyring_store, "LazyKeyringCredentialStore", explode)
    monkeypatch.setattr(keyring_store, "KeyringCredentialStore", explode)
    preview = _load_preview_script(monkeypatch)
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "preview" / "radar.db"
    code = preview.main(
        [
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--no-browser",
        ]
    )
    assert code == 0
    assert not db.exists()  # 无 --seed 时零写入


def test_preview_rejects_formal_database_path(workspace_tmp, monkeypatch):
    """默认 Demo 路径不得回退到正式库：显式指向 data/state/radar.db 必须拒绝。"""
    preview = _load_preview_script(monkeypatch)
    dist = _make_dist(workspace_tmp)
    formal = PROJECT_ROOT / "data" / "state" / "radar.db"
    code = preview.main(
        [
            "--db-path", str(formal),
            "--static-dir", str(dist),
            "--seed",
        ]
    )
    assert code == 2
    # 正式库未被创建/修改（这里只断言脚本在 seed 前拒绝，不触碰该文件）
    assert not (workspace_tmp / "preview" / "radar.db").exists()


def test_preview_seed_then_restart_keeps_db(workspace_tmp, monkeypatch):
    preview = _load_preview_script(monkeypatch)
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "preview" / "radar.db"
    code = preview.main(
        [
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--seed",
            "--no-browser",
        ]
    )
    assert code == 0
    assert db.exists()
    size_after_seed = db.stat().st_size
    code = preview.main(
        [
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--no-browser",
        ]
    )
    assert code == 0
    assert db.stat().st_size == size_after_seed


def test_browser_opens_only_after_port_actually_accepts(monkeypatch):
    """回归：只标记 started 但未监听时绝不能打开浏览器（避免“拒绝连接”标签页）。"""
    import launch_support

    opened: list[str] = []
    monkeypatch.setattr(
        launch_support.webbrowser, "open", lambda url: opened.append(url) or True
    )

    class FakeServer:
        started = True
        should_exit = False

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        unbound_port = probe.getsockname()[1]
    launch_support.open_browser_when_ready(FakeServer(), unbound_port, timeout=0.4)
    assert opened == []

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    live_port = listener.getsockname()[1]
    try:
        launch_support.open_browser_when_ready(
            FakeServer(), live_port, timeout=2.0
        )
    finally:
        listener.close()
    assert opened == [f"http://127.0.0.1:{live_port}/"]


def test_real_launcher_requires_explicit_gate_before_any_write(
    workspace_tmp, monkeypatch
):
    real, _ = _load_real_script(monkeypatch)
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "state" / "radar.db"
    assert real.main(["--db-path", str(db), "--static-dir", str(dist)]) == 2
    assert not db.exists()


def test_real_launcher_sets_mode_but_does_not_read_keyring_on_start(
    workspace_tmp, monkeypatch
):
    import app.core.keyring_store as keyring_store

    class ExplodingStore:
        def __init__(self, *args, **kwargs):
            raise AssertionError("launcher startup must not initialize real keyring")

    monkeypatch.setattr(keyring_store, "KeyringCredentialStore", ExplodingStore)
    real, fake_uvicorn = _load_real_script(monkeypatch)
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "state" / "radar.db"
    code = real.main(
        [
            "--enable-real",
            "--db-path", str(db),
            "--static-dir", str(dist),
            "--port", "8126",
            "--no-browser",
            "--schedule-minutes", "15",
            "--tag", " メジロアルダン ",
        ]
    )
    assert code == 0
    assert not db.exists()  # fake uvicorn 未进入 lifespan；启动准备本身零写入
    assert fake_uvicorn.calls
    assert fake_uvicorn.calls[0][1]["host"] == "127.0.0.1"
    assert fake_uvicorn.calls[0][1]["port"] == 8126
    assert os.environ["RADAR_DISCOVERY_MODE"] == "real"
    assert os.environ["RADAR_AUTH_ALLOW_REAL"] == "1"
    assert os.environ["RADAR_DISCOVERY_INTERVAL_MINUTES"] == "15"
    assert os.environ["RADAR_DISCOVERY_TAG"] == "メジロアルダン"
    get_settings.cache_clear()


def test_real_launcher_rejects_unsafe_schedule_interval(workspace_tmp, monkeypatch):
    real, fake_uvicorn = _load_real_script(monkeypatch)
    dist = _make_dist(workspace_tmp)
    db = workspace_tmp / "state" / "radar.db"
    code = real.main([
        "--enable-real",
        "--db-path", str(db),
        "--static-dir", str(dist),
        "--schedule-minutes", "5",
    ])
    assert code == 2
    assert not fake_uvicorn.calls
    assert not db.exists()
