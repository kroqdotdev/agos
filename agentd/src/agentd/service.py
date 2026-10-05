"""Operations behind every transport.

`LocalService` runs operations against an in-process `Core`; REST handlers
and in-process MCP tools call it. `RemoteService` speaks the same REST API
over the Unix socket, so `agentd mcp` can forward to a running `agentd serve`
(one lease, one input lock, one audit log). Both return JSON-ready dicts and
raise `AgentdError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import re
import socket
import time
import urllib.request
from typing import Any

from agentd import __version__, a11y, procs
from agentd.actions import parse_action, parse_batch
from agentd.adapters import Group, GroupOutcome, Outcome
from agentd.adapters import anthropic as anthropic_adapter
from agentd.adapters import gemini as gemini_adapter
from agentd.adapters import openai as openai_adapter
from agentd.audit import AuditLog
from agentd.auth import Principal
from agentd.config import Config
from agentd.core import Core
from agentd.errors import AgentdError, invalid
from agentd.geometry import COORD_SPACES, Frame, fit_size

BROWSER_RE = re.compile(r"chrom|firefox|browser|brave|epiphany", re.I)


class LocalService:
    def __init__(self, core: Core, config: Config, audit: AuditLog) -> None:
        self.core = core
        self.config = config
        self.audit = audit

    @property
    def display(self) -> str:
        return self.core.backend.display

    # ------------------------------------------------------------ status
    async def health(self) -> dict[str, Any]:
        return {"ok": True, "display": self.display, "version": __version__}

    async def status(self, p: Principal) -> dict[str, Any]:
        p.require("observe")
        out = {"version": __version__, **self.core.status(), "viewer_url": self.config.viewer_url}
        try:
            out["screen"] = list(await asyncio.wait_for(self.core.backend.screen_size(), 2))
            out["display_ok"] = True
        except (AgentdError, TimeoutError) as exc:
            out["screen"] = None
            out["display_ok"] = False
            out["display_error"] = getattr(exc, "message", "timeout")
        return out

    async def create_session(self, p: Principal) -> dict[str, Any]:
        p.require("observe")
        return {"session": self.core.new_session()}

    # -------------------------------------------------------- observation
    async def screenshot(self, p: Principal, session: str | None = None, format: str | None = None) -> dict[str, Any]:
        p.require("observe")
        if format is not None and format not in ("png", "jpeg", "webp"):
            raise invalid("format must be png, jpeg or webp")
        shot = await self.core.screenshot(session, format)
        return shot.to_json()

    async def actions(
        self, p: Principal, session: str | None, actions: Any, screenshot_after: bool = True, format: str | None = None
    ) -> dict[str, Any]:
        parsed = parse_batch(actions)
        if any(a.is_input for a in parsed):
            p.require("input")
        else:
            p.require("observe")
        if format is not None and format not in ("png", "jpeg", "webp"):
            raise invalid("format must be png, jpeg or webp")
        result = await self.core.execute(session, parsed, p, screenshot_after=bool(screenshot_after), fmt=format)
        return result.to_json()

    async def wait_for_stable(
        self, p: Principal, timeout: float | None = None, settle_ms: int | None = None
    ) -> dict[str, Any]:
        p.require("observe")
        return await self.core.wait_for_stable(timeout, settle_ms)

    async def windows(self, p: Principal) -> dict[str, Any]:
        p.require("observe")
        return {"windows": await self.core.backend.windows()}

    async def activate_window(self, p: Principal, wid: int) -> dict[str, Any]:
        p.require("input")
        async with self.core.input_lock:
            self.core._check_lease()
            await self.core.backend.activate_window(wid)
        self.audit.write("window", principal=p.name, transport=p.transport, activate=wid)
        return {"ok": True, "id": wid}

    async def a11y(
        self,
        p: Principal,
        session: str | None = None,
        window: str | None = None,
        max_depth: int | None = None,
        max_nodes: int | None = None,
        coord_space: str = "image",
    ) -> dict[str, Any]:
        p.require("observe")
        if coord_space not in COORD_SPACES:
            raise invalid(f"coord_space must be one of {', '.join(COORD_SPACES)}")
        depth = min(max_depth or self.config.a11y_max_depth, 50)
        nodes_max = min(max_nodes or self.config.a11y_max_nodes, 5000)
        nodes, info = await a11y.tree(window, depth, nodes_max)
        s = self.core.session(session)
        screen = await self.core.backend.screen_size()
        frame = s.last_frame
        out: dict[str, Any] = {"session": s.id, "coord_space": coord_space}
        if coord_space == "image" and (frame is None or frame.screen != screen):
            frame = Frame(
                0,
                self.core.epoch,
                screen,
                fit_size(*screen, self.config.max_image_long_edge, self.config.max_image_pixels),
            )
            out["note"] = "no screenshot of the current screen yet; boxes use the next screenshot's space"
        out["frame_id"] = frame.frame_id if frame and frame.frame_id else None
        elements = a11y.convert(nodes, coord_space, screen, frame)
        out.update(info, count=len(elements), elements=elements)
        return out

    # ------------------------------------------------------- clipboard
    async def clipboard_get(self, p: Principal) -> dict[str, Any]:
        p.require("files")
        text = await self.core.backend.clipboard_get()
        self.audit.write("clipboard", principal=p.name, transport=p.transport, op="get", length=len(text))
        return {"text": text}

    async def clipboard_set(self, p: Principal, text: Any) -> dict[str, Any]:
        p.require("files")
        if not isinstance(text, str):
            raise invalid("text must be a string")
        await self.core.backend.clipboard_set(text)
        self.audit.write("clipboard", principal=p.name, transport=p.transport, op="set", length=len(text))
        return {"ok": True, "length": len(text)}

    # ------------------------------------------------------- processes
    def _env(self, extra: Any) -> dict[str, str]:
        env = self.core.backend.subprocess_env()
        if extra is not None:
            if not isinstance(extra, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in extra.items()
            ):
                raise invalid("env must map strings to strings")
            env.update(extra)
        return env

    async def launch(self, p: Principal, argv: Any, env: Any = None, cwd: str | None = None) -> dict[str, Any]:
        p.require("exec")
        pid = await procs.launch(argv, self._env(env), cwd)
        self.audit.write("launch", principal=p.name, transport=p.transport, argv=argv[:20], pid=pid)
        return {"pid": pid}

    async def exec(
        self,
        p: Principal,
        argv: Any = None,
        command: Any = None,
        timeout: Any = 30,
        cwd: str | None = None,
        env: Any = None,
        stdin: Any = None,
    ) -> dict[str, Any]:
        p.require("exec")
        if command is not None:
            if not isinstance(command, str) or not command:
                raise invalid("command must be a non-empty string")
            argv = ["/bin/sh", "-c", command]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise invalid("timeout must be a positive number of seconds")
        if stdin is not None and not isinstance(stdin, str):
            raise invalid("stdin must be a string")
        timeout = min(float(timeout), self.config.exec_timeout_max)
        res = await procs.run(argv, self._env(env), timeout=timeout, cwd=cwd, stdin=stdin)
        self.audit.write(
            "exec",
            principal=p.name,
            transport=p.transport,
            argv=[a[:200] for a in argv[:20]],
            exit_code=res["exit_code"],
            timed_out=res["timed_out"],
            ms=res["duration_ms"],
        )
        return res

    # ----------------------------------------------------------- lease
    async def lease(self, p: Principal) -> dict[str, Any]:
        p.require("observe")
        return {**self.core.lease_json(), "epoch": self.core.epoch}

    async def takeover(self, p: Principal, by: Any = None, reason: Any = None, ttl: Any = None) -> dict[str, Any]:
        p.require("takeover")
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0):
            raise invalid("ttl must be a positive number of seconds")
        by = str(by) if by else p.name
        lease = await self.core.takeover(by[:100], str(reason or "")[:500], ttl, p)
        return {**lease, "epoch": self.core.epoch}

    async def handback(self, p: Principal, by: Any = None) -> dict[str, Any]:
        p.require("takeover")
        return await self.core.handback(str(by or p.name)[:100], p)

    async def set_display(self, p: Principal, width: Any, height: Any) -> dict[str, Any]:
        p.require("admin")
        if not all(isinstance(v, int) and not isinstance(v, bool) and 64 <= v <= 16384 for v in (width, height)):
            raise invalid("width and height must be integers between 64 and 16384")
        async with self.core.input_lock:
            size = await self.core.backend.set_resolution(width, height)
        self.audit.write("display", principal=p.name, transport=p.transport, size=list(size))
        return {"screen": list(size)}

    async def audit_tail(self, p: Principal, limit: int = 50) -> dict[str, Any]:
        p.require("takeover")
        return {"entries": self.audit.tail(max(1, min(limit, 500)))}

    # -------------------------------------------------------- adapters
    async def run_groups(
        self, p: Principal, session: str | None, groups: list[Group], policy: str, fmt: str | None, source: str
    ) -> Outcome:
        """Run provider calls as one batch: in order, stop at the first failure.

        policy: "always" (attach a screenshot), "if_changed" (only after input
        or waits) or "never".
        """
        core = self.core
        core.session(session)
        parsed: list[list[Any] | None] = []
        for g in groups:
            if g.error is not None:
                parsed.append(None)
                continue
            try:
                parsed.append([parse_action(a) for a in g.actions])
            except AgentdError as exc:
                g.error = exc
                parsed.append(None)
        lock_needed = any(g.special for g in groups) or any(a.is_input for acts in parsed if acts for a in acts)
        outcomes: list[GroupOutcome] = []
        error: AgentdError | None = None
        final = None
        changed = dirty = False
        last_full = None
        async with core.input_lock if lock_needed else contextlib.nullcontext():
            for g, acts in zip(groups, parsed, strict=True):
                if error is not None:
                    outcomes.append(GroupOutcome("skipped"))
                    continue
                if g.error is not None or acts is None:
                    error = g.error or invalid("invalid call")
                    outcomes.append(GroupOutcome("error", error))
                    self.audit.write(
                        "adapter",
                        principal=p.name,
                        transport=p.transport,
                        source=source,
                        call=g.meta,
                        result=error.code,
                        message=error.message,
                    )
                    continue
                if g.special == "open_web_browser":
                    try:
                        core._check_lease()
                        await self._open_browser(p)
                        changed = dirty = True
                        outcomes.append(GroupOutcome("ok"))
                    except AgentdError as exc:
                        error = exc
                        outcomes.append(GroupOutcome("error", exc))
                    continue
                if not acts:
                    outcomes.append(GroupOutcome("ok"))
                    continue
                br = await core.execute(
                    session, acts, p, screenshot_after=False, fmt=fmt, source=source, take_lock=False
                )
                for r in br.results:
                    a = acts[r.index]
                    if r.ok and (a.is_input or a.type in ("wait", "wait_for_stable")):
                        changed = dirty = True
                    if r.shot is not None and not r.shot.meta.get("zoom"):
                        last_full, dirty = r.shot, False
                if br.error is not None:
                    error = br.error
                    final = br.screenshot
                    outcomes.append(GroupOutcome("error", br.error, br.results))
                else:
                    outcomes.append(GroupOutcome("ok", None, br.results))
            want = policy == "always" or (policy == "if_changed" and changed)
            if want and final is None and (error is None or error.code != "DISPLAY_UNAVAILABLE"):
                if last_full is not None and not dirty:
                    final = last_full
                else:
                    with contextlib.suppress(AgentdError):
                        if changed:
                            await core.wait_for_stable()
                        final = await core.screenshot(session, fmt)
        return Outcome(outcomes, final, error)

    async def _open_browser(self, p: Principal) -> None:
        for w in await self.core.backend.windows():
            if BROWSER_RE.search(f"{w['class']} {w['instance']}"):
                await self.core.backend.activate_window(w["id"])
                return
        p.require("exec")
        await procs.launch(list(self.config.browser_command), self.core.backend.subprocess_env())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if any(BROWSER_RE.search(f"{w['class']} {w['instance']}") for w in await self.core.backend.windows()):
                await self.core.wait_for_stable(5.0)
                return
        raise AgentdError("INTERNAL", f"no browser window appeared after running {self.config.browser_command}")

    async def anthropic(
        self, p: Principal, session: str | None, body: Any, screenshot_after: bool = True, fmt: str | None = None
    ) -> tuple[Any, AgentdError | None]:
        p.require("input")
        groups, style = anthropic_adapter.parse(body)
        outcome = await self.run_groups(
            p, session, groups, "if_changed" if screenshot_after else "never", fmt, "anthropic"
        )
        return anthropic_adapter.render(groups, outcome, style), outcome.error

    async def openai(
        self, p: Principal, session: str | None, body: Any, fmt: str | None = None
    ) -> tuple[Any, AgentdError | None]:
        p.require("input")
        groups, meta = openai_adapter.parse(body)
        outcome = await self.run_groups(p, session, groups, "always", fmt, "openai")
        return openai_adapter.render(outcome, meta), outcome.error

    async def gemini(
        self, p: Principal, session: str | None, body: Any, fmt: str | None = None
    ) -> tuple[Any, AgentdError | None]:
        p.require("input")
        screen = await self.core.backend.screen_size()
        groups, many = gemini_adapter.parse(body, screen, self.config.search_url)
        outcome = await self.run_groups(p, session, groups, "always", fmt, "gemini")
        url = await asyncio.to_thread(current_url, self.config.cdp_url)
        return gemini_adapter.render(groups, outcome, many, url), outcome.error


def current_url(cdp_url: str) -> str:
    """Best effort: the URL of the most recently focused Chromium page via DevTools."""
    if not cdp_url:
        return ""
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=0.5) as resp:
            targets = json.loads(resp.read())
        for t in targets:
            if t.get("type") == "page":
                return str(t.get("url", ""))
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- remote
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def unix_request(
    path: str, method: str, url: str, body: Any = None, timeout: float = 30
) -> tuple[int, dict[str, str], Any]:
    conn = UnixHTTPConnection(path, timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, url, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        data = json.loads(raw) if raw else None
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


class RemoteService:
    """Same surface as LocalService (as used by the MCP tools), over the Unix socket."""

    def __init__(self, socket_path: str, display: str = "") -> None:
        self.socket_path = socket_path
        self.display = display

    async def _call(self, method: str, url: str, body: Any = None, timeout: float = 30, batch: bool = False) -> Any:
        try:
            status, _, data = await asyncio.to_thread(unix_request, self.socket_path, method, url, body, timeout)
        except (OSError, http.client.HTTPException) as exc:
            raise AgentdError("INTERNAL", f"agentd server at {self.socket_path} unreachable: {exc}") from None
        if status >= 400:
            if batch and isinstance(data, dict) and "results" in data:
                return data
            raise AgentdError.from_json(data, status)
        return data

    async def health(self) -> dict[str, Any]:
        return await self._call("GET", "/v1/health", timeout=5)

    async def status(self, p: Principal) -> dict[str, Any]:
        return await self._call("GET", "/v1/status")

    async def create_session(self, p: Principal) -> dict[str, Any]:
        return await self._call("POST", "/v1/sessions", {})

    async def screenshot(self, p: Principal, session: str | None = None, format: str | None = None) -> dict[str, Any]:
        return await self._call("POST", "/v1/screenshot", {"session": session, "format": format})

    async def actions(
        self, p: Principal, session: str | None, actions: Any, screenshot_after: bool = True, format: str | None = None
    ) -> dict[str, Any]:
        body = {"session": session, "actions": actions, "screenshot_after": screenshot_after, "format": format}
        return await self._call("POST", "/v1/actions", body, timeout=700, batch=True)

    async def wait_for_stable(
        self, p: Principal, timeout: float | None = None, settle_ms: int | None = None
    ) -> dict[str, Any]:
        act: dict[str, Any] = {"type": "wait_for_stable", "timeout": timeout, "settle_ms": settle_ms}
        res = await self.actions(p, None, [act], screenshot_after=False)
        if res.get("error"):
            raise AgentdError.from_json(res)
        return {k: v for k, v in res["results"][0].items() if k in ("stable", "waited_ms")}

    async def windows(self, p: Principal) -> dict[str, Any]:
        return await self._call("GET", "/v1/windows")

    async def activate_window(self, p: Principal, wid: int) -> dict[str, Any]:
        return await self._call("POST", f"/v1/windows/{int(wid)}/activate", {})

    async def a11y(
        self,
        p: Principal,
        session: str | None = None,
        window: str | None = None,
        max_depth: int | None = None,
        max_nodes: int | None = None,
        coord_space: str = "image",
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        q = {
            k: v
            for k, v in {
                "session": session,
                "window": window,
                "max_depth": max_depth,
                "max_nodes": max_nodes,
                "coord_space": coord_space,
            }.items()
            if v is not None
        }
        return await self._call("GET", "/v1/a11y?" + urlencode(q), timeout=60)

    async def clipboard_get(self, p: Principal) -> dict[str, Any]:
        return await self._call("GET", "/v1/clipboard")

    async def clipboard_set(self, p: Principal, text: Any) -> dict[str, Any]:
        return await self._call("PUT", "/v1/clipboard", {"text": text})

    async def launch(self, p: Principal, argv: Any, env: Any = None, cwd: str | None = None) -> dict[str, Any]:
        return await self._call("POST", "/v1/launch", {"argv": argv, "env": env, "cwd": cwd})
