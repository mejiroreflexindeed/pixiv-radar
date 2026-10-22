"""登录 worker 子进程入口（Login-1B）。

模式：
- fake：测试用假 worker（延迟成功/失败/取消/超时/崩溃/重复结果/错误 state/子进程）。
- selfcheck：仅验证隔离兼容层与 Chromium 存在性，不登录不联网。
- gppt：真实受监督浏览器登录路径——仅当环境变量 ``RADAR_AUTH_ALLOW_REAL=1``
  时可执行；OFFLINE 阶段该变量恒为 0，本路径不可达。

隔离兼容层（Codex 批准，只存在于本子进程，绝不修改 Python/site-packages/系统文件）：
1. 导入 gppt 前设置 ``os.EX_OK = 0``（install-playwright 的 Windows 兼容缺陷）。
2. 导入后从 ``gppt.browser.BROWSER_ARGS`` 移除 ``--no-sandbox``（不关闭 Chromium 沙箱）。
3. 替换 ``gppt.browser.install``：只检查 Chromium executable 是否真实存在于
   ``D:\\codex\\12\\.runtime\\playwright``；不存在则安全失败（固定错误码），禁止联网补装。

请求与结果只经匿名管道（fd 句柄由环境变量传入，非敏感值）；token 不经过
stdout/stderr/文件/命令行。父进程关闭请求管道（EOF）时，worker 清理自己
spawn 的子进程后退出。
"""

from __future__ import annotations

import argparse
import json
import msvcrt
import os
import subprocess
import sys
import threading
import time

from .runtime_paths import GPPT_CONFIG_DIR, PLAYWRIGHT_BROWSERS_PATH
from .worker import kill_process_tree

_ALLOW_REAL = "RADAR_AUTH_ALLOW_REAL"
_REQUEST_PIPE = "RADAR_AUTH_IPC_REQ_HANDLE"
_RESULT_PIPE = "RADAR_AUTH_IPC_RES_HANDLE"

_spawned_children: list[int] = []


# ---- 管道 I/O ----


def _open_pipe_fds() -> tuple[int, int]:
    req_handle = int(os.environ[_REQUEST_PIPE])
    res_handle = int(os.environ[_RESULT_PIPE])
    req_fd = msvcrt.open_osfhandle(req_handle, os.O_RDONLY)
    res_fd = msvcrt.open_osfhandle(res_handle, os.O_WRONLY)
    return req_fd, res_fd


def _read_line(fd: int) -> str:
    buf = b""
    while b"\n" not in buf:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", errors="replace")


def _write_result(res_fd: int, payload: dict) -> None:
    os.write(res_fd, (json.dumps(payload) + "\n").encode("utf-8"))


# ---- 子进程清理（只清理自己 spawn 的 PID）----


def _spawn_dummy_child() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _spawned_children.append(proc.pid)
    return proc.pid


def _spawn_unregistered_child() -> int:
    """spawn 一个不加入 _spawned_children 的假后代（模拟真实浏览器未登记）。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return proc.pid


def _cleanup_children() -> None:
    for pid in _spawned_children:
        kill_process_tree(pid)
    _spawned_children.clear()


# ---- 父进程退出监控（请求管道 EOF → 清理并退出）----


def _monitor_parent(req_fd: int) -> None:
    while True:
        try:
            chunk = os.read(req_fd, 4096)
        except OSError:
            chunk = b""
        if chunk == b"":
            _cleanup_children()
            os._exit(0)


# ---- 隔离兼容层（仅 worker 子进程内生效）----


def _patch_gppt() -> None:
    # 1) Windows 兼容：install-playwright 的 `from os import EX_OK` 只在 POSIX 存在
    if not hasattr(os, "EX_OK"):
        os.EX_OK = 0  # type: ignore[attr-defined]
    # 2) 导入 gppt（此时 EX_OK 已可用）
    import gppt.browser as gbrowser  # noqa: PLC0415

    # 3) 移除 --no-sandbox：登录浏览器不得禁用 Chromium 沙箱
    gbrowser.BROWSER_ARGS = [
        arg for arg in gbrowser.BROWSER_ARGS if arg != "--no-sandbox"
    ]

    # 4) 替换 install：禁止自动下载/安装/更新浏览器，只做存在性检查
    def _check_browser_installed(*args, **kwargs) -> bool:
        del args, kwargs
        return chromium_executable() is not None

    gbrowser.install = _check_browser_installed  # type: ignore[assignment]


def chromium_executable() -> str | None:
    """Playwright Chromium executable 是否真实存在于工作区 .runtime。"""
    base = PLAYWRIGHT_BROWSERS_PATH
    if not base.is_dir():
        return None
    for revision in sorted(base.glob("chromium-*"), reverse=True):
        candidate = revision / "chrome-win64" / "chrome.exe"
        if candidate.is_file():
            return str(candidate)
    return None


# ---- 模式实现 ----


def _run_fake(request: dict, res_fd: int) -> None:
    behavior = str(request.get("behavior", "success"))
    delay = float(request.get("delay", 0) or 0)
    token = str(request.get("token", "fake-token-sentinel"))
    state = str(request.get("state", ""))

    def complete() -> None:
        _write_result(
            res_fd,
            {"status": "completed", "error_code": None,
             "refresh_token": token, "state": state},
        )

    if behavior == "crash":
        os._exit(1)
    if behavior == "state_missing":
        # 完成但不回传 state 字段（缺失）
        _write_result(
            res_fd,
            {"status": "completed", "error_code": None, "refresh_token": token},
        )
    elif behavior == "state_empty":
        # 完成但 state 为空字符串
        _write_result(
            res_fd,
            {"status": "completed", "error_code": None,
             "refresh_token": token, "state": ""},
        )
    elif behavior in ("delayed_success",):
        time.sleep(delay)
        complete()
    elif behavior == "double_result":
        complete()
        time.sleep(0.2)
        _write_result(
            res_fd,
            {"status": "completed", "error_code": None,
             "refresh_token": "second-token-must-be-ignored", "state": state},
        )
    elif behavior == "wrong_state":
        _write_result(
            res_fd,
            {"status": "completed", "error_code": None,
             "refresh_token": token, "state": "wrong-state-echo"},
        )
    elif behavior == "fail_browser_closed":
        _write_result(res_fd, {"status": "failed", "error_code": "browser_closed"})
    elif behavior == "fail_runtime":
        _write_result(
            res_fd, {"status": "failed", "error_code": "browser_runtime_unavailable"}
        )
    elif behavior == "timeout":
        while True:
            time.sleep(3600)
    elif behavior == "spawn_child_hold":
        _spawn_dummy_child()
        while True:
            time.sleep(3600)
    elif behavior == "spawn_unregistered_hold":
        # 假后代不登记（模拟真实 Playwright/Chromium 子进程）
        _spawn_unregistered_child()
        while True:
            time.sleep(3600)
    elif behavior == "timeout_unregistered":
        _spawn_unregistered_child()
        while True:
            time.sleep(3600)
    elif behavior == "spawn_child_success":
        _spawn_dummy_child()
        time.sleep(delay)
        complete()
        # 父进程取消/父端关闭由监控线程负责清理
        while True:
            time.sleep(3600)
    else:  # success
        complete()


def _run_selfcheck(res_fd: int) -> None:
    _patch_gppt()
    import gppt  # noqa: PLC0415  # 验证兼容层后 gppt 可导入
    import gppt.browser as gbrowser  # noqa: PLC0415

    exe = chromium_executable()
    details = {
        "gppt_imported": True,
        "no_sandbox_removed": "--no-sandbox" not in gbrowser.BROWSER_ARGS,
        "install_replaced": gbrowser.install.__name__ == "_check_browser_installed",
        "chromium_present": exe is not None,
    }
    if exe is None or not details["no_sandbox_removed"] or not details["install_replaced"]:
        _write_result(
            res_fd,
            {"status": "failed", "error_code": "browser_runtime_unavailable",
             "details": details},
        )
        return
    _write_result(res_fd, {"status": "selfcheck_ok", "error_code": None, "details": details})


def _run_gppt(request: dict, res_fd: int) -> None:
    """真实受监督浏览器登录（仅 RADAR_AUTH_ALLOW_REAL=1 可达；OFFLINE 不执行）。"""
    if os.environ.get(_ALLOW_REAL) != "1":
        _write_result(
            res_fd, {"status": "failed", "error_code": "real_login_not_approved"}
        )
        return
    _patch_gppt()
    if chromium_executable() is None:
        _write_result(
            res_fd, {"status": "failed", "error_code": "browser_runtime_unavailable"}
        )
        return
    try:
        from gppt import login as gppt_login  # noqa: PLC0415
        from gppt.token import Token  # noqa: PLC0415

        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PLAYWRIGHT_BROWSERS_PATH)
        os.environ["GPPT_CONFIG_DIR"] = str(GPPT_CONFIG_DIR)
        token_obj = gppt_login(
            username="", password="", totp_secret="", headless=False
        )
        assert isinstance(token_obj, Token)
        _write_result(
            res_fd,
            {"status": "completed", "error_code": None,
             "refresh_token": token_obj.refresh_token, "state": request.get("state")},
        )
    except Exception:
        # 只映射固定错误码，绝不回传上游异常正文
        _write_result(res_fd, {"status": "failed", "error_code": "authorization_failed"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fake", "selfcheck", "gppt"), required=True)
    args = parser.parse_args()

    req_fd, res_fd = _open_pipe_fds()
    line = _read_line(req_fd)
    request: dict = {}
    if line:
        try:
            request = json.loads(line)
        except ValueError:
            request = {}

    threading.Thread(target=_monitor_parent, args=(req_fd,), daemon=True).start()

    if args.mode == "fake":
        _run_fake(request, res_fd)
    elif args.mode == "selfcheck":
        _run_selfcheck(res_fd)
    else:
        _run_gppt(request, res_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
