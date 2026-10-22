"""启动辅助：回环端口探测、就绪后打开浏览器、统一 uvicorn 启动。

只服务本地单用户 WebUI；不读凭据、不联网、不创建数据库。
"""

from __future__ import annotations

import socket
import threading
import time
import webbrowser


def check_loopback_port(port: int, *, host: str = "127.0.0.1") -> None:
    """端口可用性探测；占用时抛出 ValueError（绝不杀占用者、不换端口）。"""
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1..65535 内")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("只允许回环监听地址")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # 不设置 SO_REUSEADDR：Windows 上它会把已占用端口误判为空闲。
        try:
            probe.bind((host if host != "localhost" else "127.0.0.1", port))
        except OSError as exc:
            raise ValueError(
                f"端口 {host}:{port} 已被占用；请先停止原服务，或改用 --port 指定其他端口"
            ) from exc


def _port_accepts_connections(port: int) -> bool:
    """服务真正可连接才打开页面；避免只标记 started 但未监听时弹出“拒绝连接”。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def open_browser_when_ready(server, port: int, *, timeout: float = 30.0) -> None:
    """在服务真正就绪（端口可连接）后打开回环 URL；不打开尚未监听的地址。"""
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "should_exit", False):
            return
        if getattr(server, "started", False) and _port_accepts_connections(port):
            try:
                webbrowser.open(url)
            except Exception:
                return
            return
        time.sleep(0.1)


def run_server(application, *, port: int, open_browser: bool) -> int:
    """启动 uvicorn；主进程退出不遗留线程。"""
    import uvicorn

    config = uvicorn.Config(
        application, host="127.0.0.1", port=port, log_level="info"
    )
    server = uvicorn.Server(config)
    if open_browser:
        threading.Thread(
            target=open_browser_when_ready,
            args=(server, port),
            daemon=True,
            name="open-browser-when-ready",
        ).start()
    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    return 0
