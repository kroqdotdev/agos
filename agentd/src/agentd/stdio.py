"""`agentd mcp`: MCP over stdio, forwarding to `agentd serve` when it runs.

Forwarding keeps one takeover lease, one input lock and one audit log for
every agent in the VM. Local mode drives the display in-process (useful on a
workstation without the server); it has no lease shared with anyone.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from agentd import mcp_server
from agentd.audit import AuditLog
from agentd.backend.x11 import X11Backend
from agentd.config import Config
from agentd.core import Core
from agentd.errors import AgentdError
from agentd.service import LocalService, RemoteService

log = logging.getLogger("agentd.stdio")


def make_factory(cfg: Config, display_overridden: bool = False):
    async def local() -> tuple[Any, str | None]:
        audit = AuditLog(cfg.audit_path, cfg.audit_max_bytes, cfg.audit_backups)
        core = Core(cfg, X11Backend(cfg.display, cfg.draw_cursor), audit)
        audit.write("start", mode="mcp-local", display=cfg.display, pid=os.getpid())
        return LocalService(core, cfg, audit), "default"

    async def remote(retries: int) -> tuple[Any, str | None]:
        svc = RemoteService(cfg.socket, cfg.display)
        last: AgentdError | None = None
        for attempt in range(retries):
            try:
                health = await svc.health()
                svc.display = health.get("display", cfg.display)
                if display_overridden and svc.display != cfg.display and cfg.mcp_mode == "auto":
                    raise LookupError("server drives another display")
                session = (await svc.create_session(None))["session"]  # type: ignore[arg-type]
                return svc, session
            except AgentdError as exc:
                last = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(1)
        assert last is not None
        raise last

    async def factory() -> tuple[Any, str | None]:
        mode = cfg.mcp_mode
        if mode == "local" or (mode == "auto" and not (cfg.socket and os.path.exists(cfg.socket))):
            return await local()
        try:
            return await remote(10 if mode == "remote" else 1)
        except LookupError:
            return await local()
        except AgentdError as exc:
            if mode == "remote":
                raise
            log.info("agentd server not reachable (%s); driving %s in-process", exc.message, cfg.display)
            return await local()

    return factory


def run_stdio(cfg: Config, display_overridden: bool = False) -> None:
    provider = mcp_server.StdioProvider(make_factory(cfg, display_overridden))
    mcp_server.build(provider, cfg.display).run()
