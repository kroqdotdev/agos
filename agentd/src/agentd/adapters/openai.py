"""OpenAI Responses API computer tool (`{"type": "computer"}`).

Input: a `computer_call` item (GA: batched `actions[]`; preview: one
`action`), a bare action, `{"actions": [...]}` or a list of actions.
Output: a `computer_call_output` item whose `output` is a
`computer_screenshot`. Coordinates are screenshot pixels.
"""

from __future__ import annotations

from typing import Any

from agentd.adapters import Group, Outcome
from agentd.errors import AgentdError, invalid
from agentd.keys import normalize_combo

BUTTONS = {
    "left": "left",
    "right": "right",
    "wheel": "middle",
    "middle": "middle",
    "back": "back",
    "forward": "forward",
}
PX_PER_WHEEL_CLICK = 100  # OpenAI's xdotool recipe: clicks = max(1, |round(delta / 100)|)
WAIT_SECONDS = 2.0  # the guide's handlers sleep 2000 ms for `wait`


def _clicks(delta: Any) -> int:
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        raise invalid("scroll_x/scroll_y must be numbers")
    if delta == 0:
        return 0
    n = max(1, abs(round(delta / PX_PER_WHEEL_CLICK)))
    return n if delta > 0 else -n


def to_canonical(a: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(a, dict):
        raise invalid("each OpenAI action must be an object")
    t = a.get("type")
    mods = a.get("keys") or []
    if t == "click":
        button = BUTTONS.get(str(a.get("button", "left")).lower())
        if button is None:
            raise invalid(f"unknown button {a.get('button')!r}")
        return [{"type": "click", "x": a.get("x"), "y": a.get("y"), "button": button, "modifiers": mods}]
    if t == "double_click":
        return [{"type": "click", "x": a.get("x"), "y": a.get("y"), "count": 2, "modifiers": mods}]
    if t == "drag":
        path = a.get("path")
        if not isinstance(path, list):
            raise invalid("drag needs path: [{x, y}, ...]")
        return [{"type": "drag", "path": path, "modifiers": mods}]
    if t == "move":
        return [{"type": "move", "x": a.get("x"), "y": a.get("y"), "modifiers": mods}]
    if t == "scroll":
        out = {
            "type": "scroll",
            "dx": _clicks(a.get("scroll_x", 0)),
            "dy": _clicks(a.get("scroll_y", 0)),
            "modifiers": mods,
        }
        if a.get("x") is not None or a.get("y") is not None:
            out["x"], out["y"] = a.get("x"), a.get("y")
        if out["dx"] == 0 and out["dy"] == 0:
            return [{"type": "move", "x": a.get("x"), "y": a.get("y")}] if "x" in out else []
        return [out]
    if t == "keypress":
        keys = a.get("keys")
        if not isinstance(keys, list) or not keys:
            raise invalid("keypress needs keys: [...]")
        # Keys in one keypress are pressed together; OpenAI spells them in caps.
        return [{"type": "key", "keys": normalize_combo([str(k) for k in keys], lower_letters=True)}]
    if t == "type":
        return [{"type": "type", "text": a.get("text")}]
    if t == "wait":
        return [{"type": "wait", "duration": WAIT_SECONDS}]
    if t == "screenshot":
        return [{"type": "screenshot"}]
    raise invalid(f"unsupported OpenAI computer action {t!r}")


def parse(body: Any) -> tuple[list[Group], dict[str, Any]]:
    """Returns one group per OpenAI action plus call metadata."""
    meta: dict[str, Any] = {}
    if isinstance(body, list):
        actions = body
    elif isinstance(body, dict):
        meta = {"call_id": body.get("call_id")}
        if isinstance(body.get("actions"), list):
            actions = body["actions"]
        elif isinstance(body.get("action"), dict):
            actions = [body["action"]]
        elif body.get("type") not in (None, "computer_call"):
            actions = [body]
        else:
            raise invalid("expected a computer_call with `actions`, a bare action or a list of actions")
        pending = body.get("pending_safety_checks") or []
        acked = body.get("acknowledged_safety_checks") or []
        acked_ids = {c.get("id") for c in acked if isinstance(c, dict)}
        missing = [c for c in pending if isinstance(c, dict) and c.get("id") not in acked_ids]
        if missing:
            raise AgentdError(
                "CONFIRMATION_REQUIRED",
                "pending_safety_checks need a human's approval; resend with acknowledged_safety_checks: "
                + "; ".join(f"{c.get('code')}: {c.get('message')}" for c in missing),
            )
        meta["acknowledged_safety_checks"] = [c for c in acked if isinstance(c, dict)]
    else:
        raise invalid("expected a computer_call object or a list of actions")
    if not actions:
        actions = [{"type": "screenshot"}]
    groups = []
    for a in actions:
        try:
            groups.append(Group(actions=to_canonical(a), meta={"type": a.get("type")}))
        except AgentdError as exc:
            groups.append(Group(error=exc, meta={"type": a.get("type") if isinstance(a, dict) else None}))
    return groups, meta


def render(outcome: Outcome, meta: dict[str, Any]) -> dict[str, Any]:
    shot = outcome.final
    if shot is None:
        for g in reversed(outcome.groups):
            if g.last_shot is not None and not g.last_shot.meta.get("zoom"):
                shot = g.last_shot
                break
    out: dict[str, Any] = {"type": "computer_call_output"}
    if meta.get("call_id"):
        out["call_id"] = meta["call_id"]
    if meta.get("acknowledged_safety_checks"):
        out["acknowledged_safety_checks"] = meta["acknowledged_safety_checks"]
    out["output"] = {
        "type": "computer_screenshot",
        "image_url": f"data:{shot.mime};base64,{shot.b64}" if shot else "",
        "detail": "original",
    }
    return out
