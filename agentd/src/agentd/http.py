"""One Starlette app: REST under /v1, MCP Streamable HTTP at /mcp, UI at /ui.

The same app serves TCP and the Unix socket; `scope["agentd.transport"]`
(set by the per-listener wrapper in server.py) tells them apart. TCP always
needs a bearer token (tailscale serve proxies from localhost, so loopback is
not trusted); the Unix socket is trusted after the SO_PEERCRED uid check.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib import resources
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from agentd import mcp_server
from agentd.audit import AuditLog
from agentd.auth import Principal, TokenStore, bearer_from_header, trusted
from agentd.errors import AgentdError, invalid
from agentd.service import LocalService

log = logging.getLogger("agentd.http")
MAX_BODY = 16 * 1024 * 1024
Handler = Callable[[Request, Principal], Awaitable[Any]]


def authenticate(scope: Scope, tokens: TokenStore, audit: AuditLog | None = None) -> Principal:
    transport = scope.get("agentd.transport", "tcp")
    if transport == "unix":
        return trusted("unix", f"unix:uid={scope.get('agentd.peer_uid', '?')}")
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    token = bearer_from_header(headers.get("authorization"))
    principal = tokens.verify(token, transport) if token else None
    if principal is None:
        if audit is not None:
            client = scope.get("client")
            audit.write(
                "auth",
                result="UNAUTHORIZED",
                path=scope.get("path"),
                client=f"{client[0]}:{client[1]}" if client else None,
                token_present=bool(token),
            )
        raise AgentdError("UNAUTHORIZED", "missing or invalid bearer token")
    return principal


async def read_json(request: Request, required: bool = True) -> Any:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise invalid(f"request body larger than {MAX_BODY} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        if required:
            raise invalid("expected a JSON body")
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise invalid(f"invalid JSON: {exc}") from None


def _error(exc: AgentdError) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Bearer realm="agentd"'} if exc.code == "UNAUTHORIZED" else None
    return JSONResponse(exc.to_json(), status_code=exc.status, headers=headers)


def _header_safe(text: str) -> str:
    return text.encode("ascii", "replace").decode()[:300].replace("\r", " ").replace("\n", " ")


class MCPEndpoint:
    """ASGI wrapper: authenticate, then hand the request to the MCP session manager."""

    def __init__(self, server: Any, tokens: TokenStore, audit: AuditLog) -> None:
        self.server = server
        self.tokens = tokens
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            scope["agentd.principal"] = authenticate(scope, self.tokens, self.audit)
        except AgentdError as exc:
            await _error(exc)(scope, receive, send)
            return
        await self.server.session_manager.handle_request(scope, receive, send)


def create_app(service: LocalService, tokens: TokenStore) -> Starlette:
    audit = service.audit
    mcp = mcp_server.build(mcp_server.HTTPProvider(service), service.display)
    # Every request carries a bearer token (or arrives on the peer-checked
    # socket), so DNS rebinding cannot reach it; Host checks would only break
    # `tailscale serve`, which forwards the tailnet hostname.
    mcp.streamable_http_app(transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    def endpoint(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Response:
            try:
                principal = authenticate(request.scope, tokens, audit)
                out = await handler(request, principal)
            except AgentdError as exc:
                return _error(exc)
            except Exception as exc:  # pragma: no cover - last resort
                log.exception("unhandled error in %s", request.url.path)
                return _error(AgentdError("INTERNAL", f"{type(exc).__name__}: {exc}"))
            return out if isinstance(out, Response) else JSONResponse(out)

        return run

    def session_of(request: Request, body: Any = None) -> str | None:
        if isinstance(body, dict) and body.get("session"):
            return str(body["session"])
        return request.query_params.get("session") or request.headers.get("x-agentd-session") or None

    async def health(request: Request) -> Response:
        return JSONResponse(await service.health())

    async def status(request: Request, p: Principal) -> Any:
        return await service.status(p)

    async def sessions(request: Request, p: Principal) -> Any:
        return await service.create_session(p)

    async def screenshot(request: Request, p: Principal) -> Any:
        body = await read_json(request, required=False)
        return await service.screenshot(p, session_of(request, body), body.get("format"))

    async def actions(request: Request, p: Principal) -> Any:
        body = await read_json(request)
        if not isinstance(body, dict):
            raise invalid("body must be an object with `actions`")
        res = await service.actions(
            p, session_of(request, body), body.get("actions"), body.get("screenshot_after", True), body.get("format")
        )
        status = AgentdError(res["error"]["code"], "").status if res.get("error") else 200
        return JSONResponse(res, status_code=status)

    def adapter(name: str) -> Handler:
        async def run(request: Request, p: Principal) -> Any:
            body = await read_json(request)
            fmt = request.query_params.get("format")
            if fmt is not None and fmt not in ("png", "jpeg", "webp"):
                raise invalid("format must be png, jpeg or webp")
            session = session_of(request)
            if name == "anthropic":
                after = request.query_params.get("screenshot_after", "true").lower() not in ("0", "false", "no")
                out, err = await service.anthropic(p, session, body, after, fmt)
            elif name == "openai":
                out, err = await service.openai(p, session, body, fmt)
            else:
                out, err = await service.gemini(p, session, body, fmt)
            headers = {}
            if err is not None:
                # Provider-shaped bodies stay forwardable as-is; agentd's own
                # error travels in headers.
                headers = {"X-Agentd-Error-Code": err.code, "X-Agentd-Error-Message": _header_safe(err.message)}
            return JSONResponse(out, headers=headers)

        return run

    async def windows(request: Request, p: Principal) -> Any:
        return await service.windows(p)

    async def activate(request: Request, p: Principal) -> Any:
        return await service.activate_window(p, int(request.path_params["wid"]))

    async def clipboard(request: Request, p: Principal) -> Any:
        if request.method == "GET":
            return await service.clipboard_get(p)
        body = await read_json(request)
        return await service.clipboard_set(p, body.get("text") if isinstance(body, dict) else None)

    async def launch(request: Request, p: Principal) -> Any:
        body = await read_json(request)
        if not isinstance(body, dict):
            raise invalid("body must be an object with `argv`")
        return await service.launch(p, body.get("argv"), body.get("env"), body.get("cwd"))

    async def exec_(request: Request, p: Principal) -> Any:
        body = await read_json(request)
        if not isinstance(body, dict):
            raise invalid("body must be an object with `argv` or `command`")
        return await service.exec(
            p,
            body.get("argv"),
            body.get("command"),
            body.get("timeout", 30),
            body.get("cwd"),
            body.get("env"),
            body.get("stdin"),
        )

    async def a11y_(request: Request, p: Principal) -> Any:
        q = request.query_params

        def opt_int(key: str) -> int | None:
            v = q.get(key)
            if v is None:
                return None
            if not v.isdigit():
                raise invalid(f"{key} must be a positive integer")
            return int(v)

        return await service.a11y(
            p,
            q.get("session"),
            q.get("window"),
            opt_int("max_depth"),
            opt_int("max_nodes"),
            q.get("coord_space", "image"),
        )

    async def takeover(request: Request, p: Principal) -> Any:
        if request.method == "GET":
            return await service.lease(p)
        if request.method == "DELETE":
            body = await read_json(request, required=False)
            return await service.handback(p, body.get("by") if isinstance(body, dict) else None)
        body = await read_json(request, required=False)
        if not isinstance(body, dict):
            raise invalid("body must be an object")
        return await service.takeover(p, body.get("by"), body.get("reason"), body.get("ttl"))

    async def display(request: Request, p: Principal) -> Any:
        body = await read_json(request)
        if not isinstance(body, dict):
            raise invalid("body must be {width, height}")
        return await service.set_display(p, body.get("width"), body.get("height"))

    async def audit_(request: Request, p: Principal) -> Any:
        limit = request.query_params.get("limit", "50")
        return await service.audit_tail(p, int(limit) if limit.isdigit() else 50)

    ui_html = resources.files("agentd").joinpath("ui.html").read_text()

    async def ui(request: Request) -> Response:
        return HTMLResponse(
            ui_html,
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src 'self' data:",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    async def root(request: Request) -> Response:
        return RedirectResponse("/ui")

    mcp_endpoint = MCPEndpoint(mcp, tokens, audit)
    routes = [
        Route("/", root),
        Route("/ui", ui),
        Route("/v1/health", health),
        Route("/v1/status", endpoint(status)),
        Route("/v1/sessions", endpoint(sessions), methods=["POST"]),
        Route("/v1/screenshot", endpoint(screenshot), methods=["POST"]),
        Route("/v1/actions", endpoint(actions), methods=["POST"]),
        Route("/v1/adapters/anthropic", endpoint(adapter("anthropic")), methods=["POST"]),
        Route("/v1/adapters/openai", endpoint(adapter("openai")), methods=["POST"]),
        Route("/v1/adapters/gemini", endpoint(adapter("gemini")), methods=["POST"]),
        Route("/v1/windows", endpoint(windows)),
        Route("/v1/windows/{wid:int}/activate", endpoint(activate), methods=["POST"]),
        Route("/v1/clipboard", endpoint(clipboard), methods=["GET", "PUT"]),
        Route("/v1/launch", endpoint(launch), methods=["POST"]),
        Route("/v1/exec", endpoint(exec_), methods=["POST"]),
        Route("/v1/a11y", endpoint(a11y_)),
        Route("/v1/takeover", endpoint(takeover), methods=["GET", "POST", "DELETE"]),
        Route("/v1/display", endpoint(display), methods=["POST"]),
        Route("/v1/audit", endpoint(audit_)),
        Route("/mcp", mcp_endpoint),
        Route("/mcp/", mcp_endpoint),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.service = service
    app.state.mcp = mcp
    return app
