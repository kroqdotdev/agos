"""MCP tools (SDK v2 `MCPServer`), shared by stdio and Streamable HTTP.

`computer` mirrors Anthropic's action-based computer tool so this server is a
drop-in replacement for the workstation's `desktop` server.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent, ToolAnnotations

from agentd import __version__
from agentd.adapters import anthropic as anthropic_adapter
from agentd.auth import Principal, trusted
from agentd.errors import AgentdError

ComputerAction = Literal[
    "screenshot",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "mouse_move",
    "left_click_drag",
    "left_mouse_down",
    "left_mouse_up",
    "scroll",
    "type",
    "key",
    "hold_key",
    "wait",
    "cursor_position",
    "zoom",
]

INSTRUCTIONS = (
    "Controls a Linux X11 desktop (display {display}) with the mouse, keyboard and screenshots. "
    "Use it for GUI-only work; prefer shell, browser DevTools or other tools when they can do the "
    "job. Start with a screenshot and always take coordinates from the most recent one. If the "
    "screen changed (resolution, or a human took over and handed back) an action is NOT performed "
    "and a fresh screenshot is returned instead (STALE_FRAME). While a human holds the takeover "
    "lease, input fails with HUMAN_IN_CONTROL; wait and retry later."
)


class Provider(Protocol):
    """Resolves the service and default session for a tool call."""

    async def resolve(self, ctx: Context) -> tuple[Any, Principal, str | None]: ...


class HTTPProvider:
    """Tools served over /mcp: principal comes from the authenticated request."""

    def __init__(self, service: Any) -> None:
        self.service = service

    async def resolve(self, ctx: Context) -> tuple[Any, Principal, str | None]:
        req = ctx.request_context.request
        principal = req.scope.get("agentd.principal") if req is not None else None
        if not isinstance(principal, Principal):
            raise ToolError("UNAUTHORIZED: no authenticated principal")
        headers = ctx.headers or {}
        mcp_sid = headers.get("mcp-session-id")
        # Handshake-era clients get one coordinate basis per MCP session; stateless
        # (2026-07-28) clients get one per token unless they pass `session`.
        session = f"mcp-{mcp_sid[:40]}" if mcp_sid else f"tok-{principal.name}"
        return self.service, principal, session


class StdioProvider:
    """stdio is trusted. `factory` picks a local or remote service on first use."""

    def __init__(self, factory: Callable[[], Awaitable[tuple[Any, str | None]]]) -> None:
        self.factory = factory
        self._resolved: tuple[Any, str | None] | None = None

    async def resolve(self, ctx: Context) -> tuple[Any, Principal, str | None]:
        if self._resolved is None:
            self._resolved = await self.factory()
        service, session = self._resolved
        return service, trusted("stdio"), session


def _fail(exc: AgentdError) -> ToolError:
    return ToolError(f"{exc.code}: {exc.message}")


def _image(shot: dict[str, Any]) -> ImageContent:
    return ImageContent(type="image", data=shot["data"], mime_type=shot["mime_type"])


def _describe(shot: dict[str, Any]) -> str:
    w, h = shot["image"]
    sw, sh = shot["screen"]
    return f"Screenshot {w}x{h} (screen {sw}x{sh}, frame {shot['frame_id']}, epoch {shot['epoch']})."


def build(provider: Provider, display: str) -> MCPServer:
    mcp = MCPServer("desktop", version=__version__, instructions=INSTRUCTIONS.format(display=display))

    async def resolve(ctx: Context) -> tuple[Any, Principal, str | None]:
        try:
            return await provider.resolve(ctx)
        except AgentdError as exc:
            raise _fail(exc) from None

    async def call(ctx: Context, fn: str, *args: Any, **kwargs: Any) -> Any:
        service, principal, _ = await resolve(ctx)
        try:
            return await getattr(service, fn)(principal, *args, **kwargs)
        except AgentdError as exc:
            raise _fail(exc) from None

    @mcp.tool(structured_output=False)
    async def computer(
        action: ComputerAction,
        ctx: Context,
        coordinate: list[int] | None = None,
        start_coordinate: list[int] | None = None,
        text: str | None = None,
        scroll_direction: Literal["up", "down", "left", "right"] | None = None,
        scroll_amount: int | None = None,
        duration: float | None = None,
        region: list[int] | None = None,
        repeat: int | None = None,
        session: str | None = None,
    ) -> list[TextContent | ImageContent]:
        """Use the mouse and keyboard on the desktop and take screenshots.

        Coordinates are [x, y] pixels in the most recent full screenshot this tool
        returned (screenshots are downscaled to fit 2576 px / 3.75 MP; the tool maps
        back to real pixels). If the screen changed since your last screenshot, actions
        that use coordinates are NOT performed; you get a fresh screenshot instead.

        Actions:
        - screenshot: capture the whole screen.
        - left_click / right_click / middle_click / double_click / triple_click: click at
          `coordinate` (or the cursor). `text` holds modifiers, e.g. "ctrl" or "ctrl+shift".
        - mouse_move: move the cursor to `coordinate`.
        - left_click_drag: drag from `start_coordinate` (default: cursor) to `coordinate`;
          `text` holds modifiers.
        - left_mouse_down / left_mouse_up: press or release the left button.
        - scroll: `scroll_direction` by `scroll_amount` wheel clicks (default 3) at
          `coordinate`; `text` holds modifiers.
        - type: type `text`.
        - key: xdotool key combos from `text` (space-separated sequences allowed), e.g.
          "ctrl+s", "Return", "alt+Tab", "ctrl+a BackSpace"; `repeat` 1-100.
        - hold_key: hold `text` for `duration` seconds (max 300).
        - wait: wait `duration` seconds (max 300), then screenshot.
        - cursor_position: report the cursor position.
        - zoom: `region` [x0, y0, x1, y1] at full detail; coordinates stay in the
          full-screenshot space.

        `session` (optional) selects an agentd coordinate session.
        Every action except cursor_position and zoom returns a fresh screenshot.
        """
        service, principal, default_session = await resolve(ctx)
        inp = {
            k: v
            for k, v in {
                "coordinate": coordinate,
                "start_coordinate": start_coordinate,
                "text": text,
                "scroll_direction": scroll_direction,
                "scroll_amount": scroll_amount,
                "duration": duration,
                "region": region,
                "repeat": repeat,
            }.items()
            if v is not None
        }
        try:
            actions = anthropic_adapter.to_canonical(action, inp)
        except AgentdError as exc:
            raise _fail(exc) from None
        want_after = action not in ("screenshot", "cursor_position", "zoom")
        try:
            res = await service.actions(principal, session or default_session, actions, screenshot_after=want_after)
        except AgentdError as exc:
            raise _fail(exc) from None
        err = res.get("error")
        shot = res.get("screenshot")
        if err:
            if err["code"] == "STALE_FRAME" and shot:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"{action} was NOT performed: {err['message']}. Re-issue it using coordinates "
                            f"from this screenshot. {_describe(shot)}"
                        ),
                    ),
                    _image(shot),
                ]
            raise ToolError(f"{err['code']}: {err['message']}")
        step = res["results"][-1]
        if action == "cursor_position":
            return [TextContent(type="text", text=f"X={step['x']}, Y={step['y']}")]
        if action == "zoom":
            z = step["screenshot"]
            zw, zh = z["image"]
            x0, y0, x1, y1 = z["region_screen"]
            return [
                TextContent(
                    type="text",
                    text=(
                        f"zoom of region {region} ({x1 - x0}x{y1 - y0} real px) shown at {zw}x{zh}. "
                        "Coordinates for other actions still use the full-screenshot space."
                    ),
                ),
                _image(z),
            ]
        if action == "screenshot":
            shot = step["screenshot"]
        if shot is None:
            return [TextContent(type="text", text=f"{action} done.")]
        return [TextContent(type="text", text=f"{action} done. {_describe(shot)}"), _image(shot)]

    @mcp.tool()
    async def windows(
        ctx: Context, action: Literal["list", "activate"] = "list", id: int | None = None
    ) -> dict[str, Any]:
        """List top-level windows (id, title, class, pid, geometry in screen pixels, active)
        or activate (focus and raise) the window with `id`."""
        if action == "activate":
            if id is None:
                raise ToolError("INVALID_ACTION: activate needs id")
            return await call(ctx, "activate_window", id)
        return await call(ctx, "windows")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def clipboard_get(ctx: Context) -> dict[str, Any]:
        """Read the text clipboard."""
        return await call(ctx, "clipboard_get")

    @mcp.tool()
    async def clipboard_set(text: str, ctx: Context) -> dict[str, Any]:
        """Replace the text clipboard (faster and safer than typing long text: set it,
        then press ctrl+v)."""
        return await call(ctx, "clipboard_set", text)

    @mcp.tool()
    async def launch(
        argv: list[str], ctx: Context, env: dict[str, str] | None = None, cwd: str | None = None
    ) -> dict[str, Any]:
        """Start a GUI application detached in the desktop session, e.g. ["agos-browser"]
        or ["xfce4-terminal"]. Returns its pid."""
        return await call(ctx, "launch", argv, env, cwd)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def wait_for_stable(
        ctx: Context, timeout: float | None = None, settle_ms: int | None = None
    ) -> dict[str, Any]:
        """Wait until the screen stops changing for `settle_ms` (default 300) or
        `timeout` seconds pass (default 3). Returns {stable, waited_ms}."""
        return await call(ctx, "wait_for_stable", timeout, settle_ms)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def status(ctx: Context) -> dict[str, Any]:
        """Display, screen size, takeover lease, epoch and sessions."""
        return await call(ctx, "status")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def a11y_tree(
        ctx: Context,
        window: str | None = None,
        max_depth: int | None = None,
        max_nodes: int | None = None,
        coord_space: Literal["image", "screen", "normalized"] = "image",
        session: str | None = None,
    ) -> dict[str, Any]:
        """Accessibility (AT-SPI) elements of one window: role, name, states and
        bounding boxes [x0, y0, x1, y1] in `coord_space` (default: pixels of your
        last screenshot). `window` filters by title or app name; default is the
        active window. Best effort: apps must expose AT-SPI."""
        service, principal, default_session = await resolve(ctx)
        try:
            return await service.a11y(principal, session or default_session, window, max_depth, max_nodes, coord_space)
        except AgentdError as exc:
            raise _fail(exc) from None

    return mcp
