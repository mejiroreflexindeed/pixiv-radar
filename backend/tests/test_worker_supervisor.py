"""WP2 worker supervisor 测试：全部使用 fake worker，不联网、不登录、无真实凭据。

覆盖：延迟成功、重复完成、错误 state、假 token 单次传递、取消（无孤儿进程）、
超时、启动失败、子进程崩溃、父端关闭、单 worker 互斥、selfcheck 兼容层证据。

竞态说明：venv python 存在再派生链（shim→base），满载运行时后代生成较慢；
测试用 _wait_children 确定性等待后代出现，且 autouse fixture 在断言失败时
也会按已记录 PID 精准清理，避免残留。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from app.auth.service import AuthOperationError
from app.auth.worker import (
    WorkerResult,
    WorkerSupervisor,
    is_process_alive,
    kill_process_tree,
)

FAKE_COMMAND = [sys.executable, "-m", "app.auth.worker_entry", "--mode", "fake"]
SELFCHECK_COMMAND = [sys.executable, "-m", "app.auth.worker_entry", "--mode", "selfcheck"]
SENTINEL = "fake-refresh-sentinel-never-return"

REQUEST = {
    "session_id": "test-session",
    "state": "test-state",
    "expires_at": "2026-08-31T01:00:00+00:00",
}

# 本轮测试启动的 worker PID：断言失败时也由 autouse fixture 精准清理
_STARTED_WORKERS: set[int] = set()


@pytest.fixture(autouse=True)
def _cleanup_started_workers():
    _STARTED_WORKERS.clear()
    yield
    for pid in list(_STARTED_WORKERS):
        kill_process_tree(pid)
    _STARTED_WORKERS.clear()


def test_kill_process_tree_falls_back_to_tree_kill_when_enumeration_fails(
    monkeypatch,
):
    """R14：枚举不可用（CIM 权限/超时）时，必须终止整棵自有进程树，
    不能只杀父 PID 后把子进程存活误报为清理成功。"""
    import app.auth.worker as worker_module

    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "print(p.pid,flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    child_pid = 0
    try:
        assert proc.stdout is not None
        child_pid = int(proc.stdout.readline().strip())
        monkeypatch.setattr(worker_module, "_collect_process_map", lambda: {})
        assert kill_process_tree(proc.pid, wait_dead_seconds=15)
        assert not is_process_alive(proc.pid)
        assert child_pid > 0
        assert not is_process_alive(child_pid), "后代进程不得残留"
    finally:
        if child_pid > 0 and is_process_alive(child_pid):
            worker_module._taskkill(child_pid, tree=True)
        if proc.poll() is None:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()


def _start(supervisor: WorkerSupervisor, request: dict) -> int:
    pid = supervisor.start(request)
    assert pid > 0
    _STARTED_WORKERS.add(pid)
    return pid


def make_request(**overrides):
    return {**REQUEST, **overrides}


def _child_pids(pid: int) -> list[int]:
    """递归收集目标 PID 的全部后代（含 venv 再派生链）。"""
    template = (
        "$all = Get-CimInstance Win32_Process;"
        " $tree = @{}; $frontier = @(@PID@);"
        " while ($frontier.Count -gt 0) {"
        "  $next = @();"
        "  foreach ($f in $frontier) {"
        "   foreach ($p in ($all | Where-Object { $_.ParentProcessId -eq $f })) {"
        "    if (-not $tree.ContainsKey($p.ProcessId)) {"
        "     $tree[$p.ProcessId] = $true; $next += $p.ProcessId } } }"
        "  $frontier = $next }"
        " $tree.Keys"
    )
    script = template.replace("@PID@", str(pid))
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return [int(x) for x in (result.stdout or "").split() if x.strip().isdigit()]


def _wait_children(pid: int, timeout: float = 20.0) -> list[int]:
    """确定性等待：venv 再派生链在满载下生成较慢，轮询直到后代出现。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        children = _child_pids(pid)
        if children:
            return children
        time.sleep(0.3)
    return _child_pids(pid)


def _wait_dead(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.2)
    return not is_process_alive(pid)


def test_delayed_success_single_token_pass():
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    pid = _start(
        supervisor,
        make_request(behavior="delayed_success", delay=0.3, token=SENTINEL),
    )
    result = supervisor.wait(timeout=10)
    assert result.status == "completed"
    assert result.refresh_token == SENTINEL
    assert result.callback_state == "test-state"
    assert _wait_dead(pid)


def test_duplicate_completion_second_ignored():
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    _start(supervisor, make_request(behavior="double_result", token=SENTINEL))
    result = supervisor.wait(timeout=10)
    assert result.status == "completed"
    assert result.refresh_token == SENTINEL  # 只接受第一条结果


def test_wrong_state_echo():
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    _start(supervisor, make_request(behavior="wrong_state", token=SENTINEL))
    result = supervisor.wait(timeout=10)
    assert result.status == "completed"
    assert result.callback_state == "wrong-state-echo"


def test_cancel_kills_tree_without_orphans():
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    pid = _start(supervisor, make_request(behavior="spawn_child_hold"))
    children = _wait_children(pid)
    assert len(children) >= 1  # fake worker 的占位子进程（可能含系统辅助进程）

    result = supervisor.cancel()
    assert result.status == "cancelled"
    assert _wait_dead(pid)
    for child in children:
        assert _wait_dead(child)  # 无孤儿：全部子进程一并终止


def test_cancel_kills_unregistered_child():
    """未登记进 _spawned_children 的假后代（模拟真实浏览器）在取消后也必须退出。"""
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    pid = _start(supervisor, make_request(behavior="spawn_unregistered_hold"))
    children = _wait_children(pid)
    assert len(children) >= 1  # 未登记子进程存在

    result = supervisor.cancel()
    assert result.status == "cancelled"
    assert _wait_dead(pid)
    for child in children:
        assert _wait_dead(child)  # 树清理覆盖未登记后代


def test_timeout_kills_unregistered_child():
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    pid = _start(supervisor, make_request(behavior="timeout_unregistered"))
    children = _wait_children(pid)
    assert len(children) >= 1

    result = supervisor.wait(timeout=2)
    assert result.status == "timeout"
    assert result.error_code == "authorization_timeout"
    assert _wait_dead(pid)
    for child in children:
        assert _wait_dead(child)


def test_no_handle_accumulation_across_runs():
    """正常成功反复运行后，请求/结果管道句柄不累计。"""

    def open_fd_count() -> int:
        count = 0
        for fd in range(3, 1024):
            try:
                os.fstat(fd)
                count += 1
            except OSError:
                pass
        return count

    before = open_fd_count()
    for _ in range(15):
        supervisor = WorkerSupervisor(FAKE_COMMAND)
        pid = _start(
            supervisor,
            make_request(behavior="delayed_success", delay=0.05, token=SENTINEL),
        )
        result = supervisor.wait(timeout=10)
        assert result.status == "completed"
        assert _wait_dead(pid)
    after = open_fd_count()
    assert after - before <= 4  # 允许少量残留；不得随轮次线性增长


def test_worker_result_repr_never_contains_token():
    result = WorkerResult(
        status="completed",
        refresh_token=SENTINEL,
        callback_state="secret-state",
    )
    assert SENTINEL not in repr(result)
    assert "secret-state" not in repr(result)


def test_missing_and_empty_callback_state_pass_through():
    for behavior, expected in (("state_missing", None), ("state_empty", "")):
        supervisor = WorkerSupervisor(FAKE_COMMAND)
        pid = _start(supervisor, make_request(behavior=behavior, token=SENTINEL))
        result = supervisor.wait(timeout=10)
        assert result.status == "completed"
        assert result.callback_state is expected  # 原样透传，不补造
        assert _wait_dead(pid)


def test_timeout_terminates_worker():
    supervisor = WorkerSupervisor(FAKE_COMMAND, timeout_seconds=60)
    pid = _start(supervisor, make_request(behavior="timeout"))
    result = supervisor.wait(timeout=2)
    assert result.status == "timeout"
    assert result.error_code == "authorization_timeout"
    assert _wait_dead(pid)


def test_start_failure_fixed_code():
    supervisor = WorkerSupervisor(
        ["C:\\nonexistent\\python_worker_xyz.exe", "--mode", "fake"]
    )
    pid = supervisor.start(make_request())
    assert pid == 0
    result = supervisor.wait()
    assert result.status == "start_failed"
    assert result.error_code == "worker_start_failed"


def test_child_crash_detected():
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    pid = _start(supervisor, make_request(behavior="crash"))
    result = supervisor.wait(timeout=10)
    assert result.status == "crashed"
    assert result.error_code == "worker_crashed"
    assert _wait_dead(pid)


def test_parent_close_triggers_cleanup():
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    pid = _start(supervisor, make_request(behavior="spawn_child_hold"))
    children = _wait_children(pid)
    assert len(children) >= 1

    supervisor.close_parent()  # 模拟父进程退出：关闭请求管道
    assert _wait_dead(pid)
    for child in children:
        assert _wait_dead(child)


def test_only_one_worker_at_a_time():
    supervisor = WorkerSupervisor(FAKE_COMMAND)
    pid = _start(supervisor, make_request(behavior="timeout"))
    with pytest.raises(AuthOperationError) as exc_info:
        supervisor.start(make_request(behavior="success"))
    assert exc_info.value.status_code == 409
    supervisor.cancel()
    assert _wait_dead(pid)


def test_selfcheck_compat_layer_offline_proof():
    """离线证据：EX_OK 补丁 + gppt 可导入 + --no-sandbox 已移除 + install 已替换。"""
    supervisor = WorkerSupervisor(SELFCHECK_COMMAND)
    pid = _start(supervisor, make_request())
    result = supervisor.wait(timeout=60)
    assert result.status == "selfcheck_ok", result
    assert result.details is not None
    assert result.details["gppt_imported"] is True
    assert result.details["no_sandbox_removed"] is True
    assert result.details["install_replaced"] is True
    assert result.details["chromium_present"] is True
    assert _wait_dead(pid)
