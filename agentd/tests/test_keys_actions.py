import pytest

from agentd.actions import parse_action, parse_batch
from agentd.errors import AgentdError
from agentd.keys import normalize_combo, normalize_modifiers, normalize_sequence


@pytest.mark.parametrize(
    "given,expected",
    [
        ("ctrl+l", "ctrl+l"),
        ("Return", "Return"),
        ("alt+Tab", "alt+Tab"),
        ("ENTER", "Return"),
        ("ArrowLeft", "Left"),
        ("PageDown", "Page_Down"),
        ("Control+A", "ctrl+a"),
        ("CMD+c", "super+c"),
        ("ctrl++", "ctrl+plus"),
        ("F5", "F5"),
        ("esc", "Escape"),
        ("XF86AudioMute", "XF86AudioMute"),
        ("ctrl+shift+T", "ctrl+shift+t"),
    ],
)
def test_combo_normalization(given, expected):
    assert normalize_combo(given) == expected


def test_sequences_and_lists():
    assert normalize_sequence("ctrl+a BackSpace") == ["ctrl+a", "BackSpace"]
    assert normalize_combo(["CTRL", "SHIFT", "A"], lower_letters=True) == "ctrl+shift+a"
    assert normalize_sequence(" ") == ["space"]


def test_unknown_key_names_are_rejected():
    # xdotool would silently ignore them and exit 0.
    with pytest.raises(AgentdError) as e:
        normalize_combo("ctrl+nosuchkey")
    assert e.value.code == "INVALID_ACTION"


def test_modifiers():
    assert normalize_modifiers("ctrl+shift") == ["ctrl", "shift"]
    assert normalize_modifiers(["SHIFT", "META"]) == ["shift", "super"]
    assert normalize_modifiers(None) == []
    with pytest.raises(AgentdError):
        normalize_modifiers("ctrl+q")


def test_parse_click_defaults():
    a = parse_action({"type": "click", "x": 10, "y": 20})
    assert a.params == {"x": 10, "y": 20, "button": "left", "count": 1, "modifiers": []}
    assert a.is_input and a.uses_coords and a.coord_space == "image"


def test_parse_click_without_coordinates_uses_cursor():
    a = parse_action({"type": "click", "button": "wheel", "count": 2})
    assert a.params["button"] == "middle" and not a.uses_coords


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "nope"},
        {"type": "click", "x": 1},
        {"type": "click", "x": 1, "y": 2, "count": 9},
        {"type": "click", "x": "1", "y": 2},
        {"type": "move", "x": 1, "y": 2, "coord_space": "pixels"},
        {"type": "scroll", "dx": 0, "dy": 0},
        {"type": "type", "text": ""},
        {"type": "key", "keys": "ctrl+l", "repeat": 0},
        {"type": "key", "keys": "ctrl+l", "repeat": 101},
        {"type": "hold_key", "keys": "shift", "duration": 301},
        {"type": "wait", "duration": -1},
        {"type": "zoom", "region": [10, 10, 5, 50]},
        {"type": "drag", "path": []},
        {"type": "click", "x": 1, "y": 2, "expect_frame_id": "4"},
        {"type": "screenshot", "format": "gif"},
    ],
)
def test_invalid_actions(bad):
    with pytest.raises(AgentdError) as e:
        parse_action(bad)
    assert e.value.code == "INVALID_ACTION"


def test_parse_drag_accepts_objects_and_arrays():
    a = parse_action({"type": "drag", "path": [{"x": 1, "y": 2}, [3, 4]], "modifiers": ["shift"]})
    assert a.params["path"] == [(1.0, 2.0), (3.0, 4.0)]
    assert a.params["modifiers"] == ["shift"]


def test_batch_validation_reports_index():
    with pytest.raises(AgentdError) as e:
        parse_batch([{"type": "screenshot"}, {"type": "key", "keys": "nosuch"}])
    assert "action 1" in e.value.message
    with pytest.raises(AgentdError):
        parse_batch([])
