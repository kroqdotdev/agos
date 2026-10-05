"""Provider shapes -> canonical actions -> provider-shaped results."""

import base64
import json

import pytest
from conftest import make_config
from fakes import FakeBackend

from agentd.actions import parse_action
from agentd.adapters import anthropic, gemini, openai
from agentd.audit import AuditLog
from agentd.auth import trusted
from agentd.core import Core
from agentd.errors import AgentdError
from agentd.service import LocalService

P = trusted("internal", "test")


def canon(actions):
    """Validate through the canonical parser too."""
    return [parse_action(a) for a in actions]


def make_service(tmp_path, width=1280, height=800):
    be = FakeBackend(width, height)
    cfg = make_config(tmp_path, settle_ms=0, cdp_url="")
    audit = AuditLog(cfg.audit_path)
    return LocalService(Core(cfg, be, audit), cfg, audit), be


# --------------------------------------------------------------- anthropic


@pytest.mark.parametrize(
    "action,inp,expected",
    [
        ("left_click", {"coordinate": [5, 6]}, {"type": "click", "button": "left", "count": 1, "x": 5, "y": 6}),
        ("double_click", {}, {"type": "click", "button": "left", "count": 2}),
        (
            "triple_click",
            {"coordinate": [1, 2], "text": "shift"},
            {"type": "click", "button": "left", "count": 3, "x": 1, "y": 2, "modifiers": "shift"},
        ),
        (
            "right_click",
            {"coordinate": [1, 2], "key": "ctrl"},  # computer_20250124 spelling
            {"type": "click", "button": "right", "count": 1, "x": 1, "y": 2, "modifiers": "ctrl"},
        ),
        ("mouse_move", {"coordinate": [3, 4]}, {"type": "move", "x": 3, "y": 4}),
        (
            "left_click_drag",
            {"start_coordinate": [1, 2], "coordinate": [30, 40], "text": "alt"},
            {"type": "drag", "path": [[1, 2], [30, 40]], "button": "left", "modifiers": "alt"},
        ),
        ("left_click_drag", {"coordinate": [30, 40]}, {"type": "drag", "path": [[30, 40]], "button": "left"}),
        ("left_mouse_down", {}, {"type": "mouse_down", "button": "left"}),
        (
            "scroll",
            {"scroll_direction": "up", "scroll_amount": 5, "coordinate": [9, 9], "text": "ctrl"},
            {"type": "scroll", "dx": 0, "dy": -5, "x": 9, "y": 9, "modifiers": "ctrl"},
        ),
        ("scroll", {"scroll_direction": "right"}, {"type": "scroll", "dx": 3, "dy": 0}),
        ("type", {"text": "hi"}, {"type": "type", "text": "hi"}),
        ("key", {"text": "Tab", "repeat": 4}, {"type": "key", "keys": "Tab", "repeat": 4}),
        ("hold_key", {"text": "shift", "duration": 2}, {"type": "hold_key", "keys": "shift", "duration": 2}),
        ("wait", {"duration": 300}, {"type": "wait", "duration": 300}),
        ("zoom", {"region": [0, 0, 10, 10]}, {"type": "zoom", "region": [0, 0, 10, 10]}),
        ("cursor_position", {}, {"type": "cursor_position"}),
        ("screenshot", {}, {"type": "screenshot"}),
    ],
)
def test_anthropic_mapping(action, inp, expected):
    out = anthropic.to_canonical(action, inp)
    assert out == [expected]
    canon(out)


def test_anthropic_toolset_limits_are_enforced():
    for bad in ({"text": "Tab", "repeat": 101}, {"text": "Tab", "repeat": 0}):
        with pytest.raises(AgentdError):
            canon(anthropic.to_canonical("key", bad))
    with pytest.raises(AgentdError):
        canon(anthropic.to_canonical("wait", {"duration": 301}))
    with pytest.raises(AgentdError):
        anthropic.to_canonical("scroll", {"scroll_direction": "sideways"})
    with pytest.raises(AgentdError):
        anthropic.to_canonical("teleport", {})


def test_anthropic_request_styles():
    groups, style = anthropic.parse({"action": "left_click", "coordinate": [1, 2]})
    assert style == "input" and groups[0].meta["action"] == "left_click"
    block = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "left_click",
        "toolset_name": "computer",
        "input": {"coordinate": [1, 2]},
    }
    groups, style = anthropic.parse(block)
    assert style == "block" and groups[0].actions[0]["type"] == "click"
    groups, style = anthropic.parse([block, {**block, "id": "toolu_2", "name": "screenshot", "input": {}}])
    assert style == "blocks" and len(groups) == 2
    groups, _ = anthropic.parse(
        {"type": "tool_use", "id": "t", "name": "computer", "input": {"action": "key", "text": "Return"}}
    )
    assert groups[0].actions == [{"type": "key", "keys": "Return", "repeat": 1}]


@pytest.mark.anyio
async def test_anthropic_toolset_batch_semantics(tmp_path):
    svc, be = make_service(tmp_path)
    await svc.screenshot(P)
    blocks = [
        {
            "type": "tool_use",
            "id": "a",
            "name": "left_click",
            "toolset_name": "computer",
            "input": {"coordinate": [10, 10]},
        },
        {"type": "tool_use", "id": "b", "name": "key", "toolset_name": "computer", "input": {"text": "nosuchkey"}},
        {"type": "tool_use", "id": "c", "name": "type", "toolset_name": "computer", "input": {"text": "x"}},
    ]
    out, err = await svc.anthropic(P, None, blocks)
    assert err is not None and err.code == "INVALID_ACTION"
    assert [b["tool_use_id"] for b in out] == ["a", "b", "c"]
    assert all(b["toolset_name"] == "computer" and b["type"] == "tool_result" for b in out)
    assert out[0]["content"][0] == {"type": "text", "text": "OK"} and "is_error" not in out[0]
    assert out[1]["is_error"] is True and out[1]["content"][0]["text"].startswith("INVALID_ACTION")
    # The fresh screenshot rides on the last attempted result.
    assert out[1]["content"][-1]["type"] == "image"
    assert out[2] == {
        "type": "tool_result",
        "tool_use_id": "c",
        "toolset_name": "computer",
        "content": anthropic.NOT_EXECUTED,
        "is_error": True,
    }
    assert ("click", 1, 1) in be.calls and not any(c[0] == "type_text" for c in be.calls)


@pytest.mark.anyio
async def test_anthropic_single_input_results(tmp_path):
    svc, be = make_service(tmp_path)
    out, err = await svc.anthropic(P, None, {"action": "screenshot"})
    assert err is None and out["is_error"] is False
    img = out["content"][0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
    assert base64.b64decode(img["source"]["data"]).startswith(b"\x89PNG")
    be.pointer = (512, 384)
    out, _ = await svc.anthropic(P, None, {"action": "cursor_position"})
    assert out["content"] == [{"type": "text", "text": "X=512, Y=384"}]
    out, _ = await svc.anthropic(P, None, {"action": "left_click", "coordinate": [1, 1]})
    assert [b["type"] for b in out["content"]] == ["text", "image"]
    out, _ = await svc.anthropic(P, None, {"action": "left_click", "coordinate": [1, 1]}, screenshot_after=False)
    assert [b["type"] for b in out["content"]] == ["text"]


@pytest.mark.anyio
async def test_anthropic_stale_frame_returns_screenshot(tmp_path):
    svc, be = make_service(tmp_path)
    out, err = await svc.anthropic(P, "fresh", {"action": "left_click", "coordinate": [1, 1]})
    assert err.code == "STALE_FRAME" and out["is_error"] is True
    assert out["content"][0]["text"].startswith("STALE_FRAME")
    assert out["content"][1]["type"] == "image"


# ------------------------------------------------------------------ openai


@pytest.mark.parametrize(
    "action,expected",
    [
        (
            {"type": "click", "button": "left", "x": 405, "y": 157},
            [{"type": "click", "x": 405, "y": 157, "button": "left", "modifiers": []}],
        ),
        (
            {"type": "click", "button": "wheel", "x": 1, "y": 2, "keys": ["SHIFT"]},
            [{"type": "click", "x": 1, "y": 2, "button": "middle", "modifiers": ["SHIFT"]}],
        ),
        ({"type": "double_click", "x": 1, "y": 2}, [{"type": "click", "x": 1, "y": 2, "count": 2, "modifiers": []}]),
        (
            {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]},
            [{"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}], "modifiers": []}],
        ),
        ({"type": "move", "x": 7, "y": 8}, [{"type": "move", "x": 7, "y": 8, "modifiers": []}]),
        (
            {"type": "scroll", "x": 10, "y": 20, "scroll_x": 0, "scroll_y": 250},
            [{"type": "scroll", "dx": 0, "dy": 2, "x": 10, "y": 20, "modifiers": []}],
        ),
        (
            {"type": "scroll", "x": 10, "y": 20, "scroll_x": -30, "scroll_y": 0},
            [{"type": "scroll", "dx": -1, "dy": 0, "x": 10, "y": 20, "modifiers": []}],
        ),
        ({"type": "keypress", "keys": ["CTRL", "A"]}, [{"type": "key", "keys": "ctrl+a"}]),
        ({"type": "keypress", "keys": ["ENTER"]}, [{"type": "key", "keys": "Return"}]),
        ({"type": "keypress", "keys": ["META", "ARROWLEFT"]}, [{"type": "key", "keys": "super+Left"}]),
        ({"type": "type", "text": "penguin"}, [{"type": "type", "text": "penguin"}]),
        ({"type": "wait"}, [{"type": "wait", "duration": 2.0}]),
        ({"type": "screenshot"}, [{"type": "screenshot"}]),
    ],
)
def test_openai_mapping(action, expected):
    out = openai.to_canonical(action)
    assert out == expected
    canon(out)


def test_openai_safety_checks_need_acknowledgement():
    call = {
        "type": "computer_call",
        "call_id": "c1",
        "actions": [{"type": "screenshot"}],
        "pending_safety_checks": [{"id": "sc1", "code": "malicious_instructions", "message": "hmm"}],
    }
    with pytest.raises(AgentdError) as e:
        openai.parse(call)
    assert e.value.code == "CONFIRMATION_REQUIRED" and e.value.status == 409
    groups, meta = openai.parse({**call, "acknowledged_safety_checks": [{"id": "sc1"}]})
    assert meta["acknowledged_safety_checks"] == [{"id": "sc1"}]


@pytest.mark.anyio
async def test_openai_computer_call_output(tmp_path):
    svc, be = make_service(tmp_path)
    await svc.screenshot(P)
    call = {
        "type": "computer_call",
        "id": "cu_1",
        "call_id": "call_002",
        "status": "completed",
        "pending_safety_checks": [],
        "actions": [{"type": "click", "button": "left", "x": 405, "y": 157}, {"type": "type", "text": "penguin"}],
    }
    out, err = await svc.openai(P, None, call)
    assert err is None
    assert out["type"] == "computer_call_output" and out["call_id"] == "call_002"
    assert out["output"]["type"] == "computer_screenshot" and out["output"]["detail"] == "original"
    assert out["output"]["image_url"].startswith("data:image/png;base64,")
    assert ("type_text", "penguin") in be.calls and be.pointer == (405, 157)
    # Legacy single `action`, and a bare list.
    out, err = await svc.openai(P, None, {"call_id": "x", "action": {"type": "screenshot"}})
    assert err is None and out["output"]["image_url"]
    out, err = await svc.openai(P, None, [{"type": "move", "x": 1, "y": 1}])
    assert err is None and "call_id" not in out


# ------------------------------------------------------------------ gemini

SCREEN = (1440, 900)
SEARCH = "https://www.google.com/"


@pytest.mark.parametrize(
    "name,args,expected",
    [
        (
            "click_at",
            {"x": 500, "y": 500},
            [{"type": "click", "x": 500, "y": 500, "coord_space": "normalized", "button": "left", "count": 1}],
        ),
        (
            "click",
            {"x": 1, "y": 2, "intent": "press OK"},
            [{"type": "click", "x": 1, "y": 2, "coord_space": "normalized", "button": "left", "count": 1}],
        ),
        (
            "right_click",
            {"x": 1, "y": 2},
            [{"type": "click", "x": 1, "y": 2, "coord_space": "normalized", "button": "right", "count": 1}],
        ),
        ("hover_at", {"x": 10, "y": 20}, [{"type": "move", "x": 10, "y": 20, "coord_space": "normalized"}]),
        (
            "type_text_at",
            {"x": 10, "y": 20, "text": "hi", "press_enter": True},
            [
                {"type": "click", "x": 10, "y": 20, "coord_space": "normalized"},
                {"type": "key", "keys": "ctrl+a"},
                {"type": "key", "keys": "BackSpace"},
                {"type": "type", "text": "hi"},
                {"type": "key", "keys": "Return"},
            ],
        ),
        (
            "type_text_at",
            {"x": 10, "y": 20, "text": "hi", "clear_before_typing": False},
            [{"type": "click", "x": 10, "y": 20, "coord_space": "normalized"}, {"type": "type", "text": "hi"}],
        ),
        (
            "type",
            {"text": "hello", "press_enter": True},
            [{"type": "type", "text": "hello"}, {"type": "key", "keys": "Return"}],
        ),
        ("key_combination", {"keys": "Control+A"}, [{"type": "key", "keys": "ctrl+a"}]),
        ("hotkey", {"keys": ["Control", "Shift", "T"]}, [{"type": "key", "keys": "ctrl+shift+t"}]),
        ("press_key", {"key": "Enter"}, [{"type": "key", "keys": "Return"}]),
        ("key_down", {"key": "Shift"}, [{"type": "key_down", "keys": "shift"}]),
        (
            "drag_and_drop",
            {"x": 100, "y": 200, "destination_x": 300, "destination_y": 400},
            [{"type": "drag", "path": [[100, 200], [300, 400]], "coord_space": "normalized"}],
        ),
        (
            "drag_and_drop",
            {"start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4},
            [{"type": "drag", "path": [[1, 2], [3, 4]], "coord_space": "normalized"}],
        ),
        ("scroll_document", {"direction": "down"}, [{"type": "key", "keys": "Page_Down"}]),
        (
            "scroll_at",
            {"x": 500, "y": 500, "direction": "down", "magnitude": 800},
            [{"type": "scroll", "x": 500, "y": 500, "coord_space": "normalized", "dx": 0, "dy": 7}],
        ),
        (
            "scroll",
            {"x": 500, "y": 500, "direction": "left"},
            [{"type": "scroll", "x": 500, "y": 500, "coord_space": "normalized", "dx": -4, "dy": 0}],
        ),
        ("wait_5_seconds", {}, [{"type": "wait", "duration": 5}]),
        ("wait", {"seconds": 2}, [{"type": "wait", "duration": 2}]),
        ("take_screenshot", {}, [{"type": "screenshot"}]),
        (
            "navigate",
            {"url": "https://example.com"},
            [
                {"type": "key", "keys": "ctrl+l"},
                {"type": "type", "text": "https://example.com"},
                {"type": "key", "keys": "Return"},
            ],
        ),
        (
            "search",
            {},
            [{"type": "key", "keys": "ctrl+l"}, {"type": "type", "text": SEARCH}, {"type": "key", "keys": "Return"}],
        ),
        ("go_back", {}, [{"type": "key", "keys": "alt+Left"}]),
        ("go_forward", {}, [{"type": "key", "keys": "alt+Right"}]),
    ],
)
def test_gemini_mapping(name, args, expected):
    out, special = gemini.to_canonical(name, args, SCREEN, SEARCH)
    assert out == expected and special is None
    canon(out)


def test_gemini_special_and_unsupported():
    assert gemini.to_canonical("open_web_browser", {}, SCREEN, SEARCH) == ([], "open_web_browser")
    with pytest.raises(AgentdError):
        gemini.to_canonical("open_app", {"app_name": "x"}, SCREEN, SEARCH)
    with pytest.raises(AgentdError):
        gemini.to_canonical("click_at", {"x": 1}, SCREEN, SEARCH)


def test_gemini_safety_decision_needs_acknowledgement():
    call = {
        "name": "click_at",
        "args": {
            "x": 1,
            "y": 2,
            "safety_decision": {"decision": "require_confirmation", "explanation": "cookie banner"},
        },
    }
    with pytest.raises(AgentdError) as e:
        gemini.parse(call, SCREEN, SEARCH)
    assert e.value.code == "CONFIRMATION_REQUIRED"
    groups, many = gemini.parse({**call, "safety_acknowledgement": True}, SCREEN, SEARCH)
    assert groups[0].meta["acknowledged"] and not many


@pytest.mark.anyio
async def test_gemini_generate_content_response(tmp_path):
    svc, be = make_service(tmp_path, 1440, 900)
    await svc.screenshot(P)
    out, err = await svc.gemini(P, None, {"name": "click_at", "args": {"x": 500, "y": 500}, "id": "fc1"})
    assert err is None
    assert out["id"] == "fc1" and out["name"] == "click_at" and out["response"] == {"url": ""}
    part = out["parts"][0]["inlineData"]
    assert part["mimeType"] == "image/png" and base64.b64decode(part["data"]).startswith(b"\x89PNG")
    assert be.pointer == (720, 450)


@pytest.mark.anyio
async def test_gemini_parallel_calls_stop_at_failure(tmp_path):
    svc, be = make_service(tmp_path, 1440, 900)
    await svc.screenshot(P)
    calls = [
        {"name": "hover_at", "args": {"x": 100, "y": 100}},
        {"name": "list_apps", "args": {}},
        {"name": "click_at", "args": {"x": 1, "y": 1}},
    ]
    out, err = await svc.gemini(P, None, calls)
    assert err.code == "INVALID_ACTION" and len(out) == 3
    assert "error" not in out[0]["response"]
    assert out[1]["response"]["error"].startswith("INVALID_ACTION") and "parts" in out[1]
    assert out[2]["response"]["error"].startswith("Not executed")
    assert not any(c[0] == "click" for c in be.calls)


@pytest.mark.anyio
async def test_gemini_interactions_style(tmp_path):
    svc, be = make_service(tmp_path, 1440, 900)
    await svc.screenshot(P)
    call = {"type": "function_call", "name": "type", "id": "call_7", "arguments": {"text": "hello", "intent": "fill"}}
    out, err = await svc.gemini(P, None, call)
    assert err is None
    assert out["type"] == "function_result" and out["call_id"] == "call_7" and out["name"] == "type"
    assert json.loads(out["result"][0]["text"]) == {"url": ""}
    assert out["result"][1]["type"] == "image" and out["result"][1]["mime_type"] == "image/png"
    assert ("type_text", "hello") in be.calls


@pytest.mark.anyio
async def test_gemini_acknowledged_call_echoes_acknowledgement(tmp_path):
    svc, be = make_service(tmp_path, 1440, 900)
    await svc.screenshot(P)
    body = {
        "function_calls": [
            {
                "name": "click_at",
                "args": {"x": 1, "y": 1, "safety_decision": {"decision": "require_confirmation", "explanation": "buy"}},
            }
        ],
        "safety_acknowledgement": True,
    }
    out, err = await svc.gemini(P, None, body)
    assert err is None and out[0]["response"]["safety_acknowledgement"] == "true"
