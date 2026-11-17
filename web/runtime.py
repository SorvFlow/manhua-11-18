"""Bind the first free port and hand the already-open socket to Uvicorn."""

from __future__ import annotations

import errno
import os
import socket
from pathlib import Path

import uvicorn

from core.comic.locks import exclusive_lock
from web.app import create_app
from web.settings import Settings


def bind_available(host: str, start: int, end: int) -> socket.socket:
    if not 1024 <= start <= end <= 65535:
        raise ValueError("端口范围须满足 1024 <= 起始端口 <= 结束端口 <= 65535。")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in range(start, end + 1):
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
            listener.listen(128)
            return listener
        except OSError as exc:
            listener.close()
            if exc.errno != errno.EADDRINUSE:
                raise
            print(f"端口 {port} 已被占用，继续探测下一个端口。", flush=True)
    raise RuntimeError(f"端口范围 {start}-{end} 全部被占用。")


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="ascii")
    temporary.replace(path)


def notify_ready(port: int) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.connect(address)
        client.sendall(f"READY=1\nSTATUS=sorvflow listening on port {port}".encode())


def run(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    port_end: int | None = None,
    runtime_dir: Path | None = None,
    settings: Settings | None = None,
) -> None:
    settings = settings or Settings.from_env()
    from web.server import is_loopback_hostname

    if not is_loopback_hostname(host) and not settings.public_url:
        raise ValueError("对外监听前请先配置 SORVFLOW_PUBLIC_URL（HTTPS 网站地址）。")
    runtime_dir = runtime_dir or settings.state_dir / "run"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(
        runtime_dir / "launcher.lock", blocking=False, busy_message="此运行目录已有服务进程。"
    ):
        listener = bind_available(
            host, port, port_end if port_end is not None else min(port + 50, 65535)
        )
        actual = listener.getsockname()[1]

        def cleanup_runtime():
            for name, expected in (("port", actual), ("pid", os.getpid())):
                path = runtime_dir / name
                try:
                    if path.read_text().strip() == str(expected):
                        path.unlink()
                except OSError:
                    pass

        class Server(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if self.started:
                    atomic_text(runtime_dir / "port", f"{actual}\n")
                    atomic_text(runtime_dir / "pid", f"{os.getpid()}\n")
                    notify_ready(actual)

            async def shutdown(self, sockets=None):
                try:
                    await super().shutdown(sockets=sockets)
                finally:
                    # Uvicorn re-raises termination signals after shutdown;
                    # cleanup must happen before its signal handler is restored.
                    cleanup_runtime()

        try:
            app = create_app(settings)
            print(f"sorvflow 多账号工作台：http://{host}:{actual}", flush=True)
            config = uvicorn.Config(
                app,
                host=host,
                port=actual,
                log_level="info",
                proxy_headers=True,
                forwarded_allow_ips="127.0.0.1,::1",
            )
            server = Server(config)
            server.run(sockets=[listener])
            if not server.started:
                raise RuntimeError("Web 服务启动失败，请查看上方日志。")
        finally:
            listener.close()
            cleanup_runtime()
