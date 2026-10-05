"""Display-agnostic core: sessions, frames, the stale-frame guard, the input
lock, the takeover lease and batch execution of canonical actions."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import math
import re
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

from agentd import procs
from agentd.actions import BUTTONS, Action
from agentd.audit import AuditLog, now_iso, sanitize_action
from agentd.auth import Principal
from agentd.backend.base import Backend
from agentd.config import Config
from agentd.errors import AgentdError
from agentd.geometry import Frame, fit_size, fit_within, from_screen, region_to_screen, to_screen
from agentd.imaging import MIME, changed_fraction, encode, resize, thumbnail

SESSION_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,96}$")
MAX_SESSIONS = 256
STABLE_POLL = 0.05


@dataclass
class Session:
    id: str
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    frames: deque[Frame] = field(default_factory=lambda: deque(maxlen=32))

    @property
    def last_frame(self) -> Frame | None:
        return self.frames[-1] if self.frames else None

    def find(self, frame_id: int) -> Frame | None:
        for f in self.frames:
            if f.frame_id == frame_id:
                return f
        return None


@dataclass
class Lease:
    by: str
    reason: str
    since: str
    principal: str
    expires_at: float | None = None
    id: int = 0

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "held": True,
            "by": self.by,
            "reason": self.reason,
            "since": self.since,
            "principal": self.principal,
        }
        if self.expires_at is not None:
            out["expires_in"] = max(0.0, round(self.expires_at - time.monotonic(), 1))
        return out


@dataclass
class Shot:
    meta: dict[str, Any]
    data: bytes
    mime: str

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode()

    def to_json(self) -> dict[str, Any]:
        return {**self.meta, "mime_type": self.mime, "data": self.b64}


@dataclass
class StepResult:
    index: int
    type: str
    ok: bool
    error: AgentdError | None = None
    data: dict[str, Any] = field(default_factory=dict)
    shot: Shot | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"index": self.index, "type": self.type, "ok": self.ok, **self.data}
        if self.error is not None:
            out["error"] = {"code": self.error.code, "message": self.error.message}
        if self.shot is not None:
            out["screenshot"] = self.shot.to_json()
        return out


@dataclass
class BatchResult:
    session: str
    results: list[StepResult]
    error: AgentdError | None = None
    skipped: list[int] = field(default_factory=list)
    screenshot: Shot | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session": self.session,
            "ok": self.ok,
            "results": [r.to_json() for r in self.results],
            "skipped": self.skipped,
        }
        if self.error is not None:
            out["error"] = {"code": self.error.code, "message": self.error.message}
        if self.screenshot is not None:
            out["screenshot"] = self.screenshot.to_json()
        return out


def _stale(message: str) -> AgentdError:
    # Callers attach a fresh screenshot to the error; it becomes the new basis.
    return AgentdError("STALE_FRAME", message)


def interpolate(points: list[tuple[int, int]], step_px: float = 15.0, max_steps: int = 25) -> list[tuple[int, int]]:
    """Intermediate points so drag targets see real motion, not a teleport."""
    out = [points[0]]
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        n = max(1, min(max_steps, int(math.hypot(x1 - x0, y1 - y0) / step_px)))
        for i in range(1, n + 1):
            out.append((round(x0 + (x1 - x0) * i / n), round(y0 + (y1 - y0) * i / n)))
    return out


class Core:
    def __init__(self, config: Config, backend: Backend, audit: AuditLog) -> None:
        self.config = config
        self.backend = backend
        self.audit = audit
        self.sessions: dict[str, Session] = {}
        self.frame_seq = 0
        self.epoch = 1
        self.epoch_first_frame = 1  # frame ids below this predate the current epoch
        self.lease: Lease | None = None
        self.input_lock = asyncio.Lock()
        self._lease_taken = asyncio.Event()
        self._lease_seq = 0
        self._lease_timer: asyncio.TimerHandle | None = None
        self.held_keys: list[str] = []
        self.held_buttons: list[int] = []
        self.started = time.time()
        self._tasks: set[asyncio.Task[Any]] = set()

    # ----------------------------------------------------------- sessions
    def session(self, sid: str | None) -> Session:
        sid = sid or "default"
        if not SESSION_RE.match(sid):
            raise AgentdError("INVALID_ACTION", "session ids are 1-96 chars of [A-Za-z0-9_.:@-]")
        s = self.sessions.get(sid)
        if s is None:
            if len(self.sessions) >= MAX_SESSIONS:
                victim = min(
                    (x for x in self.sessions.values() if x.id != "default"), key=lambda x: x.last_used, default=None
                )
                if victim is not None:
                    del self.sessions[victim.id]
            s = self.sessions[sid] = Session(sid)
        s.last_used = time.time()
        return s

    def new_session(self) -> str:
        sid = "sess_" + secrets.token_hex(6)
        self.session(sid)
        return sid

    # ------------------------------------------------------- observation
    async def screenshot(self, sid: str | None, fmt: str | None = None) -> Shot:
        session = self.session(sid)
        fmt = fmt or self.config.default_format
        img, screen = await self.backend.capture()
        size = fit_size(*screen, self.config.max_image_long_edge, self.config.max_image_pixels)
        data = await asyncio.to_thread(self._encode, img, size, fmt)
        cursor = await self.backend.cursor()
        self.frame_seq += 1
        frame = Frame(self.frame_seq, self.epoch, screen, size)
        session.frames.append(frame)
        meta = {
            "session": session.id,
            "frame_id": frame.frame_id,
            "epoch": frame.epoch,
            "screen": list(screen),
            "image": list(size),
            "scale": round(frame.scale, 6),
            "coord_space": "image",
            "cursor": list(from_screen(*cursor, "image", screen, frame)),
            "format": fmt,
        }
        return Shot(meta, data, MIME[fmt])

    def _encode(self, img: Any, size: tuple[int, int], fmt: str) -> bytes:
        return encode(resize(img, size), fmt, self.config.jpeg_quality, self.config.webp_quality)

    async def zoom(
        self,
        session: Session,
        region: list[float],
        space: str,
        frame: Frame | None,
        screen: tuple[int, int],
        fmt: str | None = None,
    ) -> Shot:
        fmt = fmt or self.config.default_format
        try:
            box = region_to_screen(region, space, screen, frame)
        except ValueError as exc:
            raise AgentdError("INVALID_ACTION", f"zoom: {exc}") from None
        img, _ = await self.backend.capture(box)
        # Fit the crop into the usual screenshot size (upscaling small regions),
        # as Anthropic's zoom does; coordinates stay in full-screenshot space.
        target = (
            frame.image if frame else fit_size(*screen, self.config.max_image_long_edge, self.config.max_image_pixels)
        )
        size = fit_within(img.width, img.height, *target)
        size = fit_size(*size, self.config.max_image_long_edge, self.config.max_image_pixels)
        data = await asyncio.to_thread(self._encode, img, size, fmt)
        meta = {
            "session": session.id,
            "frame_id": frame.frame_id if frame else None,
            "epoch": self.epoch,
            "zoom": True,
            "region": region,
            "region_screen": list(box),
            "coord_space": space,
            "screen": list(screen),
            "image": list(size),
            "format": fmt,
        }
        return Shot(meta, data, MIME[fmt])

    async def wait_for_stable(self, timeout: float | None = None, settle_ms: int | None = None) -> dict[str, Any]:
        timeout = self.config.settle_timeout_ms / 1000 if timeout is None else timeout
        settle = (self.config.settle_ms if settle_ms is None else settle_ms) / 1000
        start = time.monotonic()
        img, _ = await self.backend.capture()
        prev = await asyncio.to_thread(thumbnail, img)
        quiet_since = start
        while True:
            now = time.monotonic()
            if now - quiet_since >= settle:
                return {"stable": True, "waited_ms": round((now - start) * 1000)}
            if now - start >= timeout:
                return {"stable": False, "waited_ms": round((now - start) * 1000)}
            await asyncio.sleep(min(STABLE_POLL, max(0.0, timeout - (now - start))))
            img, _ = await self.backend.capture()
            cur = await asyncio.to_thread(thumbnail, img)
            if changed_fraction(prev, cur) > self.config.stable_change_fraction:
                quiet_since = time.monotonic()
            prev = cur

    def status(self) -> dict[str, Any]:
        return {
            "display": self.backend.display,
            "epoch": self.epoch,
            "frame_id": self.frame_seq,
            "lease": self.lease_json(),
            "input_busy": self.input_lock.locked(),
            "sessions": [
                {
                    "id": s.id,
                    "frame_id": s.last_frame.frame_id if s.last_frame else None,
                    "last_used": round(s.last_used, 3),
                }
                for s in sorted(self.sessions.values(), key=lambda s: -s.last_used)[:50]
            ],
            "session_count": len(self.sessions),
            "held": {"keys": list(self.held_keys), "buttons": list(self.held_buttons)},
            "uptime_s": round(time.time() - self.started),
        }

    # ------------------------------------------------------------- lease
    def lease_json(self) -> dict[str, Any]:
        return self.lease.to_json() if self.lease else {"held": False}

    def _check_lease(self) -> None:
        if self.lease is not None:
            lease = self.lease
            raise AgentdError(
                "HUMAN_IN_CONTROL",
                f"{lease.by} took over at {lease.since}"
                + (f" ({lease.reason})" if lease.reason else "")
                + "; input is paused until they hand back, observation still works",
            )

    async def takeover(
        self, by: str, reason: str = "", ttl: float | None = None, principal: Principal | None = None
    ) -> dict[str, Any]:
        if self.lease is None:
            self._lease_seq += 1
            self.lease = Lease(
                by=by or "human",
                reason=reason or "",
                since=now_iso(),
                principal=principal.name if principal else "",
                id=self._lease_seq,
            )
            self._lease_taken.set()
            await asyncio.shield(self._release_held())
            self.audit.write(
                "lease",
                change="takeover",
                by=self.lease.by,
                reason=self.lease.reason,
                principal=self.lease.principal,
                ttl=ttl,
            )
            await self._run_hook("on_takeover", self.config.on_takeover)
        else:
            self.audit.write("lease", change="renew", by=by, principal=principal.name if principal else None, ttl=ttl)
        if self._lease_timer is not None:
            self._lease_timer.cancel()
            self._lease_timer = None
        if ttl:
            self.lease.expires_at = time.monotonic() + ttl
            lease_id = self.lease.id
            self._lease_timer = asyncio.get_running_loop().call_later(ttl, self._spawn_expire, lease_id)
        else:
            self.lease.expires_at = None
        return self.lease_json()

    def _spawn_expire(self, lease_id: int) -> None:
        # Keep a reference: the loop only holds tasks weakly.
        task = asyncio.ensure_future(self._expire(lease_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _expire(self, lease_id: int) -> None:
        if self.lease is not None and self.lease.id == lease_id:
            await self.handback("lease-expired")

    async def handback(self, by: str = "", principal: Principal | None = None) -> dict[str, Any]:
        if self.lease is None:
            return {"held": False, "epoch": self.epoch, "changed": False}
        previous = self.lease
        self.lease = None
        self._lease_taken.clear()
        if self._lease_timer is not None:
            self._lease_timer.cancel()
            self._lease_timer = None
        # Everything observed before or during the takeover is now stale.
        self.epoch += 1
        self.epoch_first_frame = self.frame_seq + 1
        self.audit.write(
            "lease",
            change="handback",
            by=by or previous.by,
            principal=principal.name if principal else None,
            epoch=self.epoch,
        )
        await self._run_hook("on_handback", self.config.on_handback)
        return {"held": False, "epoch": self.epoch, "changed": True}

    async def _run_hook(self, name: str, argv: list[str]) -> None:
        if not argv:
            return
        env = self.backend.subprocess_env()
        if self.lease is not None:
            env.update(AGENTD_LEASE_BY=self.lease.by, AGENTD_LEASE_REASON=self.lease.reason)
        env["AGENTD_EVENT"] = name
        try:
            res = await procs.run(list(argv), env, timeout=15)
            self.audit.write(
                "hook",
                hook=name,
                argv=argv,
                exit_code=res["exit_code"],
                timed_out=res["timed_out"],
                stderr=res["stderr"][-300:] or None,
            )
        except AgentdError as exc:
            self.audit.write("hook", hook=name, argv=argv, error=exc.message)

    async def _release_held(self) -> None:
        keys, buttons = list(self.held_keys), list(self.held_buttons)
        self.held_keys.clear()
        self.held_buttons.clear()
        await self._release_keys(reversed(keys))
        for b in buttons:
            with contextlib.suppress(Exception):
                await self.backend.button_up(b)

    async def _release_keys(self, keys: Iterable[str]) -> None:
        for k in keys:
            try:
                await self.backend.key_up(k)
            except Exception as exc:  # keep releasing the rest
                self.audit.write("error", where="key_up", key=k, message=str(exc))

    async def shutdown(self) -> None:
        await self._release_held()

    # ---------------------------------------------------------- execution
    async def execute(
        self,
        sid: str | None,
        actions: list[Action],
        principal: Principal,
        screenshot_after: bool = True,
        fmt: str | None = None,
        source: str = "actions",
        take_lock: bool = True,
    ) -> BatchResult:
        """Run `actions` in order, stopping at the first failure.

        `take_lock=False` means the caller already holds `input_lock` (adapters
        that run several provider calls as one batch).
        """
        session = self.session(sid)
        result = BatchResult(session.id, [])
        needs_lock = take_lock and any(a.is_input for a in actions)
        lock = self.input_lock if needs_lock else contextlib.nullcontext()
        async with lock:
            for i, action in enumerate(actions):
                t0 = time.monotonic()
                try:
                    step = await self._step(session, i, action)
                except AgentdError as exc:
                    err = exc
                except Exception as exc:  # unexpected: report, never leave the batch half-silent
                    err = AgentdError("INTERNAL", f"{action.type}: {exc}")
                else:
                    result.results.append(step)
                    self._audit_action(principal, session, action, "ok", t0, source)
                    continue
                if err.code == "STALE_FRAME":
                    with contextlib.suppress(AgentdError):
                        result.screenshot = await self.screenshot(session.id, fmt)
                result.results.append(StepResult(i, action.type, False, error=err))
                result.error = err
                result.skipped = list(range(i + 1, len(actions)))
                self._audit_action(principal, session, action, err.code, t0, source, err.message)
                break
            did_input = any(r.ok and actions[r.index].is_input for r in result.results)
            if (
                screenshot_after
                and result.screenshot is None
                and (result.error is None or result.error.code not in ("DISPLAY_UNAVAILABLE",))
            ):
                try:
                    if did_input:
                        await self.wait_for_stable()
                    result.screenshot = await self.screenshot(session.id, fmt)
                except AgentdError as exc:
                    if result.error is None:
                        result.error = exc
        return result

    def _audit_action(
        self,
        principal: Principal,
        session: Session,
        action: Action,
        code: str,
        t0: float,
        source: str,
        message: str | None = None,
    ) -> None:
        self.audit.write(
            "action",
            principal=principal.name,
            transport=principal.transport,
            session=session.id,
            source=source,
            action=sanitize_action(action.raw, self.config.audit_text),
            result=code,
            message=message,
            ms=round((time.monotonic() - t0) * 1000),
            epoch=self.epoch,
        )

    def _check_stale(
        self, session: Session, action: Action, screen: tuple[int, int], check_epoch: bool
    ) -> Frame | None:
        frame = session.last_frame
        efid = action.expect_frame_id
        if efid is not None:
            if efid < self.epoch_first_frame:
                raise _stale(f"frame {efid} predates epoch {self.epoch} (a human had control since)")
            found = session.find(efid)
            if found is None:
                raise _stale(f"frame {efid} is not a recent screenshot of session {session.id!r}")
            frame = found
        elif check_epoch and frame is not None and frame.epoch != self.epoch:
            raise _stale("a human had control since your last screenshot")
        if action.uses_coords and action.coord_space in ("image", "normalized"):
            if frame is None:
                raise _stale("no screenshot has been taken in this session yet")
            if frame.screen != screen:
                raise _stale(
                    f"the screen changed from {frame.screen[0]}x{frame.screen[1]} to "
                    f"{screen[0]}x{screen[1]} since your last screenshot"
                )
        return frame

    def _pt(self, action: Action, x: float, y: float, screen: tuple[int, int], frame: Frame | None) -> tuple[int, int]:
        try:
            return to_screen(x, y, action.coord_space, screen, frame)
        except ValueError as exc:
            raise _stale(str(exc)) from None

    @contextlib.asynccontextmanager
    async def _modifiers(self, mods: list[str]) -> AsyncIterator[None]:
        pressed: list[str] = []
        try:
            for m in mods:
                # Recorded before the keydown: if it half-fails we still release it.
                pressed.append(m)
                await self.backend.key_down(m)
            yield
        finally:
            await asyncio.shield(self._release_keys(reversed(pressed)))

    async def _unless_taken_over(self, keys: list[str] | None = None, buttons: list[int] | None = None) -> None:
        """A takeover that landed while these keys/buttons went down already
        released the held set; release them so nothing stays pressed under the human."""
        if self.lease is None:
            return
        for k in keys or []:
            if k not in self.held_keys:
                self.held_keys.append(k)
        for b in buttons or []:
            if b not in self.held_buttons:
                self.held_buttons.append(b)
        await asyncio.shield(self._release_held())
        self._check_lease()

    async def _sleep_or_takeover(self, seconds: float) -> bool:
        """Sleep; return False early if a human takes over meanwhile."""
        try:
            await asyncio.wait_for(self._lease_taken.wait(), seconds)
            return False
        except TimeoutError:
            return True

    async def _step(self, session: Session, index: int, a: Action) -> StepResult:
        p = a.params
        res = StepResult(index, a.type, True)
        if a.is_input:
            self._check_lease()
        needs_screen = a.is_input or a.type in ("zoom", "cursor_position")
        screen = await self.backend.screen_size() if needs_screen else (0, 0)
        frame: Frame | None = None
        if a.is_input or a.type == "zoom":
            frame = self._check_stale(session, a, screen, check_epoch=a.is_input)

        t = a.type
        if t == "screenshot":
            res.shot = await self.screenshot(session.id, p.get("format"))
        elif t == "zoom":
            res.shot = await self.zoom(session, p["region"], a.coord_space, frame, screen, p.get("format"))
        elif t == "cursor_position":
            cx, cy = await self.backend.cursor()
            basis = session.last_frame
            if a.coord_space == "image" and (basis is None or basis.screen != screen):
                # No usable basis yet: report in the space a fresh screenshot would use.
                basis = Frame(
                    0,
                    self.epoch,
                    screen,
                    fit_size(*screen, self.config.max_image_long_edge, self.config.max_image_pixels),
                )
                res.data["note"] = (
                    "no screenshot of the current screen yet; position is in the space of the next screenshot"
                )
            x, y = from_screen(cx, cy, a.coord_space, screen, basis)
            res.data.update(x=x, y=y, coord_space=a.coord_space, screen_xy=[cx, cy])
        elif t == "wait":
            await asyncio.sleep(p["duration"])
        elif t == "wait_for_stable":
            res.data.update(await self.wait_for_stable(p.get("timeout"), p.get("settle_ms")))
        elif t == "click":
            if "x" in p:
                sx, sy = self._pt(a, p["x"], p["y"], screen, frame)
                await self.backend.move(sx, sy)
                res.data["screen_xy"] = [sx, sy]
            async with self._modifiers(p["modifiers"]):
                await self.backend.click(BUTTONS[p["button"]], p["count"])
        elif t == "move":
            sx, sy = self._pt(a, p["x"], p["y"], screen, frame)
            async with self._modifiers(p["modifiers"]):
                await self.backend.move(sx, sy)
            res.data["screen_xy"] = [sx, sy]
        elif t in ("mouse_down", "mouse_up"):
            if "x" in p:
                sx, sy = self._pt(a, p["x"], p["y"], screen, frame)
                await self.backend.move(sx, sy)
            b = BUTTONS[p["button"]]
            if t == "mouse_down":
                self.held_buttons.append(b)
                await self.backend.button_down(b)
                await self._unless_taken_over(buttons=[b])
            else:
                if b in self.held_buttons:
                    self.held_buttons.remove(b)
                await self.backend.button_up(b)
        elif t == "drag":
            pts = [self._pt(a, x, y, screen, frame) for x, y in p["path"]]
            if len(pts) == 1:  # drag from wherever the pointer is
                pts.insert(0, await self.backend.cursor())
            b = BUTTONS[p["button"]]
            await self.backend.move(*pts[0])
            async with self._modifiers(p["modifiers"]):
                down = False
                try:
                    down = True
                    await self.backend.button_down(b)
                    await asyncio.sleep(0.05)
                    await self.backend.motion_path(interpolate(pts)[1:], 0.008)
                    await asyncio.sleep(0.05)
                finally:
                    if down:
                        await asyncio.shield(self.backend.button_up(b))
            res.data["screen_path"] = [list(pts[0]), list(pts[-1])]
        elif t == "scroll":
            if "x" in p:
                sx, sy = self._pt(a, p["x"], p["y"], screen, frame)
                await self.backend.move(sx, sy)
            async with self._modifiers(p["modifiers"]):
                if p["dy"]:
                    await self.backend.click(5 if p["dy"] > 0 else 4, abs(p["dy"]))
                if p["dx"]:
                    await self.backend.click(7 if p["dx"] > 0 else 6, abs(p["dx"]))
        elif t == "type":
            text = p["text"]
            for i in range(0, len(text), 50):
                self._check_lease()  # a takeover stops long typing between chunks
                await self.backend.type_text(text[i : i + 50])
        elif t == "key":
            await self.backend.key_sequence(p["keys"], p["repeat"])
        elif t == "key_down":
            for k in p["keys"]:
                self.held_keys.append(k)
                await self.backend.key_down(k)
            await self._unless_taken_over(keys=p["keys"])
        elif t == "key_up":
            for k in reversed(p["keys"]):
                if k in self.held_keys:
                    self.held_keys.remove(k)
                await self.backend.key_up(k)
        elif t == "hold_key":
            async with self._modifiers(p["keys"]):
                completed = await self._sleep_or_takeover(p["duration"])
            if not completed:
                self._check_lease()
        else:  # pragma: no cover - parse_action rejects unknown types
            raise AgentdError("INVALID_ACTION", f"unknown action {t}")
        return res
