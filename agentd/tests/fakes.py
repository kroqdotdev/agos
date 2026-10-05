"""An in-memory Backend for unit tests of the core semantics."""

from __future__ import annotations

from typing import Any

from PIL import Image

from agentd.backend.base import Backend
from agentd.errors import AgentdError


class FakeBackend(Backend):
    def __init__(self, width: int = 1280, height: int = 800) -> None:
        self.display = ":fake"
        self.size = (width, height)
        self.pointer = (0, 0)
        self.calls: list[tuple[Any, ...]] = []
        self.pressed: list[str] = []  # keys currently down
        self.buttons: list[int] = []
        self.fail_on: dict[tuple[Any, ...], int] = {}  # call signature -> remaining failures
        self.color = (40, 40, 40)
        self.clipboard = ""

    def _maybe_fail(self, *sig: Any) -> None:
        self.calls.append(sig)
        for key in (sig, sig[:1]):
            if self.fail_on.get(key):
                self.fail_on[key] -= 1
                raise AgentdError("INTERNAL", f"injected failure in {sig}")

    async def screen_size(self) -> tuple[int, int]:
        return self.size

    async def capture(self, box: tuple[int, int, int, int] | None = None) -> tuple[Image.Image, tuple[int, int]]:
        x0, y0, x1, y1 = box or (0, 0, *self.size)
        return Image.new("RGB", (x1 - x0, y1 - y0), self.color), self.size

    async def cursor(self) -> tuple[int, int]:
        return self.pointer

    async def move(self, x: int, y: int) -> None:
        self._maybe_fail("move", x, y)
        self.pointer = (x, y)

    async def motion_path(self, points: list[tuple[int, int]], step_delay: float = 0.01) -> None:
        self._maybe_fail("motion_path", len(points))
        if points:
            self.pointer = points[-1]

    async def click(self, button: int, count: int = 1) -> None:
        self._maybe_fail("click", button, count)

    async def button_down(self, button: int) -> None:
        self._maybe_fail("button_down", button)
        self.buttons.append(button)

    async def button_up(self, button: int) -> None:
        self._maybe_fail("button_up", button)
        if button in self.buttons:
            self.buttons.remove(button)

    async def key_down(self, keysym: str) -> None:
        self._maybe_fail("key_down", keysym)
        self.pressed.append(keysym)

    async def key_up(self, keysym: str) -> None:
        self._maybe_fail("key_up", keysym)
        if keysym in self.pressed:
            self.pressed.remove(keysym)

    async def key_sequence(self, combos: list[str], repeat: int = 1) -> None:
        self._maybe_fail("key_sequence", tuple(combos), repeat)

    async def type_text(self, text: str) -> None:
        self._maybe_fail("type_text", text)

    async def windows(self) -> list[dict[str, Any]]:
        return []

    async def activate_window(self, wid: int) -> None:
        self._maybe_fail("activate", wid)

    async def clipboard_get(self) -> str:
        return self.clipboard

    async def clipboard_set(self, text: str) -> None:
        self.clipboard = text

    async def set_resolution(self, width: int, height: int) -> tuple[int, int]:
        self.size = (width, height)
        return self.size

    def subprocess_env(self) -> dict[str, str]:
        import os

        return {**os.environ, "DISPLAY": self.display}
