"""Backend interface: the display-specific primitives agentd composes.

Everything above this layer (sessions, coordinate spaces, modifier
bookkeeping, leases) is display-agnostic, so a wlroots or uinput backend only
has to implement these methods. Coordinates here are always screen pixels.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from PIL import Image


class Backend(ABC):
    display: str

    @abstractmethod
    async def screen_size(self) -> tuple[int, int]: ...

    @abstractmethod
    async def capture(self, box: tuple[int, int, int, int] | None = None) -> tuple[Image.Image, tuple[int, int]]:
        """Grab the screen (or `box` = x0, y0, x1, y1). Returns (RGB image, screen size)."""

    @abstractmethod
    async def cursor(self) -> tuple[int, int]: ...

    @abstractmethod
    async def move(self, x: int, y: int) -> None: ...

    @abstractmethod
    async def motion_path(self, points: list[tuple[int, int]], step_delay: float = 0.01) -> None:
        """Move through `points` in order (the button state is left alone)."""

    @abstractmethod
    async def click(self, button: int, count: int = 1) -> None: ...

    @abstractmethod
    async def button_down(self, button: int) -> None: ...

    @abstractmethod
    async def button_up(self, button: int) -> None: ...

    @abstractmethod
    async def key_down(self, keysym: str) -> None: ...

    @abstractmethod
    async def key_up(self, keysym: str) -> None: ...

    @abstractmethod
    async def key_sequence(self, combos: list[str], repeat: int = 1) -> None: ...

    @abstractmethod
    async def type_text(self, text: str) -> None: ...

    @abstractmethod
    async def windows(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def activate_window(self, wid: int) -> None: ...

    @abstractmethod
    async def clipboard_get(self) -> str: ...

    @abstractmethod
    async def clipboard_set(self, text: str) -> None: ...

    @abstractmethod
    async def set_resolution(self, width: int, height: int) -> tuple[int, int]: ...

    @abstractmethod
    def subprocess_env(self) -> dict[str, str]:
        """Environment for processes started in the session (DISPLAY etc.)."""

    async def close(self) -> None:  # noqa: B027 - optional
        pass
