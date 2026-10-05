"""Canonical action schema: parsing and validation (no I/O).

See docs/spec.md "Canonical actions". Every action may carry `coord_space`
("image" | "screen" | "normalized") and `expect_frame_id`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from agentd.errors import AgentdError, invalid
from agentd.geometry import COORD_SPACES
from agentd.keys import normalize_combo, normalize_modifiers, normalize_sequence

BUTTONS = {"left": 1, "middle": 2, "right": 3, "back": 8, "forward": 9}
BUTTON_ALIASES = {"wheel": "middle", "primary": "left", "secondary": "right"}

INPUT_TYPES = frozenset(
    {
        "click",
        "move",
        "mouse_down",
        "mouse_up",
        "drag",
        "scroll",
        "type",
        "key",
        "key_down",
        "key_up",
        "hold_key",
    }
)
OBSERVE_TYPES = frozenset({"screenshot", "zoom", "cursor_position", "wait", "wait_for_stable"})
ALL_TYPES = INPUT_TYPES | OBSERVE_TYPES

MAX_DURATION = 300.0  # seconds, the computer_toolset_20260801 limit for wait/hold_key
MAX_TEXT = 100_000
MAX_PATH = 1000
FORMATS = ("png", "jpeg", "webp")


@dataclass
class Action:
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    coord_space: str = "image"
    expect_frame_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_input(self) -> bool:
        return self.type in INPUT_TYPES

    @property
    def uses_coords(self) -> bool:
        return "x" in self.params or "path" in self.params or "region" in self.params


def _num(
    raw: dict[str, Any],
    key: str,
    *,
    required: bool = True,
    default: Any = None,
    lo: float | None = None,
    hi: float | None = None,
    integer: bool = False,
) -> Any:
    if key not in raw or raw[key] is None:
        if required:
            raise invalid(f"{raw.get('type')}: missing {key!r}")
        return default
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise invalid(f"{raw.get('type')}: {key!r} must be a number")
    if integer:
        if v != int(v):
            raise invalid(f"{raw.get('type')}: {key!r} must be an integer")
        v = int(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise invalid(f"{raw.get('type')}: {key!r} must be between {lo} and {hi}")
    return v


def _point(p: Any, what: str) -> tuple[float, float]:
    if isinstance(p, dict):
        p = [p.get("x"), p.get("y")]
    if (
        not isinstance(p, (list, tuple))
        or len(p) != 2
        or not all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) for c in p)
    ):
        raise invalid(f"{what} must be [x, y]")
    return float(p[0]), float(p[1])


def _xy(raw: dict[str, Any], params: dict[str, Any], required: bool) -> None:
    has = raw.get("x") is not None or raw.get("y") is not None
    if required or has:
        params["x"] = _num(raw, "x")
        params["y"] = _num(raw, "y")


def _button(raw: dict[str, Any]) -> str:
    b = raw.get("button", "left")
    if isinstance(b, int) and not isinstance(b, bool):
        for name, num in BUTTONS.items():
            if num == b:
                return name
    if isinstance(b, str):
        b = BUTTON_ALIASES.get(b.lower(), b.lower())
        if b in BUTTONS:
            return b
    raise invalid(f"{raw.get('type')}: button must be one of {', '.join(BUTTONS)}")


def parse_action(raw: Any) -> Action:
    if not isinstance(raw, dict):
        raise invalid("each action must be a JSON object")
    t = raw.get("type")
    if t not in ALL_TYPES:
        raise invalid(f"unknown action type {t!r}")
    space = raw.get("coord_space", "image")
    if space not in COORD_SPACES:
        raise invalid(f"coord_space must be one of {', '.join(COORD_SPACES)}")
    efid = raw.get("expect_frame_id")
    if efid is not None and (isinstance(efid, bool) or not isinstance(efid, int)):
        raise invalid("expect_frame_id must be an integer")

    p: dict[str, Any] = {}
    if t in ("screenshot", "zoom"):
        fmt = raw.get("format")
        if fmt is not None:
            if fmt not in FORMATS:
                raise invalid(f"format must be one of {', '.join(FORMATS)}")
            p["format"] = fmt
        if t == "zoom":
            region = raw.get("region")
            if (
                not isinstance(region, (list, tuple))
                or len(region) != 4
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in region)
            ):
                raise invalid("zoom: region must be [x0, y0, x1, y1]")
            if region[2] <= region[0] or region[3] <= region[1]:
                raise invalid("zoom: region must have x1 > x0 and y1 > y0")
            p["region"] = [float(c) for c in region]
    elif t == "click":
        _xy(raw, p, required=False)
        p["button"] = _button(raw)
        p["count"] = _num(raw, "count", required=False, default=1, lo=1, hi=3, integer=True)
        p["modifiers"] = normalize_modifiers(raw.get("modifiers"))
    elif t == "move":
        _xy(raw, p, required=True)
        p["modifiers"] = normalize_modifiers(raw.get("modifiers"))
    elif t in ("mouse_down", "mouse_up"):
        _xy(raw, p, required=False)
        p["button"] = _button(raw)
    elif t == "drag":
        path = raw.get("path")
        if not isinstance(path, list) or len(path) < 1:
            raise invalid("drag: path must hold [x, y] points (one point drags from the cursor)")
        if len(path) > MAX_PATH:
            raise invalid(f"drag: path has more than {MAX_PATH} points")
        p["path"] = [_point(pt, "drag: path point") for pt in path]
        p["button"] = _button(raw)
        p["modifiers"] = normalize_modifiers(raw.get("modifiers"))
    elif t == "scroll":
        _xy(raw, p, required=False)
        p["dx"] = _num(raw, "dx", required=False, default=0, lo=-100, hi=100, integer=True)
        p["dy"] = _num(raw, "dy", required=False, default=0, lo=-100, hi=100, integer=True)
        if p["dx"] == 0 and p["dy"] == 0:
            raise invalid("scroll: dx or dy must be non-zero (wheel clicks; dy > 0 scrolls down)")
        p["modifiers"] = normalize_modifiers(raw.get("modifiers"))
    elif t == "type":
        text = raw.get("text")
        if not isinstance(text, str) or text == "":
            raise invalid("type: text must be a non-empty string")
        if len(text) > MAX_TEXT:
            raise invalid(f"type: text longer than {MAX_TEXT} characters; use the clipboard")
        p["text"] = text
    elif t == "key":
        p["keys"] = normalize_sequence(_keys(raw))
        p["repeat"] = _num(raw, "repeat", required=False, default=1, lo=1, hi=100, integer=True)
    elif t in ("key_down", "key_up", "hold_key"):
        p["keys"] = normalize_combo(_keys(raw)).split("+")
        if t == "hold_key":
            p["duration"] = float(_num(raw, "duration", lo=0, hi=MAX_DURATION))
    elif t == "wait":
        p["duration"] = float(_num(raw, "duration", required=False, default=1.0, lo=0, hi=MAX_DURATION))
    elif t == "wait_for_stable":
        p["timeout"] = _num(raw, "timeout", required=False, default=None, lo=0, hi=MAX_DURATION)
        p["settle_ms"] = _num(raw, "settle_ms", required=False, default=None, lo=0, hi=60_000, integer=True)
    return Action(type=t, params=p, coord_space=space, expect_frame_id=efid, raw=dict(raw))


def _keys(raw: dict[str, Any]) -> str:
    keys = raw.get("keys")
    if isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys):
        return "+".join(keys)
    if not isinstance(keys, str) or (not keys.strip() and keys != " "):
        raise invalid(f"{raw.get('type')}: keys must be a non-empty string like 'ctrl+l'")
    return keys


def parse_batch(raw: Any) -> list[Action]:
    if not isinstance(raw, list) or not raw:
        raise invalid("actions must be a non-empty list")
    if len(raw) > 200:
        raise invalid("at most 200 actions per batch")
    out = []
    for i, item in enumerate(raw):
        try:
            out.append(parse_action(item))
        except AgentdError as exc:
            raise invalid(f"action {i}: {exc.message}") from None
    return out
