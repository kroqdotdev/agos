"""`agentd serve`: one ASGI app on TCP and on a peer-checked Unix socket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import stat
import struct
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.protocols.http.h11_impl import H11Protocol

from agentd.audit import AuditLog
from agentd.auth import TokenStore
from agentd.backend.x11 import X11Backend
from agentd.config import Config, parse_listen
from agentd.core import Core
from agentd.http import create_app
from agentd.service import LocalService

log = logging.getLogger("agentd")


# ------------------------------------------------------------- sd_notify
def sd_notify(message: str) -> bool:
    """Minimal sd_notify(3): no libsystemd dependency."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError as exc:
        log.warning("sd_notify failed: %s", exc)
        return False


def watchdog_interval() -> float | None:
    usec = os.environ.get("WATCHDOG_USEC")
    pid = os.environ.get("WATCHDOG_PID")
    if not usec or not usec.isdigit():
        return None
    if pid and pid.isdigit() and int(pid) != os.getpid():
        return None
    return int(usec) / 1e6 / 2


# ------------------------------------------------------- socket plumbing
def peer_uid(sock: Any) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def peer_checked_protocol(audit: AuditLog, allowed_uid: int) -> type[H11Protocol]:
    class PeerCheckedH11(H11Protocol):
        """Drops Unix-socket connections whose peer uid is not ours."""

        _rejected = False

        def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
            sock = transport.get_extra_info("socket")
            try:
                uid = peer_uid(sock)
            except OSError:
                uid = -1
            if uid != allowed_uid:
                self._rejected = True
                audit.write("auth", result="UNAUTHORIZED", transport="unix", peer_uid=uid)
                transport.close()
                return
            super().connection_made(transport)

        def data_received(self, data: bytes) -> None:
            if not self._rejected:
                super().data_received(data)

        def connection_lost(self, exc: Exception | None) -> None:
            if not self._rejected:
                super().connection_lost(exc)

    return PeerCheckedH11


class Tagged:
    """Marks which listener a request came through."""

    def __init__(self, app: ASGIApp, transport: str) -> None:
        self.app = app
        self.transport = transport

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope["agentd.transport"] = self.transport
            if self.transport == "unix":
                scope["agentd.peer_uid"] = os.getuid()
        await self.app(scope, receive, send)


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # serve() owns signals for both listeners.
        yield


def tcp_socket(listen: str) -> socket.socket:
    host, port = parse_listen(listen)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def unix_socket(path: str) -> socket.socket:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.exists() or p.is_symlink():
        if not stat.S_ISSOCK(p.lstat().st_mode):
            raise RuntimeError(f"{path} exists and is not a socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(path)
            raise RuntimeError(f"another agentd is already listening on {path}")
        except (ConnectionRefusedError, FileNotFoundError):
            p.unlink()
        finally:
            probe.close()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)  # the socket file is born 0600
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    sock.listen(128)
    sock.setblocking(False)
    return sock


# ----------------------------------------------------------------- serve
def build(config: Config) -> tuple[Any, LocalService, TokenStore]:
    audit = AuditLog(config.audit_path, config.audit_max_bytes, config.audit_backups)
    backend = X11Backend(config.display, config.draw_cursor)
    core = Core(config, backend, audit)
    service = LocalService(core, config, audit)
    tokens = TokenStore(config.tokens_path, env_token=os.environ.get("AGENTD_TOKEN") or None)
    return create_app(service, tokens), service, tokens


async def serve(
    config: Config, on_ready: Callable[[dict[str, Any]], None] | None = None, stop: asyncio.Event | None = None
) -> None:
    app, service, tokens = build(config)
    audit = service.audit
    if not config.listen and not config.socket:
        raise RuntimeError("both listen and socket are disabled; nothing to serve")

    listeners: list[tuple[_Server, socket.socket, str]] = []
    common = dict(
        lifespan="off",
        log_level="warning",
        access_log=False,
        ws="none",
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=3,
    )
    try:
        if config.listen:
            sock = tcp_socket(config.listen)
            cfg = uvicorn.Config(Tagged(app, "tcp"), http=H11Protocol, **common)  # type: ignore[arg-type]
            listeners.append((_Server(cfg), sock, "tcp"))
        if config.socket:
            usock = unix_socket(config.socket)
            proto = peer_checked_protocol(audit, os.getuid())
            cfg = uvicorn.Config(Tagged(app, "unix"), http=proto, **common)  # type: ignore[arg-type]
            listeners.append((_Server(cfg), usock, "unix"))
    except BaseException:
        for _, s, _ in listeners:
            s.close()
        raise

    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)

    info: dict[str, Any] = {"display": config.display}
    for _, s, kind in listeners:
        if kind == "tcp":
            host, port = s.getsockname()[:2]
            info["tcp"] = f"{host}:{port}"
        else:
            info["unix"] = s.getsockname()

    async def watchdog(interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            sd_notify("WATCHDOG=1")

    tasks: list[asyncio.Task[Any]] = []
    try:
        async with app.router.lifespan_context(app):
            for server, s, _ in listeners:
                tasks.append(asyncio.create_task(server.serve(sockets=[s])))
            while not all(server.started for server, _, _ in listeners):
                if any(t.done() for t in tasks):
                    for t in tasks:
                        if t.done() and t.exception():
                            raise t.exception()  # type: ignore[misc]
                await asyncio.sleep(0.01)
            audit.write("start", **info)
            log.info("agentd serving %s", info)
            sd_notify("READY=1\nSTATUS=serving " + " ".join(f"{k}={v}" for k, v in info.items()))
            interval = watchdog_interval()
            if interval:
                tasks.append(asyncio.create_task(watchdog(interval)))
            if on_ready is not None:
                on_ready(info)
            stop_task = asyncio.create_task(stop.wait())
            await asyncio.wait([stop_task, *tasks[: len(listeners)]], return_when=asyncio.FIRST_COMPLETED)
            sd_notify("STOPPING=1")
            for server, _, _ in listeners:
                server.should_exit = True
            stop_task.cancel()
            await asyncio.gather(*tasks[: len(listeners)], return_exceptions=True)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await service.core.shutdown()
        with contextlib.suppress(Exception):
            await service.core.backend.close()
        for _, s, kind in listeners:
            s.close()
            if kind == "unix" and config.socket:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(config.socket)
        audit.write("stop")
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
