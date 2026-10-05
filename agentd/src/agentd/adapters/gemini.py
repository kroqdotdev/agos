"""Gemini computer use: predefined function calls with 0-999 coordinates.

Both function sets are accepted:
- legacy (gemini-2.5-computer-use-preview, gemini-3-flash-preview,
  gemini-3.1-pro-preview): click_at, hover_at, type_text_at, key_combination,
  scroll_document, scroll_at, drag_and_drop{x, y, destination_x, destination_y},
  wait_5_seconds, open_web_browser, navigate, search, go_back, go_forward;
- current (gemini-3.5-flash and later): click, double_click, triple_click,
  middle_click, right_click, mouse_down, mouse_up, move, type, press_key,
  key_down, key_up, hotkey, drag_and_drop{start_x, start_y, end_x, end_y},
  scroll{magnitude_in_pixels}, wait{seconds}, take_screenshot, navigate,
  go_back, go_forward, long_press.

Browser-only functions have no desktop equivalent; they are sent as keyboard
shortcuts to the focused browser window (see README).
"""

from __future__ import annotations

import json
from typing import Any

from agentd.adapters import Group, Outcome
from agentd.errors import AgentdError, invalid
from agentd.keys import normalize_combo

PX_PER_WHEEL_CLICK = 100
DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
CLICKS = {
    "click": ("left", 1),
    "click_at": ("left", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
    "middle_click": ("middle", 1),
    "right_click": ("right", 1),
}
UNSUPPORTED = {"open_app", "list_apps", "go_home"}


def _n(args: dict[str, Any], key: str, default: Any = None) -> Any:
    v = args.get(key, default)
    if v is None:
        raise invalid(f"missing argument {key!r}")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise invalid(f"argument {key!r} must be a number")
    return v


def _pt(args: dict[str, Any], kx: str = "x", ky: str = "y") -> dict[str, Any]:
    return {"x": _n(args, kx), "y": _n(args, ky), "coord_space": "normalized"}


def _scroll(direction: Any, magnitude: float, screen: tuple[int, int]) -> tuple[int, int]:
    if direction not in DIRS:
        raise invalid("direction must be up, down, left or right")
    sx, sy = DIRS[direction]
    # Magnitudes are on the 0-999 grid along the scroll axis.
    axis = screen[0] if sx else screen[1]
    clicks = max(1, round(magnitude / 1000 * axis / PX_PER_WHEEL_CLICK))
    return sx * clicks, sy * clicks


def _keys(value: Any) -> str:
    if isinstance(value, list):
        return normalize_combo([str(k) for k in value], lower_letters=True)
    if isinstance(value, str) and value:
        return normalize_combo(value, lower_letters=True)
    raise invalid("keys must be a string like 'Control+A' or a list")


def to_canonical(
    name: str, args: dict[str, Any], screen: tuple[int, int], search_url: str
) -> tuple[list[dict[str, Any]], str | None]:
    """-> (canonical actions, special handler name or None)."""
    if name in CLICKS:
        button, count = CLICKS[name]
        return [{"type": "click", **_pt(args), "button": button, "count": count}], None
    if name in ("hover_at", "move"):
        return [{"type": "move", **_pt(args)}], None
    if name in ("mouse_down", "mouse_up"):
        act: dict[str, Any] = {"type": name, "button": "left"}
        if args.get("x") is not None:
            act.update(_pt(args))
        return [act], None
    if name == "type_text_at":
        acts: list[dict[str, Any]] = [{"type": "click", **_pt(args)}]
        if args.get("clear_before_typing", True):
            acts += [{"type": "key", "keys": "ctrl+a"}, {"type": "key", "keys": "BackSpace"}]
        acts.append({"type": "type", "text": args.get("text")})
        # Google's reference implementation defaults press_enter to False.
        if args.get("press_enter", False):
            acts.append({"type": "key", "keys": "Return"})
        return acts, None
    if name == "type":
        acts = [{"type": "click", **_pt(args)}] if args.get("x") is not None else []
        acts.append({"type": "type", "text": args.get("text")})
        if args.get("press_enter", False):
            acts.append({"type": "key", "keys": "Return"})
        return acts, None
    if name in ("key_combination", "hotkey", "press_key"):
        return [{"type": "key", "keys": _keys(args.get("keys", args.get("key")))}], None
    if name in ("key_down", "key_up"):
        return [{"type": name, "keys": _keys(args.get("key", args.get("keys")))}], None
    if name == "drag_and_drop":
        if "start_x" in args:
            path = [[_n(args, "start_x"), _n(args, "start_y")], [_n(args, "end_x"), _n(args, "end_y")]]
        else:
            path = [[_n(args, "x"), _n(args, "y")], [_n(args, "destination_x"), _n(args, "destination_y")]]
        return [{"type": "drag", "path": path, "coord_space": "normalized"}], None
    if name == "scroll_document":
        direction = args.get("direction")
        if direction in ("up", "down"):
            return [{"type": "key", "keys": "Page_Up" if direction == "up" else "Page_Down"}], None
        dx, dy = _scroll(direction, 500, screen)
        return [{"type": "scroll", "x": 500, "y": 500, "coord_space": "normalized", "dx": dx, "dy": dy}], None
    if name in ("scroll_at", "scroll"):
        magnitude = _n(
            args, "magnitude_in_pixels" if name == "scroll" else "magnitude", 300 if name == "scroll" else 800
        )
        dx, dy = _scroll(args.get("direction"), magnitude, screen)
        return [{"type": "scroll", **_pt(args), "dx": dx, "dy": dy}], None
    if name == "wait_5_seconds":
        return [{"type": "wait", "duration": 5}], None
    if name == "wait":
        return [{"type": "wait", "duration": _n(args, "seconds", 1)}], None
    if name == "take_screenshot":
        return [{"type": "screenshot"}], None
    if name == "long_press":
        return [
            {"type": "mouse_down", **_pt(args)},
            {"type": "wait", "duration": _n(args, "seconds", 2)},
            {"type": "mouse_up", **_pt(args)},
        ], None
    # Browser navigation, sent as shortcuts to the focused browser.
    if name == "navigate":
        url = args.get("url")
        if not isinstance(url, str) or not url:
            raise invalid("navigate needs url")
        return [
            {"type": "key", "keys": "ctrl+l"},
            {"type": "type", "text": url},
            {"type": "key", "keys": "Return"},
        ], None
    if name == "search":
        return [
            {"type": "key", "keys": "ctrl+l"},
            {"type": "type", "text": search_url},
            {"type": "key", "keys": "Return"},
        ], None
    if name == "go_back":
        return [{"type": "key", "keys": "alt+Left"}], None
    if name == "go_forward":
        return [{"type": "key", "keys": "alt+Right"}], None
    if name == "open_web_browser":
        return [], "open_web_browser"
    if name in UNSUPPORTED:
        raise invalid(f"{name} is a mobile-only function; agos is a desktop")
    raise invalid(f"unsupported Gemini function {name!r}")


def _calls(body: Any) -> tuple[list[dict[str, Any]], bool]:
    """Normalize the accepted request shapes to [{name, args, id, style, ack}]."""
    ack_all = False
    if isinstance(body, dict) and isinstance(body.get("function_calls"), list):
        ack_all = bool(body.get("safety_acknowledgement"))
        items = body["function_calls"]
    elif isinstance(body, list):
        items = body
    else:
        items = [body]
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise invalid("function calls must be objects")
        ack = ack_all or bool(item.get("safety_acknowledgement"))
        call = item.get("function_call") or item.get("functionCall") or item
        if call.get("type") == "function_call" or "arguments" in call:
            out.append(
                {
                    "name": call.get("name"),
                    "args": call.get("arguments") or {},
                    "id": call.get("call_id") or call.get("id"),
                    "style": "interactions",
                    "ack": ack,
                }
            )
        else:
            out.append(
                {
                    "name": call.get("name"),
                    "args": call.get("args") or {},
                    "id": call.get("id"),
                    "style": "generate",
                    "ack": ack,
                }
            )
    if not out:
        raise invalid("no function calls")
    return out, isinstance(body, list) or (isinstance(body, dict) and "function_calls" in body)


def parse(body: Any, screen: tuple[int, int], search_url: str) -> tuple[list[Group], bool]:
    calls, many = _calls(body)
    groups = []
    for c in calls:
        meta = {"name": c["name"], "id": c["id"], "style": c["style"]}
        args = c["args"] if isinstance(c["args"], dict) else {}
        decision = args.get("safety_decision")
        if isinstance(decision, dict) and decision.get("decision") == "require_confirmation":
            if not c["ack"]:
                raise AgentdError(
                    "CONFIRMATION_REQUIRED",
                    f"{c['name']} needs a human's confirmation ({decision.get('explanation', '')}); "
                    "resend with safety_acknowledgement: true once approved",
                )
            meta["acknowledged"] = True
        try:
            if not isinstance(c["name"], str):
                raise invalid("function call without a name")
            actions, special = to_canonical(c["name"], args, screen, search_url)
            groups.append(Group(actions=actions, special=special, meta=meta))
        except AgentdError as exc:
            groups.append(Group(error=exc, meta=meta))
    return groups, many


def render(groups: list[Group], outcome: Outcome, many: bool, url: str) -> Any:
    last = outcome.last_attempted
    responses = []
    for i, (g, o) in enumerate(zip(groups, outcome.groups, strict=True)):
        response: dict[str, Any] = {"url": url}
        if o.status == "error" and o.error is not None:
            response["error"] = f"{o.error.code}: {o.error.message}"
        elif o.status == "skipped":
            response["error"] = "Not executed: an earlier function call in this turn failed."
        if g.meta.get("acknowledged"):
            response["safety_acknowledgement"] = "true"
        shot = outcome.final if i == last else None
        if shot is None and o.status == "ok" and o.last_shot is not None:
            shot = o.last_shot
        if g.meta["style"] == "interactions":
            result: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(response)}]
            if shot is not None:
                result.append({"type": "image", "data": shot.b64, "mime_type": shot.mime})
            item: dict[str, Any] = {
                "type": "function_result",
                "name": g.meta["name"],
                "call_id": g.meta["id"],
                "result": result,
            }
            if o.status != "ok":
                item["is_error"] = True
        else:
            item = {"name": g.meta["name"], "response": response}
            if g.meta.get("id"):
                item = {"id": g.meta["id"], **item}
            if shot is not None:
                item["parts"] = [{"inlineData": {"mimeType": shot.mime, "data": shot.b64}}]
        responses.append(item)
    return responses if many else responses[0]
