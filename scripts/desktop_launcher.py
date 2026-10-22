"""Pixiv 小说雷达桌面启动器（EXE 打包入口；也用于开发调试）。

特性：
- 无控制台窗口的图形面板：离线演示 / 真实模式、端口、可选定时扫描、
  打开页面、停止并退出；日志写入 data/logs/。
- ``--worker`` 自调度：冻结后 gppt 登录 worker 用同一个 exe 重新拉起
  （``Pixiv雷达.exe --worker --mode gppt``），在负责 GUI 的代码之前处理。
- ``--selftest <秒>``：无界面自检（健康检查 + 首页 200），写
  data/logs/selftest.json 后自动退出，供打包验证使用。
- 所有路径以 exe/项目目录为基准；不会读写开发机上的正式库。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

APP_TITLE = "Pixiv 小说雷达"


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


BASE_DIR = _base_dir()
BACKEND_DIR = BASE_DIR / "backend"
SCRIPTS_DIR = BASE_DIR / "scripts"
DATA_DIR = Path(
    os.environ.get("RADAR_LAUNCHER_DATA_DIR", str(BASE_DIR / "data"))
)
RUNTIME_DIR = Path(
    os.environ.get("RADAR_LAUNCHER_RUNTIME_DIR", str(BASE_DIR / ".runtime"))
)
LOG_DIR = DATA_DIR / "logs"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _patch_runtime_paths() -> None:
    """把 gppt/Playwright 的工作区路径固定到应用目录（冻结时在 exe 旁）。"""
    import app.auth.runtime_paths as runtime_paths

    runtime_paths.WORKSPACE_ROOT = BASE_DIR
    runtime_paths.RUNTIME_DIR = RUNTIME_DIR
    runtime_paths.PLAYWRIGHT_BROWSERS_PATH = RUNTIME_DIR / "playwright"
    runtime_paths.GPPT_CONFIG_DIR = RUNTIME_DIR / "gppt-empty"


_patch_runtime_paths()


def _ensure_stdio() -> None:
    """窗口模式下 sys.stdout/stderr 可能为 None；worker 与主进程都需要可写流。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOG_DIR / "worker.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


_ensure_stdio()


def _redirect_output() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "launcher.log"
    if getattr(sys, "frozen", False):
        handle = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle
    return log_path


def _run_worker_if_requested() -> None:
    """worker 自调度必须在任何 GUI/应用导入之前完成。"""
    if len(sys.argv) < 2 or sys.argv[1] != "--worker":
        return
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[2:]]
    from app.auth import worker_entry

    raise SystemExit(worker_entry.main())


_run_worker_if_requested()


# ---------------------------------------------------------------------------
# 服务启动/停止
# ---------------------------------------------------------------------------


def _free_port_ok(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1..65535 内")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise ValueError(f"端口 127.0.0.1:{port} 已被占用，请先关闭其他实例或改端口") from exc


def _static_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "frontend" / "dist"
    return BASE_DIR / "frontend" / "dist"


def _worker_command() -> list[str] | None:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", "--mode", "gppt"]
    return None


def _build_demo_app(static_dir: Path):
    os.environ["RADAR_DISCOVERY_MODE"] = "demo"
    os.environ["RADAR_AUTH_ALLOW_REAL"] = "0"
    os.environ["RADAR_DISCOVERY_INTERVAL_MINUTES"] = "0"
    db_path = DATA_DIR / "demo" / "radar.db"
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        import seed_e_demo

        saved_argv = sys.argv
        try:
            sys.argv = ["seed_e_demo", str(db_path)]
            seed_e_demo.main()
        finally:
            sys.argv = saved_argv
    os.environ["RADAR_DB_PATH"] = str(db_path)

    from app.auth.service import (
        AuthCapability,
        AuthService,
        UnavailableAuthorizationProvider,
    )
    from app.core.config import get_settings
    from app.core.credentials import InMemoryCredentialStore
    from app.main import create_app

    get_settings.cache_clear()
    auth_service = AuthService(
        InMemoryCredentialStore(),
        UnavailableAuthorizationProvider(),
        capability=lambda: AuthCapability(False, False, "demo_mode"),
    )
    return create_app(auth_service=auth_service, static_dir=static_dir), db_path


def _build_real_app(static_dir: Path, port: int, schedule_minutes: int, tag: str):
    db_path = DATA_DIR / "state" / "radar.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["RADAR_DB_PATH"] = str(db_path)
    os.environ["RADAR_DISCOVERY_MODE"] = "real"
    os.environ["RADAR_AUTH_ALLOW_REAL"] = "1"
    os.environ["RADAR_DISCOVERY_INTERVAL_MINUTES"] = str(schedule_minutes)
    os.environ["RADAR_DISCOVERY_TAG"] = tag

    from app.auth.providers.supervised import GpptSupervisedAuthorizationProvider
    from app.auth.service import AuthService
    from app.auth.worker import real_login_gate_enabled
    from app.core.config import get_settings
    from app.core.keyring_store import LazyKeyringCredentialStore
    from app.main import _auth_capability, _default_validator, create_app
    from app.pixiv.request_gate import RequestGate

    get_settings.cache_clear()

    if getattr(sys, "frozen", False):
        # 冻结环境里 keyring 的入口点发现可能不可用；显式使用 Windows 凭据管理器后端。
        import keyring
        from keyring.backends.Windows import WinVaultKeyring

        keyring.set_keyring(WinVaultKeyring())

    gate = RequestGate()
    store = LazyKeyringCredentialStore()
    provider = GpptSupervisedAuthorizationProvider(worker_command=_worker_command())
    auth_service = AuthService(
        store,
        provider,
        allow_real=real_login_gate_enabled,
        validator=_default_validator(gate),
        capability=_auth_capability,
        gate=gate,
    )
    return (
        create_app(
            auth_service=auth_service,
            static_dir=static_dir,
            credential_store=store,
            request_gate=gate,
        ),
        db_path,
    )


class RadarServer:
    """在后台线程运行 uvicorn；供 GUI/自检共用。"""

    def __init__(self) -> None:
        self._server = None
        self._thread: threading.Thread | None = None
        self.port = 8000

    def start(self, application, port: int, *, open_browser: bool) -> None:
        import uvicorn

        config = uvicorn.Config(
            application, host="127.0.0.1", port=port, log_level="info"
        )
        self._server = uvicorn.Server(config)
        self.port = port
        self._thread = threading.Thread(
            target=self._server.run, name="radar-uvicorn", daemon=True
        )
        self._thread.start()
        if open_browser:
            threading.Thread(
                target=self._open_browser_when_ready,
                args=(port,),
                daemon=True,
                name="radar-open-browser",
            ).start()

    def _open_browser_when_ready(self, port: int) -> None:
        import webbrowser

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._server is None or self._server.should_exit:
                return
            if self._server.started and self._accepts(port):
                try:
                    webbrowser.open(f"http://127.0.0.1:{port}/")
                except Exception:
                    return
                return
            time.sleep(0.1)

    @staticmethod
    def _accepts(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            return False

    def wait_health(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/api/health", timeout=2
                ) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def stop(self, timeout: float = 15.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None


def _start_mode(
    mode: str, port: int, schedule_minutes: int, tag: str, *, open_browser: bool
) -> tuple[RadarServer, Path]:
    _free_port_ok(port)
    static_dir = _static_dir()
    if not (static_dir / "index.html").is_file():
        raise ValueError(f"前端构建缺失：{static_dir}")
    if mode == "real":
        application, db_path = _build_real_app(static_dir, port, schedule_minutes, tag)
    else:
        application, db_path = _build_demo_app(static_dir)
    server = RadarServer()
    server.start(application, port, open_browser=open_browser)
    return server, db_path


# ---------------------------------------------------------------------------
# 无界面自检
# ---------------------------------------------------------------------------


def _run_selftest(mode: str, port: int, seconds: float, schedule: int, tag: str) -> int:
    report: dict = {"mode": mode, "port": port, "ok": False}
    server: RadarServer | None = None
    try:
        server, db_path = _start_mode(
            mode, port, schedule, tag, open_browser=False
        )
        report["db_path"] = str(db_path)
        ok = server.wait_health(40)
        report["health"] = ok
        if ok:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=5
            ) as resp:
                report["index_status"] = resp.status
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/discovery/status", timeout=5
            ) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                report["discovery_source"] = payload.get("source")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/auth/status", timeout=5
            ) as resp:
                auth = json.loads(resp.read().decode("utf-8"))
                report["auth"] = {
                    key: auth.get(key)
                    for key in (
                        "state",
                        "login_available",
                        "provider_mode",
                        "validate_available",
                        "unavailable_reason",
                    )
                }
        report["db_exists"] = db_path.exists()
        report["ok"] = bool(ok and report.get("index_status") == 200)
        time.sleep(max(0.0, seconds))
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        if server is not None:
            server.stop()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "selftest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------


def _run_gui(default_port: int, default_mode: str) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    state = {"server": None, "busy": False}

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("560x360")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)

    ttk.Label(
        frame,
        text="本工具只在本机运行（127.0.0.1），只做低频只读搜索；\n"
        "登录在 Pixiv 官方页面由你本人完成，凭据仅保存在 Windows 凭据管理器。",
        foreground="#555",
        justify="left",
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

    mode_var = tk.StringVar(value="demo" if default_mode != "real" else "real")
    ttk.Label(frame, text="模式").grid(row=1, column=0, sticky="w")
    ttk.Radiobutton(
        frame, text="离线演示（无需登录）", value="demo", variable=mode_var
    ).grid(row=1, column=1, sticky="w")
    ttk.Radiobutton(
        frame, text="真实模式（官方页面自行登录）", value="real", variable=mode_var
    ).grid(row=1, column=2, sticky="w")

    port_var = tk.StringVar(value=str(default_port))
    ttk.Label(frame, text="端口").grid(row=2, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(frame, textvariable=port_var, width=8).grid(
        row=2, column=1, sticky="w", pady=(6, 0)
    )

    schedule_var = tk.BooleanVar(value=False)
    minutes_var = tk.StringVar(value="30")
    tag_var = tk.StringVar(value="ウマ娘")
    ttk.Checkbutton(
        frame, text="定时自动扫描（真实模式）", variable=schedule_var
    ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
    schedule_row = ttk.Frame(frame)
    schedule_row.grid(row=4, column=0, columnspan=3, sticky="w")
    ttk.Label(schedule_row, text="每").pack(side="left")
    ttk.Entry(schedule_row, textvariable=minutes_var, width=6).pack(side="left")
    ttk.Label(schedule_row, text="分钟，标签").pack(side="left")
    ttk.Entry(schedule_row, textvariable=tag_var, width=18).pack(side="left")
    ttk.Label(schedule_row, text="（15–1440；首次等待一个完整周期）").pack(side="left")

    status_var = tk.StringVar(value="状态：未启动")
    ttk.Label(frame, textvariable=status_var, foreground="#1d4ed8").grid(
        row=5, column=0, columnspan=3, sticky="w", pady=(12, 0)
    )

    buttons = ttk.Frame(frame)
    buttons.grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
    start_button = ttk.Button(buttons, text="启动")
    start_button.pack(side="left")
    open_button = ttk.Button(buttons, text="打开页面", state="disabled")
    open_button.pack(side="left", padx=8)
    stop_button = ttk.Button(buttons, text="停止并退出", state="disabled")
    stop_button.pack(side="left")

    def _set_status(text: str) -> None:
        status_var.set(text)

    def _open_page() -> None:
        server = state["server"]
        if server is not None:
            import webbrowser

            webbrowser.open(f"http://127.0.0.1:{server.port}/")

    def _stop(*, exit_after: bool) -> None:
        server = state["server"]
        if server is not None:
            _set_status("状态：正在停止…")
            server.stop()
            state["server"] = None
        if exit_after:
            root.destroy()
            return
        start_button.config(state="normal")
        open_button.config(state="disabled")
        stop_button.config(state="disabled")
        _set_status("状态：已停止")

    def _start() -> None:
        if state["server"] is not None:
            return
        try:
            port = int(port_var.get())
        except ValueError:
            messagebox.showerror(APP_TITLE, "端口必须是数字")
            return
        mode = mode_var.get()
        try:
            schedule = int(minutes_var.get()) if schedule_var.get() else 0
        except ValueError:
            messagebox.showerror(APP_TITLE, "定时分钟数必须是数字")
            return
        if schedule and not 15 <= schedule <= 1440:
            messagebox.showerror(APP_TITLE, "定时分钟数必须在 15–1440")
            return
        tag = tag_var.get().strip() or "ウマ娘"
        start_button.config(state="disabled")
        _set_status("状态：启动中…")
        root.update_idletasks()
        try:
            server, db_path = _start_mode(
                mode, port, schedule, tag, open_browser=True
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"启动失败：{exc}")
            start_button.config(state="normal")
            _set_status("状态：未启动")
            return
        state["server"] = server
        open_button.config(state="normal")
        stop_button.config(state="normal")
        _set_status(
            f"状态：{('真实模式' if mode == 'real' else '离线演示')}"
            f"运行中 http://127.0.0.1:{port}/  数据：{db_path}"
        )
        if schedule:
            _set_status(
                f"状态：真实模式运行中（每 {schedule} 分钟扫描 {tag}，"
                f"首次等待一个完整周期）http://127.0.0.1:{port}/"
            )

    start_button.config(command=_start)
    open_button.config(command=_open_page)
    stop_button.config(command=lambda: _stop(exit_after=True))
    root.protocol("WM_DELETE_WINDOW", lambda: _stop(exit_after=True))

    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    _redirect_output()
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--mode", choices=("demo", "real"), default="demo")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--schedule-minutes", type=int, default=0)
    parser.add_argument("--tag", default="ウマ娘")
    parser.add_argument("--selftest", type=float, default=None,
                        help="无界面自检并运行指定秒数后自动退出")
    args = parser.parse_args(argv)

    if args.selftest is not None:
        return _run_selftest(
            args.mode, args.port, args.selftest, args.schedule_minutes, args.tag
        )
    return _run_gui(args.port, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
