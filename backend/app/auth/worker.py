"""登录 worker supervisor：可取消、可超时、只终止自己启动并核验过的进程树。

安全边界（见 docs/DEEPSEEK_LOGIN_1B_TASK.md）：
- 每次授权使用独立子进程；请求（session/state）与结果（refresh token）只经
  匿名管道（进程内存 IPC）传递，不经过 stdout/stderr/shell/文件/命令行/环境变量。
- 真实登录门禁（RADAR_AUTH_ALLOW_REAL）只有一个来源，默认关闭；worker 收到的
  值与 provider 判断可用性时一致。
- 取消/超时：先对已核验 PID 执行 `taskkill /T /F`（进程树，含未登记后代），
  再关闭请求管道；不按进程名盲杀，不影响用户其他进程。
- 正常完成后关闭请求/结果管道句柄，避免句柄累计。
- 结果状态为固定枚举；错误只映射固定错误码，绝不回传上游异常正文。
- refresh_token / callback_state 不进入 repr。
"""

from __future__ import annotations

import json
import msvcrt
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .service import AuthOperationError

_REQUEST_PIPE = "RADAR_AUTH_IPC_REQ_HANDLE"
_RESULT_PIPE = "RADAR_AUTH_IPC_RES_HANDLE"
REAL_LOGIN_GATE = "RADAR_AUTH_ALLOW_REAL"


def real_login_gate_enabled() -> bool:
    """真实登录门禁的唯一来源：环境变量显式等于 "1" 才算开启。"""
    return os.environ.get(REAL_LOGIN_GATE) == "1"


@dataclass(frozen=True)
class WorkerResult:
    status: str  # completed / failed / cancelled / timeout / crashed / start_failed / selfcheck_ok
    error_code: str | None = None
    refresh_token: str | None = field(default=None, repr=False)
    callback_state: str | None = field(default=None, repr=False)
    details: dict | None = None


def _make_inheritable(fd: int) -> None:
    import ctypes

    handle = msvcrt.get_osfhandle(fd)
    ctypes.windll.kernel32.SetHandleInformation(
        handle, 0x1, 0x1  # HANDLE_FLAG_INHERIT
    )


def _decode_command_output(raw: bytes | None) -> str:
    """Windows 命令输出显式解码；任何字节都不得抛错或产生 reader 线程异常。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def is_process_alive(pid: int) -> bool:
    """按 PID 精确查询进程是否存活（tasklist 过滤，不涉及进程名）。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        # 查询本身不可用时保守视为仍存活，避免误报“已清理”。
        return True
    if result.returncode != 0:
        return True
    text = _decode_command_output(result.stdout)
    # CSV 字段带引号，按精确字段匹配，避免 PID 数字被其他数字包含。
    return f'"{pid}"' in text or f",{pid}," in text


def _collect_process_map() -> dict[int, int]:
    """全量 PID→PPID 快照（只查询，不杀进程）；查询失败返回空表。"""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Process | ForEach-Object {"
                " \"$($_.ProcessId);$($_.ParentProcessId)\" })",
            ],
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if result.returncode != 0:
        return {}
    mapping: dict[int, int] = {}
    for line in _decode_command_output(result.stdout).splitlines():
        parts = line.strip().split(";")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            mapping[int(parts[0])] = int(parts[1])
    return mapping


def _taskkill(pid: int, *, tree: bool = False) -> None:
    """精确 PID 终止单个进程（可选整树）；失败/超时只放弃这一步，不向上抛错。"""
    command = ["taskkill", "/PID", str(pid)]
    if tree:
        command.append("/T")
    command.append("/F")
    try:
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20 if tree else 15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def kill_process_tree(pid: int, *, wait_dead_seconds: float = 10.0) -> bool:
    """只终止已核验 PID 的进程树，不按进程名盲杀。

    采用迭代快照：沿 PID→PPID 链逐 PID 执行 taskkill /F（精确 PID，不用 /T）。
    被终止进程若在竞态中已派生迟到后代（如 venv python 再派生解释器），其
    PPID 指向已终止集合，下一轮快照仍可捕获，直到无新增目标或轮次上限。
    枚举不可用（CIM 权限/超时）时兜底：对已核验的自有根 PID 使用系统树终止
    （taskkill /T），保证后代被清理而不是只杀父 PID 后误报成功。
    返回进程是否在有限等待内确认不再存活。
    """
    terminated: set[int] = set()
    enumeration_failures = 0
    for _round in range(6):
        mapping = _collect_process_map()
        if not mapping:
            # 单次超时可能是瞬时负载；重试一次后再走整树兜底。
            enumeration_failures += 1
            if enumeration_failures >= 2:
                if is_process_alive(pid):
                    _taskkill(pid, tree=True)
                terminated.add(pid)
                break
            time.sleep(0.2)
            continue
        targets = {
            proc
            for proc, parent in mapping.items()
            if proc == pid or parent == pid
            or proc in terminated or parent in terminated
        }
        targets -= terminated
        if not targets:
            break
        for target in sorted(targets):
            _taskkill(target)
        terminated |= targets
        time.sleep(0.15)
    deadline = time.monotonic() + wait_dead_seconds
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.2)
    return not is_process_alive(pid)


class WorkerSupervisor:
    """管理单个登录 worker 子进程。同一时间只允许一个 worker。"""

    def __init__(
        self,
        worker_command: list[str],
        *,
        timeout_seconds: float = 300.0,
        allow_real: bool | None = None,
    ) -> None:
        self._command = list(worker_command)
        self._timeout = timeout_seconds
        # 真实登录门禁：单一来源（env RADAR_AUTH_ALLOW_REAL），默认关闭；
        # 传入值必须与 provider 判断可用性时一致。
        self._allow_real = (
            real_login_gate_enabled() if allow_real is None else bool(allow_real)
        )
        # worker 子进程的工作目录固定为 backend/（保证 `python -m app.auth...` 可解析）
        self._cwd = str(Path(__file__).resolve().parents[2])
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._result: WorkerResult | None = None
        self._forced: WorkerResult | None = None
        self._reader: threading.Thread | None = None
        self._request_write_fd: int | None = None

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # ---- 启动 ----

    def start(self, request) -> int:
        """启动 worker 并发送请求（session/state 只走管道）。返回 PID；0=启动失败。"""
        with self._lock:
            if self.running:
                raise AuthOperationError(409, "authorization_in_progress")

            req_r, req_w = os.pipe()   # 父写 → 子读（请求）
            res_r, res_w = os.pipe()   # 子写 → 父读（结果）
            _make_inheritable(req_r)
            _make_inheritable(res_w)
            req_handle = int(msvcrt.get_osfhandle(req_r))
            res_handle = int(msvcrt.get_osfhandle(res_w))

            env = {
                **os.environ,
                _REQUEST_PIPE: str(req_handle),
                _RESULT_PIPE: str(res_handle),
                REAL_LOGIN_GATE: "1" if self._allow_real else "0",
            }
            try:
                proc = subprocess.Popen(
                    self._command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    cwd=self._cwd,
                    close_fds=False,  # Windows：允许继承已标记的管道句柄
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except OSError:
                for fd in (req_r, req_w, res_r, res_w):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._result = WorkerResult("start_failed", "worker_start_failed")
                return 0

            # 父进程关闭不再需要的副本；请求管道写端保留到取消/父进程退出
            os.close(req_r)
            os.close(res_w)

            self._proc = proc
            self._result = None
            self._forced = None
            self._request_write_fd = req_w

            os.write(req_w, (json.dumps(request) + "\n").encode("utf-8"))

            self._reader = threading.Thread(
                target=self._read_results, args=(res_r,), daemon=True
            )
            self._reader.start()
            return int(proc.pid)

    # ---- 结果读取（结果管道，token 只在此内存通道）----

    def _read_results(self, res_r: int) -> None:
        data = b""
        try:
            while True:
                chunk = os.read(res_r, 4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        finally:
            try:
                os.close(res_r)
            except OSError:
                pass
        # 只取第一条完整结果；后续行（重复完成）一律忽略
        line = (data or b"").split(b"\n", 1)[0]
        try:
            payload = json.loads(line.decode("utf-8"))
            result = WorkerResult(
                status=str(payload.get("status", "crashed")),
                error_code=payload.get("error_code"),
                refresh_token=payload.get("refresh_token"),
                callback_state=payload.get("state"),
                details=payload.get("details"),
            )
        except (ValueError, TypeError):
            result = WorkerResult("crashed", "worker_crashed")
        with self._lock:
            if self._result is None:
                self._result = result

    # ---- 等待 / 取消 / 超时 ----

    def wait(self, timeout: float | None = None) -> WorkerResult:
        proc = self._proc
        if proc is None:
            return self._result or WorkerResult("start_failed", "worker_start_failed")
        try:
            proc.wait(timeout=timeout if timeout is not None else self._timeout)
        except subprocess.TimeoutExpired:
            self._terminate()  # 先树杀（无顺序窗口），再关管道
            self._close_request_pipe()
            with self._lock:
                self._forced = WorkerResult("timeout", "authorization_timeout")
        if self._reader is not None:
            self._reader.join(timeout=10)
        # 正常完成：释放请求管道句柄，避免重复登录累计
        self._close_request_pipe()
        with self._lock:
            forced = self._forced
            result = self._result
        if forced is not None:
            return forced
        if result is not None:
            return result
        return WorkerResult("crashed", "worker_crashed")

    def cancel(self) -> WorkerResult:
        """先树杀（worker 存活时连带未登记后代），再关管道并确认死亡。"""
        self._terminate()
        self._close_request_pipe()
        with self._lock:
            self._forced = WorkerResult("cancelled", "authorization_cancelled")
            result = self._result
        # worker 在取消前已完成 → 返回真实完成结果；否则一律 cancelled
        if result is not None and result.status == "completed":
            return result
        return self._forced

    def close_parent(self) -> None:
        """模拟父进程退出：关闭请求管道，让 worker 自查父端关闭并清理。"""
        self._close_request_pipe()

    def _close_request_pipe(self) -> None:
        with self._lock:
            fd = self._request_write_fd
            self._request_write_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _terminate(self) -> None:
        """只终止已核验 PID 的进程树（含未登记后代）。"""
        proc = self._proc
        if proc is not None:
            kill_process_tree(proc.pid)
