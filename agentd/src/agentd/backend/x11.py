"""X11 backend: mss + Pillow capture, python-xlib queries, xdotool/xclip/xrandr.

All Xlib and mss calls run on one dedicated worker thread (neither library is
thread-safe and both block), so the event loop never stalls on X round trips.
Input goes through `xdotool` subprocesses started with argv lists only, with
`--` before any user-supplied text.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

import mss
import mss.exception
from PIL import Image
from Xlib import X, Xatom
from Xlib import display as xdisplay
from Xlib import error as xerror

from agentd.backend.base import Backend
from agentd.errors import AgentdError

T = TypeVar("T")

TYPE_CHUNK = 50
TYPE_DELAY_MS = 12
CLICK_DELAY_MS = 40
KEY_DELAY_MS = 50
DISPLAY_ERRORS = ("Can't open display", "cannot open display", "Error: Can't open display")


class X11Backend(Backend):
    def __init__(self, display: str, draw_cursor: bool = False) -> None:
        self.display = display
        self.draw_cursor = draw_cursor
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="agentd-x11")
        self._xd: xdisplay.Display | None = None
        self._sct: Any = None
        self._mss_backend = "default"
        self._atoms: dict[str, int] = {}
        env = dict(os.environ)
        env["DISPLAY"] = display
        self._env = env

    # ----------------------------------------------------------- plumbing
    def subprocess_env(self) -> dict[str, str]:
        return dict(self._env)

    async def _x(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, functools.partial(self._guarded, fn, *args))

    def _guarded(self, fn: Callable[..., T], *args: Any) -> T:
        try:
            return fn(*args)
        except (xerror.DisplayError, xerror.ConnectionClosedError, ConnectionError, OSError) as exc:
            self._reset()
            raise AgentdError("DISPLAY_UNAVAILABLE", f"X display {self.display}: {exc}") from None
        except mss.exception.ScreenShotError as exc:
            self._reset()
            raise AgentdError("DISPLAY_UNAVAILABLE", f"screen capture on {self.display} failed: {exc}") from None

    def _reset(self) -> None:
        for obj in (self._xd, self._sct):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._xd, self._sct, self._atoms = None, None, {}

    def _dpy(self) -> xdisplay.Display:
        if self._xd is None:
            try:
                self._xd = xdisplay.Display(self.display)
            except (xerror.DisplayError, OSError, ValueError, OverflowError) as exc:
                raise AgentdError("DISPLAY_UNAVAILABLE", f"cannot open X display {self.display}: {exc}") from None
        return self._xd

    def _atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self._dpy().intern_atom(name)
        return self._atoms[name]

    def _root(self) -> Any:
        return self._dpy().screen().root

    def _geometry(self) -> tuple[int, int]:
        g = self._root().get_geometry()
        return int(g.width), int(g.height)

    async def _run(
        self, prog: str, *args: str, timeout: float = 10.0, stdin: bytes | None = None, capture: bool = True
    ) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                prog,
                *args,
                env=self._env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise AgentdError("INTERNAL", f"{prog} is not installed") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except BaseException as exc:
            if proc.returncode is None:
                proc.kill()
                await asyncio.shield(proc.wait())
            if isinstance(exc, TimeoutError):
                raise AgentdError("INTERNAL", f"{prog} {args[0] if args else ''} timed out") from None
            raise
        stdout = (out or b"").decode(errors="replace")
        stderr = (err or b"").decode(errors="replace").strip()
        if proc.returncode != 0 and any(m in stderr for m in DISPLAY_ERRORS):
            raise AgentdError("DISPLAY_UNAVAILABLE", f"X display {self.display}: {stderr}")
        return proc.returncode or 0, stdout, stderr

    async def _xdo(self, *args: str, timeout: float = 10.0) -> str:
        rc, out, err = await self._run("xdotool", *args, timeout=timeout)
        if rc != 0:
            raise AgentdError("INTERNAL", f"xdotool {args[0]} failed: {err or rc}")
        return out

    # ------------------------------------------------------------ queries
    async def screen_size(self) -> tuple[int, int]:
        return await self._x(self._geometry)

    async def capture(self, box: tuple[int, int, int, int] | None = None) -> tuple[Image.Image, tuple[int, int]]:
        return await self._x(self._capture, box)

    def _capture(self, box: tuple[int, int, int, int] | None) -> tuple[Image.Image, tuple[int, int]]:
        for attempt in range(3):
            w, h = self._geometry()
            x0, y0, x1, y1 = box if box else (0, 0, w, h)
            x1, y1 = min(x1, w), min(y1, h)
            if x1 <= x0 or y1 <= y0:
                raise AgentdError("INVALID_ACTION", "region lies outside the screen")
            try:
                if self._sct is None:
                    self._sct = mss.MSS(display=self.display, with_cursor=self.draw_cursor, backend=self._mss_backend)
                shot = self._sct.grab({"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0})
            except mss.exception.ScreenShotError:
                # The screen can shrink between reading its size and grabbing it.
                self._reset()
                if self._mss_backend != "xlib":
                    # mss's XCB backends stop at the first depth-24 entry in the
                    # screen's allowed depths. KasmVNC's Xvnc lists depth 24 twice
                    # and keeps the root visual in the second entry, so every
                    # capture fails there; the Xlib backend handles it.
                    self._mss_backend = "xlib"
                    continue
                if attempt == 2:
                    raise
                time.sleep(0.2)
                continue
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            return img, (w, h)
        raise AssertionError("unreachable")

    async def cursor(self) -> tuple[int, int]:
        def q() -> tuple[int, int]:
            p = self._root().query_pointer()
            return int(p.root_x), int(p.root_y)

        return await self._x(q)

    async def windows(self) -> list[dict[str, Any]]:
        return await self._x(self._windows)

    def _prop(self, win: Any, name: str, typ: int = X.AnyPropertyType) -> Any:
        prop = win.get_full_property(self._atom(name), typ)
        return prop.value if prop is not None else None

    def _windows(self) -> list[dict[str, Any]]:
        d = self._dpy()
        root = self._root()
        ids = self._prop(root, "_NET_CLIENT_LIST")
        active = self._prop(root, "_NET_ACTIVE_WINDOW")
        active_id = int(active[0]) if active is not None and len(active) else None
        ewmh = ids is not None
        if not ewmh:
            # No EWMH window manager: fall back to mapped top-level windows.
            ids = []
            for child in root.query_tree().children:
                try:
                    attrs = child.get_attributes()
                    if attrs.map_state == X.IsViewable and not attrs.override_redirect:
                        ids.append(child.id)
                except xerror.XError:
                    continue
            focus = d.get_input_focus().focus
            active_id = getattr(focus, "id", None)
        out = []
        for wid in ids:
            win = d.create_resource_object("window", int(wid))
            try:
                name = self._prop(win, "_NET_WM_NAME", self._atom("UTF8_STRING"))
                if isinstance(name, bytes):
                    name = name.decode(errors="replace")
                if not name:
                    raw = self._prop(win, "WM_NAME", Xatom.STRING)
                    name = raw.decode("latin-1") if isinstance(raw, bytes) else (raw or "")
                wm_class = win.get_wm_class() or (None, None)
                pid = self._prop(win, "_NET_WM_PID", Xatom.CARDINAL)
                geo = win.get_geometry()
                pos = root.translate_coords(win, 0, 0)
                desktop = self._prop(win, "_NET_WM_DESKTOP", Xatom.CARDINAL)
                state = self._prop(win, "_NET_WM_STATE", Xatom.ATOM) or []
                hidden = self._atom("_NET_WM_STATE_HIDDEN") in list(state)
            except xerror.XError:
                continue  # the window went away while we looked at it
            out.append(
                {
                    "id": int(wid),
                    "title": name or "",
                    "class": wm_class[1] or "",
                    "instance": wm_class[0] or "",
                    "pid": int(pid[0]) if pid is not None and len(pid) else None,
                    "geometry": [int(pos.x), int(pos.y), int(geo.width), int(geo.height)],
                    "active": int(wid) == active_id,
                    "desktop": int(desktop[0]) if desktop is not None and len(desktop) else None,
                    "hidden": hidden,
                }
            )
        return out

    async def activate_window(self, wid: int) -> None:
        known = {w["id"] for w in await self.windows()}
        if wid not in known:
            raise AgentdError("NOT_FOUND", f"no window with id {wid}")
        rc, _, _ = await self._run("xdotool", "windowactivate", "--sync", str(wid), timeout=3)
        if rc != 0:
            # No EWMH window manager: raise and focus directly.
            await self._xdo("windowmap", str(wid))
            await self._xdo("windowraise", str(wid))
            await self._xdo("windowfocus", str(wid))

    # -------------------------------------------------------------- input
    async def move(self, x: int, y: int) -> None:
        if await self.cursor() == (x, y):
            return  # `mousemove --sync` to the current position never returns
        await self._xdo("mousemove", str(x), str(y))
        deadline = time.monotonic() + 1.0
        while await self.cursor() != (x, y):
            if time.monotonic() > deadline:
                raise AgentdError("INTERNAL", f"pointer did not reach {x},{y}")
            await asyncio.sleep(0.005)

    async def motion_path(self, points: list[tuple[int, int]], step_delay: float = 0.01) -> None:
        if not points:
            return
        args: list[str] = []
        for i, (x, y) in enumerate(points):
            if i:
                args += ["sleep", f"{step_delay:.3f}"]
            args += ["mousemove", str(x), str(y)]
        await self._xdo(*args, timeout=10 + len(points) * (step_delay + 0.05))

    async def click(self, button: int, count: int = 1) -> None:
        await self._xdo(
            "click", "--repeat", str(count), "--delay", str(CLICK_DELAY_MS), str(button), timeout=10 + count
        )

    async def button_down(self, button: int) -> None:
        await self._xdo("mousedown", str(button))

    async def button_up(self, button: int) -> None:
        await self._xdo("mouseup", str(button))

    async def key_down(self, keysym: str) -> None:
        await self._xdo("keydown", "--", keysym)

    async def key_up(self, keysym: str) -> None:
        await self._xdo("keyup", "--", keysym)

    async def key_sequence(self, combos: list[str], repeat: int = 1) -> None:
        await self._xdo(
            "key",
            "--delay",
            str(KEY_DELAY_MS),
            "--repeat",
            str(repeat),
            "--repeat-delay",
            str(KEY_DELAY_MS),
            "--",
            *combos,
            timeout=10 + 0.2 * len(combos) * repeat,
        )

    async def type_text(self, text: str) -> None:
        for i in range(0, len(text), TYPE_CHUNK):
            chunk = text[i : i + TYPE_CHUNK]
            await self._xdo("type", "--delay", str(TYPE_DELAY_MS), "--", chunk, timeout=10 + len(chunk) * 0.1)

    # ---------------------------------------------------------- clipboard
    async def clipboard_get(self) -> str:
        for extra in (["-t", "UTF8_STRING"], []):
            rc, out, err = await self._run("xclip", "-selection", "clipboard", "-o", *extra, timeout=5)
            if rc == 0:
                return out
        if "not available" in err or "no owner" in err.lower() or not err:
            return ""
        raise AgentdError("INTERNAL", f"xclip failed: {err}")

    async def clipboard_set(self, text: str) -> None:
        # xclip forks a child that owns the selection until someone else takes
        # it; that child inherits stdout/stderr, so they must not be pipes.
        rc, _, _ = await self._run(
            "xclip", "-selection", "clipboard", "-i", stdin=text.encode(), timeout=5, capture=False
        )
        if rc != 0:
            raise AgentdError("INTERNAL", "xclip could not take the clipboard")

    # --------------------------------------------------------- resolution
    async def set_resolution(self, width: int, height: int) -> tuple[int, int]:
        if await self.screen_size() == (width, height):
            return width, height
        rc, out, err = await self._run("xrandr", "--query", timeout=5)
        if rc != 0:
            raise AgentdError("INTERNAL", f"xrandr --query failed: {err}")
        output = None
        for line in out.splitlines():
            m = re.match(r"^(\S+) connected", line)
            if m:
                output = m.group(1)
                break
        name = f"{width}x{height}"
        attempts: list[list[str]] = [["-s", name]]
        if output:
            if not re.search(rf"^\s+{re.escape(name)}\s", out, re.M):
                # Virtual outputs (Xvnc) accept any timings; these are CVT-like.
                htot, vtot = width + 160, height + 30
                clock = f"{htot * vtot * 60 / 1e6:.2f}"
                attempts.insert(
                    0,
                    [
                        "--newmode",
                        name,
                        clock,
                        str(width),
                        str(width + 48),
                        str(width + 80),
                        str(htot),
                        str(height),
                        str(height + 3),
                        str(height + 6),
                        str(vtot),
                    ],
                )
                attempts.insert(1, ["--addmode", output, name])
            attempts.append(["--output", output, "--mode", name])
        attempts.append(["--fb", name])
        errors = []
        for args in attempts:
            rc, _, err = await self._run("xrandr", *args, timeout=10)
            if rc != 0:
                errors.append(f"xrandr {' '.join(args[:2])}: {err.splitlines()[0] if err else rc}")
            if await self.screen_size() == (width, height):
                return width, height
        raise AgentdError("INVALID_ACTION", f"could not set {name}: " + "; ".join(errors[-3:]))

    async def close(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._pool, self._reset)
        self._pool.shutdown(wait=False)
