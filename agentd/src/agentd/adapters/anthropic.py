"""Anthropic computer use: `computer_20250124` / `computer_20251124` (one
`computer` tool with an `action` field) and `computer_toolset_20260801`
(17 member tools named after the action, `toolset_name: "computer"`).

Coordinates are screenshot pixels (coord_space "image").
"""

from __future__ import annotations

from typing import Any

from agentd.adapters import Group, Outcome, coord
from agentd.core import Shot
from agentd.errors import AgentdError, invalid

TOOLSET_NAME = "computer"
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."

CLICKS = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}
ACTIONS = (
    "screenshot",
    "zoom",
    *CLICKS,
    "left_click_drag",
    "mouse_move",
    "left_mouse_down",
    "left_mouse_up",
    "cursor_position",
    "scroll",
    "type",
    "key",
    "hold_key",
    "wait",
)
SCROLL = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def to_canonical(action: str, inp: dict[str, Any]) -> list[dict[str, Any]]:
    """One Anthropic action (old `action` value or toolset member name) -> canonical actions."""
    if action not in ACTIONS:
        raise invalid(f"unsupported computer action {action!r}")
    text = inp.get("text")
    out: dict[str, Any]
    if action == "screenshot":
        out = {"type": "screenshot"}
    elif action == "zoom":
        out = {"type": "zoom", "region": inp.get("region")}
    elif action in CLICKS:
        button, count = CLICKS[action]
        out = {"type": "click", "button": button, "count": count}
        if inp.get("coordinate") is not None:
            out["x"], out["y"] = coord(inp["coordinate"])
        # computer_20250124 clients sometimes send click modifiers as `key`.
        mods = text if text is not None else inp.get("key")
        if mods:
            out["modifiers"] = mods
    elif action == "mouse_move":
        x, y = coord(inp.get("coordinate"))
        out = {"type": "move", "x": x, "y": y}
    elif action == "left_click_drag":
        end = coord(inp.get("coordinate"))
        path = (
            [list(coord(inp["start_coordinate"], "start_coordinate")), list(end)]
            if inp.get("start_coordinate") is not None
            else [list(end)]
        )
        out = {"type": "drag", "path": path, "button": "left"}
        if text:
            out["modifiers"] = text
    elif action in ("left_mouse_down", "left_mouse_up"):
        out = {"type": "mouse_down" if action == "left_mouse_down" else "mouse_up", "button": "left"}
        if inp.get("coordinate") is not None:  # accepted for the workstation server's callers
            out["x"], out["y"] = coord(inp["coordinate"])
    elif action == "cursor_position":
        out = {"type": "cursor_position"}
    elif action == "scroll":
        direction = inp.get("scroll_direction")
        if direction not in SCROLL:
            raise invalid("scroll needs scroll_direction: up, down, left or right")
        amount = inp.get("scroll_amount", 3)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise invalid("scroll_amount must be a positive integer")
        sx, sy = SCROLL[direction]
        out = {"type": "scroll", "dx": sx * amount, "dy": sy * amount}
        if inp.get("coordinate") is not None:
            out["x"], out["y"] = coord(inp["coordinate"])
        if text:
            out["modifiers"] = text
    elif action == "type":
        out = {"type": "type", "text": text}
    elif action == "key":
        out = {"type": "key", "keys": text, "repeat": inp.get("repeat", 1)}
    elif action == "hold_key":
        out = {"type": "hold_key", "keys": text, "duration": inp.get("duration")}
    else:  # wait
        out = {"type": "wait", "duration": inp.get("duration", 1.0)}
    return [out]


def parse(body: Any) -> tuple[list[Group], str]:
    """Returns the groups and the response style: "input" | "block" | "blocks"."""
    if isinstance(body, list):
        if not body:
            raise invalid("empty list of tool_use blocks")
        return [_group(b) for b in body], "blocks"
    if not isinstance(body, dict):
        raise invalid("body must be a tool_use input, a tool_use block or a list of blocks")
    if "action" in body and "input" not in body:
        return [_group({"name": "computer", "input": body})], "input"
    if "input" in body:
        return [_group(body)], "block" if body.get("type") == "tool_use" else "input"
    raise invalid("body must be a tool_use input (with `action`), a tool_use block or a list of blocks")


def _group(block: Any) -> Group:
    meta: dict[str, Any] = {}
    try:
        if not isinstance(block, dict) or not isinstance(block.get("input", {}), dict):
            raise invalid("tool_use blocks are objects with an `input` object")
        meta = {"id": block.get("id"), "name": block.get("name"), "toolset_name": block.get("toolset_name")}
        inp = block.get("input") or {}
        name = block.get("name") or "computer"
        action = inp.get("action") if name == "computer" else name
        if not isinstance(action, str):
            raise invalid("missing `action` for the computer tool")
        meta["action"] = action
        return Group(actions=to_canonical(action, inp), meta=meta)
    except AgentdError as exc:
        return Group(error=exc, meta=meta)


def _image(shot: Shot) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": shot.mime, "data": shot.b64}}


def _text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def render(groups: list[Group], outcome: Outcome, style: str) -> Any:
    last = outcome.last_attempted
    results = []
    for i, (g, o) in enumerate(zip(groups, outcome.groups, strict=True)):
        if o.status == "skipped":
            item: dict[str, Any] = {"content": NOT_EXECUTED, "is_error": True}
        else:
            blocks: list[dict[str, Any]] = []
            shot = o.last_shot
            if o.status == "error":
                assert o.error is not None
                blocks.append(_text(f"{o.error.code}: {o.error.message}"))
            elif shot is not None:
                blocks.append(_image(shot))
            elif g.meta.get("action") == "cursor_position" and o.steps:
                d = o.steps[-1].data
                blocks.append(_text(f"X={d.get('x')}, Y={d.get('y')}"))
            else:
                blocks.append(_text("OK"))
            # One fresh screenshot rides on the last result of the batch.
            if i == last and outcome.final is not None and outcome.final is not shot:
                blocks.append(_image(outcome.final))
            item = {"content": blocks, "is_error": o.status == "error"}
        results.append(item)

    if style == "input":
        return results[0]

    def block(g: Group, item: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "tool_result", "tool_use_id": g.meta.get("id") or ""}
        if g.meta.get("toolset_name"):
            out["toolset_name"] = g.meta["toolset_name"]
        out["content"] = item["content"]
        if item["is_error"]:
            out["is_error"] = True
        return out

    blocks_out = [block(g, item) for g, item in zip(groups, results, strict=True)]
    return blocks_out[0] if style == "block" else blocks_out
