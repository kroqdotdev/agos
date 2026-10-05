"""Best-effort AT-SPI element list via gi.repository.Atspi.

Each node costs several D-Bus round trips, so the walk is scoped to one
window (title filter, else the active window) and bounded by depth, node
count and wall time. Bounds come back in screen pixels and are converted into
the session's coordinate space by `convert`, which is pure and unit-tested.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from agentd.errors import AgentdError
from agentd.geometry import Frame, box_from_screen

TIME_BUDGET = 8.0
_pool = ThreadPoolExecutor(1, thread_name_prefix="agentd-a11y")
_atspi: Any = None


@dataclass
class Node:
    role: str
    name: str
    states: list[str]
    bbox: tuple[int, int, int, int] | None  # screen pixels, x1/y1 exclusive
    depth: int
    parent: int | None
    extra: dict[str, Any] = field(default_factory=dict)


def load() -> Any:
    global _atspi
    if _atspi is not None:
        return _atspi
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        raise AgentdError(
            "A11Y_UNAVAILABLE",
            f"AT-SPI is unavailable ({exc}); it needs python3-gi and gir1.2-atspi-2.0 visible to "
            "agentd's Python (the image venv uses --system-site-packages)",
        ) from None
    Atspi.init()
    _atspi = Atspi
    return Atspi


def _states(Atspi: Any, acc: Any) -> list[str]:
    try:
        return sorted(Atspi.StateType(s).value_nick for s in acc.get_state_set().get_states())
    except Exception:
        return []


def _bbox(Atspi: Any, acc: Any) -> tuple[int, int, int, int] | None:
    try:
        r = acc.get_extents(Atspi.CoordType.SCREEN)
    except Exception:
        return None
    if r is None or r.width <= 0 or r.height <= 0:
        return None
    return (int(r.x), int(r.y), int(r.x + r.width), int(r.y + r.height))


TEXT_ROLES = {"text", "entry", "terminal", "document text", "paragraph"}
TEXT_LIMIT = 200


def _text(Atspi: Any, acc: Any, role: str, states: list[str]) -> str | None:
    """Current text of editable fields (never of password fields)."""
    if role not in TEXT_ROLES or "editable" not in states:
        return None
    try:
        iface = acc.get_text_iface()
        if iface is None:
            return None
        # Unbound calls: iface.get_text() resolves to the deprecated Accessible.get_text.
        count = Atspi.Text.get_character_count(iface)
        value = Atspi.Text.get_text(iface, 0, min(count, TEXT_LIMIT))
        return value if isinstance(value, str) else None
    except Exception:
        return None


def collect(
    window: str | None, max_depth: int, max_nodes: int, visible_only: bool = True
) -> tuple[list[Node], dict[str, Any]]:
    """Walk one window's subtree. Runs on the a11y worker thread."""
    Atspi = load()
    desktop = Atspi.get_desktop(0)
    wanted = window.lower() if window else None
    target = app_name = None
    seen: list[str] = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        for j in range(app.get_child_count()):
            win = app.get_child_at_index(j)
            if win is None:
                continue
            title = win.get_name() or ""
            seen.append(title)
            if wanted is not None:
                hit = wanted in title.lower() or wanted in (app.get_name() or "").lower()
            else:
                states = _states(Atspi, win)
                hit = "active" in states
            if hit:
                target, app_name = win, app.get_name()
                break
        if target is not None:
            break
    if target is None:
        what = f"matching {window!r}" if window else "that is active"
        raise AgentdError("NOT_FOUND", f"no accessible window {what}; windows: {seen[:20]}")

    nodes: list[Node] = []
    truncated = False
    deadline = time.monotonic() + TIME_BUDGET
    stack: list[tuple[Any, int, int | None]] = [(target, 0, None)]
    while stack:
        if len(nodes) >= max_nodes or time.monotonic() > deadline:
            truncated = True
            break
        acc, depth, parent = stack.pop()
        try:
            states = _states(Atspi, acc)
            if visible_only and depth > 0 and "showing" not in states:
                continue
            role = acc.get_role_name() or ""
            node = Node(role, acc.get_name() or "", states, _bbox(Atspi, acc), depth, parent)
            text = _text(Atspi, acc, role, states)
            if text is not None:
                node.extra["text"] = text
            nodes.append(node)
            idx = len(nodes) - 1
            if depth >= max_depth:
                truncated = truncated or acc.get_child_count() > 0
                continue
            count = acc.get_child_count()
            for k in range(count - 1, -1, -1):  # reversed so children pop in order
                child = acc.get_child_at_index(k)
                if child is not None:
                    stack.append((child, depth + 1, idx))
        except Exception:
            continue  # objects disappear while we walk; skip them
    return nodes, {"window": target.get_name() or "", "app": app_name or "", "truncated": truncated}


def convert(nodes: list[Node], space: str, screen: tuple[int, int], frame: Frame | None) -> list[dict[str, Any]]:
    out = []
    for i, n in enumerate(nodes):
        item: dict[str, Any] = {
            "id": i,
            "parent": n.parent,
            "depth": n.depth,
            "role": n.role,
            "name": n.name,
            "states": n.states,
            **n.extra,
        }
        if n.bbox is not None:
            item["bbox"] = box_from_screen(n.bbox, space, screen, frame)
            item["bbox_screen"] = list(n.bbox)
        else:
            item["bbox"] = None
        out.append(item)
    return out


async def tree(window: str | None, max_depth: int, max_nodes: int) -> tuple[list[Node], dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool, collect, window, max_depth, max_nodes)
