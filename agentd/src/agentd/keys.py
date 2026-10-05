"""Key-name normalization to xdotool keysym syntax.

Canonical `key` actions use xdotool syntax ("ctrl+l", "alt+Tab", space-separated
sequences like "ctrl+a BackSpace"), which is also what Anthropic's tool emits.
Provider spellings ("ENTER", "ArrowLeft", "Control+A", "CMD") are mapped through
an alias table. xdotool silently ignores unknown key names (exit 0), so every
name is validated against the X keysym table here instead.
"""

from __future__ import annotations

import contextlib

from Xlib import XK

from agentd.errors import invalid

for _group in ("xf86", "xkb", "latin2", "latin3", "latin4", "greek", "cyrillic", "publishing", "technical", "special"):
    with contextlib.suppress(ImportError, AttributeError):  # depends on the python-xlib build
        XK.load_keysym_group(_group)

MODIFIERS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "controlormeta": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    # Linux has no Command key; models trained on macOS send it for the
    # "system" modifier, which is Super under XFCE.
    "super": "super",
    "win": "super",
    "windows": "super",
    "cmd": "super",
    "command": "super",
    "meta": "super",
    "altgr": "ISO_Level3_Shift",
}

ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "spacebar": "space",
    "backspace": "BackSpace",
    "bksp": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "pgup": "Page_Up",
    "prior": "Page_Up",
    "pagedown": "Page_Down",
    "pgdn": "Page_Down",
    "next": "Page_Down",
    "up": "Up",
    "arrowup": "Up",
    "down": "Down",
    "arrowdown": "Down",
    "left": "Left",
    "arrowleft": "Left",
    "right": "Right",
    "arrowright": "Right",
    "capslock": "Caps_Lock",
    "numlock": "Num_Lock",
    "scrolllock": "Scroll_Lock",
    "printscreen": "Print",
    "print": "Print",
    "prtsc": "Print",
    "pause": "Pause",
    "menu": "Menu",
    "contextmenu": "Menu",
    "apps": "Menu",
    "semicolon": "semicolon",
    "equals": "equal",
    "equal": "equal",
    "plus": "plus",
    "minus": "minus",
    "comma": "comma",
    "period": "period",
    "dot": "period",
    "slash": "slash",
    "backslash": "backslash",
    "quote": "apostrophe",
    "backquote": "grave",
    "backtick": "grave",
    "multiply": "KP_Multiply",
    "add": "KP_Add",
    "subtract": "KP_Subtract",
    "decimal": "KP_Decimal",
    "divide": "KP_Divide",
    "separator": "KP_Separator",
    "volumeup": "XF86AudioRaiseVolume",
    "volumedown": "XF86AudioLowerVolume",
    "volumemute": "XF86AudioMute",
    "mute": "XF86AudioMute",
}
ALIASES.update({f"f{i}": f"F{i}" for i in range(1, 25)})

_CHAR_NAMES = {" ": "space", "+": "plus", "\n": "Return", "\t": "Tab"}


def _squash(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def is_keysym(name: str) -> bool:
    if XK.string_to_keysym(name) != XK.NoSymbol:
        return True
    # python-xlib spells XF86AudioMute as XF86_AudioMute.
    return name.startswith("XF86") and XK.string_to_keysym("XF86_" + name[4:].lstrip("_")) != XK.NoSymbol


def normalize_key(token: str, lower_letters: bool = False) -> str:
    """One key name -> xdotool keysym name (or modifier alias)."""
    if token in _CHAR_NAMES:
        return _CHAR_NAMES[token]
    if len(token) == 1:
        return token.lower() if lower_letters else token
    squashed = _squash(token)
    if squashed in MODIFIERS:
        return MODIFIERS[squashed]
    if squashed in ALIASES:
        return ALIASES[squashed]
    if is_keysym(token):
        return token
    for candidate in (token.capitalize(), token.upper(), token.lower()):
        if is_keysym(candidate):
            return candidate
    raise invalid(f"unknown key name {token!r}")


def split_combo(combo: str) -> list[str]:
    """'ctrl+shift+t' -> ['ctrl', 'shift', 't']; 'ctrl++' -> ['ctrl', '+']."""
    if combo == "+":
        return ["+"]
    parts = combo.split("+")
    out: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i] == "" and i + 1 < len(parts) and parts[i + 1] == "":
            out.append("+")
            i += 2
            continue
        if parts[i]:
            out.append(parts[i])
        i += 1
    return out


def normalize_combo(combo: str | list[str], lower_letters: bool = False) -> str:
    tokens = split_combo(combo) if isinstance(combo, str) else [str(t) for t in combo]
    if not tokens:
        raise invalid("empty key combination")
    has_modifier = len(tokens) > 1
    # With modifiers, "ctrl+A" means ctrl+a, not ctrl+shift+a.
    return "+".join(normalize_key(t, lower_letters or has_modifier) for t in tokens)


def normalize_sequence(keys: str, lower_letters: bool = False) -> list[str]:
    """'ctrl+a BackSpace' -> ['ctrl+a', 'BackSpace']."""
    if keys == " ":
        return ["space"]
    if not isinstance(keys, str) or not keys.strip():
        raise invalid("keys must be a non-empty string")
    return [normalize_combo(c, lower_letters) for c in keys.split()]


def normalize_modifiers(mods: str | list[str] | None) -> list[str]:
    """Modifiers for mouse actions: 'ctrl+shift' or ['CTRL', 'SHIFT'] -> ['ctrl', 'shift']."""
    if not mods:
        return []
    items = split_combo(mods) if isinstance(mods, str) else [str(m) for m in mods]
    out: list[str] = []
    for item in items:
        name = MODIFIERS.get(_squash(item))
        if name is None:
            raise invalid(f"unknown modifier {item!r}; use ctrl, shift, alt or super")
        if name not in out:
            out.append(name)
    return out
