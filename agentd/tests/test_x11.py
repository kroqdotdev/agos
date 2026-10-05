"""X11 backend against a private Xvfb (:90-:99)."""

import asyncio
import io
import subprocess

import pytest
from conftest import XWindow, make_config
from PIL import Image
from Xlib import X
from Xlib import display as xdisplay

from agentd.actions import parse_batch
from agentd.audit import AuditLog
from agentd.auth import trusted
from agentd.backend.x11 import X11Backend
from agentd.core import Core
from agentd.errors import AgentdError

pytestmark = pytest.mark.anyio
P = trusted("internal", "test")


@pytest.fixture
async def x11(xvfb):
    be = X11Backend(xvfb.display)
    yield be
    await be.close()


def make_core(tmp_path, be, **cfg):
    config = make_config(tmp_path, display=be.display, **cfg)
    return Core(config, be, AuditLog(config.audit_path))


def modifier_mask(display: str) -> int:
    d = xdisplay.Display(display)
    try:
        return d.screen().root.query_pointer().mask & (X.ShiftMask | X.ControlMask | X.Mod1Mask | X.Mod4Mask)
    finally:
        d.close()


async def test_screenshot_sizes_and_metadata(tmp_path, x11):
    core = make_core(tmp_path, x11)
    shot = await core.screenshot("default")
    assert shot.meta["screen"] == [1280, 800] and shot.meta["image"] == [1280, 800]
    assert Image.open(io.BytesIO(shot.data)).size == (1280, 800)
    small = make_core(tmp_path, x11, max_image_long_edge=640, max_image_pixels=400_000)
    shot = await small.screenshot("default", "jpeg")
    assert shot.meta["image"] == [640, 400] and shot.meta["scale"] == 0.5
    img = Image.open(io.BytesIO(shot.data))
    assert img.format == "JPEG" and img.size == (640, 400)
    shot = await small.screenshot("default", "webp")
    assert Image.open(io.BytesIO(shot.data)).format == "WEBP"


async def test_click_move_cursor_round_trip(tmp_path, x11, xwindow):
    core = make_core(tmp_path, x11, max_image_long_edge=640, max_image_pixels=400_000, settle_ms=50)
    await core.screenshot("default")  # 640x400 basis, scale 0.5
    res = await core.execute(
        "default",
        parse_batch(
            [
                {"type": "click", "x": 150, "y": 150},
                {"type": "cursor_position"},
            ]
        ),
        P,
        screenshot_after=True,
    )
    assert res.ok, res.to_json()
    assert res.results[0].data["screen_xy"] == [301, 301]
    assert (res.results[1].data["x"], res.results[1].data["y"]) == (150, 150)
    assert res.screenshot.meta["cursor"] == [150, 150]
    assert await x11.cursor() == (301, 301)
    events = xwindow.read_events(2, stop_after=1)
    assert events and events[0].startswith("BUTTON 1 201 201")


async def test_typing_into_a_window(tmp_path, x11, xwindow):
    core = make_core(tmp_path, x11)
    text = "Hello, World! agentd 123 ~/_-+"
    res = await core.execute("default", parse_batch([{"type": "type", "text": text}]), P, screenshot_after=False)
    assert res.ok
    assert xwindow.typed(5, expect=text) == text


async def test_key_combo_and_modifier_click(tmp_path, xvfb, x11, xwindow):
    core = make_core(tmp_path, x11)
    await core.screenshot("default")
    res = await core.execute(
        "default",
        parse_batch(
            [
                {"type": "key", "keys": "Return", "repeat": 2},
                {"type": "click", "x": 200, "y": 200, "modifiers": ["ctrl"]},
            ]
        ),
        P,
        screenshot_after=False,
    )
    assert res.ok
    lines = xwindow.read_events(2, stop_after=4)  # Return, Return, Control_L, button
    assert sum(line.startswith("KEY 65293") for line in lines) == 2  # XK_Return
    button = next(line for line in lines if line.startswith("BUTTON"))
    assert int(button.split()[4]) & X.ControlMask
    assert modifier_mask(xvfb.display) == 0


async def test_modifiers_released_after_a_failing_click(tmp_path, xvfb, x11):
    core = make_core(tmp_path, x11)
    await core.screenshot("default")

    async def broken_click(button, count=1):
        assert modifier_mask(xvfb.display) & X.ShiftMask  # held while clicking
        raise AgentdError("INTERNAL", "boom")

    x11.click = broken_click  # type: ignore[method-assign]
    res = await core.execute(
        "default",
        parse_batch([{"type": "click", "x": 10, "y": 10, "modifiers": ["ctrl", "shift"]}]),
        P,
        screenshot_after=False,
    )
    assert res.error is not None and res.error.message == "boom"
    assert modifier_mask(xvfb.display) == 0


async def test_drag_with_modifier(tmp_path, xvfb, x11, xwindow):
    core = make_core(tmp_path, x11)
    await core.screenshot("default")
    res = await core.execute(
        "default",
        parse_batch([{"type": "drag", "path": [[150, 150], [300, 250]], "modifiers": ["shift"]}]),
        P,
        screenshot_after=False,
    )
    assert res.ok
    assert await x11.cursor() == (300, 250)
    lines = [line for line in xwindow.read_events(1.5, stop_after=2) if line.startswith("BUTTON")]
    assert lines[0].startswith("BUTTON 1 50 50") and int(lines[0].split()[4]) & X.ShiftMask
    assert modifier_mask(xvfb.display) == 0


async def test_scroll_sends_wheel_buttons(tmp_path, x11, xwindow):
    core = make_core(tmp_path, x11)
    await core.screenshot("default")
    res = await core.execute(
        "default",
        parse_batch([{"type": "scroll", "x": 200, "y": 200, "dy": 2}, {"type": "scroll", "dx": -1}]),
        P,
        screenshot_after=False,
    )
    assert res.ok
    buttons = [line.split()[1] for line in xwindow.read_events(2, stop_after=3) if line.startswith("BUTTON")]
    assert buttons == ["5", "5", "6"]


async def test_clipboard_round_trip(x11):
    await x11.clipboard_set("héllo agentd ✓")
    assert await x11.clipboard_get() == "héllo agentd ✓"
    await x11.clipboard_set("second")
    assert await x11.clipboard_get() == "second"


async def test_windows_fallback_listing_and_activate(xvfb, x11, xwindow):
    wins = await x11.windows()
    mine = [w for w in wins if w["id"] == xwindow.id]
    assert len(mine) == 1
    w = mine[0]
    assert w["title"] == "agentd-test" and w["class"] == "AgentdTest" and w["instance"] == "agentdtest"
    assert w["pid"] == xwindow.proc.pid and w["geometry"] == [100, 100, 400, 300]
    await x11.activate_window(xwindow.id)
    with pytest.raises(AgentdError) as e:
        await x11.activate_window(0x7FFFFFF)
    assert e.value.code == "NOT_FOUND"


async def test_windows_ewmh_listing(xvfb, x11, xwindow):
    other = XWindow(xvfb.display, "second window", "600,100,200,100")
    d = xdisplay.Display(xvfb.display)
    root = d.screen().root
    client_list, active = d.intern_atom("_NET_CLIENT_LIST"), d.intern_atom("_NET_ACTIVE_WINDOW")
    try:
        # Pretend to be an EWMH window manager.
        root.change_property(client_list, d.intern_atom("WINDOW"), 32, [xwindow.id, other.id])
        root.change_property(active, d.intern_atom("WINDOW"), 32, [other.id])
        d.sync()
        wins = await x11.windows()
        assert [w["id"] for w in wins] == [xwindow.id, other.id]
        assert [w["active"] for w in wins] == [False, True]
        assert wins[1]["title"] == "second window"
    finally:
        root.delete_property(client_list)
        root.delete_property(active)
        d.sync()
        d.close()
        other.close()


async def test_wait_for_stable_on_a_static_screen(tmp_path, x11):
    core = make_core(tmp_path, x11)
    res = await core.wait_for_stable(timeout=3, settle_ms=200)
    assert res["stable"] and res["waited_ms"] < 2000


async def test_zoom_on_real_display(tmp_path, x11, xwindow):
    core = make_core(tmp_path, x11)
    await core.screenshot("default")
    res = await core.execute(
        "default", parse_batch([{"type": "zoom", "region": [100, 100, 500, 400]}]), P, screenshot_after=False
    )
    shot = res.results[0].shot
    assert shot.meta["region_screen"] == [100, 100, 500, 400]
    img = Image.open(io.BytesIO(shot.data))
    assert img.size == (1067, 800)
    # The helper window is white on Xvfb's black root.
    assert img.getpixel((500, 400)) == (255, 255, 255)


async def test_cursor_can_be_drawn(tmp_path, xvfb):
    plain, drawn = X11Backend(xvfb.display), X11Backend(xvfb.display, draw_cursor=True)
    try:
        await plain.move(640, 400)
        a, _ = await plain.capture((600, 360, 680, 440))
        b, _ = await drawn.capture((600, 360, 680, 440))
        assert a.tobytes() != b.tobytes()
    finally:
        await plain.close()
        await drawn.close()


async def test_geometry_change_is_detected(tmp_path, fresh_xvfb):
    be = X11Backend(fresh_xvfb.display)
    try:
        core = make_core(tmp_path, be)
        await core.screenshot("default")
        try:
            size = await be.set_resolution(1024, 768)
        except AgentdError as exc:
            pytest.skip(f"this Xvfb cannot resize: {exc.message}")
        assert size == (1024, 768)
        res = await core.execute("default", parse_batch([{"type": "click", "x": 5, "y": 5}]), P)
        assert res.error.code == "STALE_FRAME" and "1280x800 to 1024x768" in res.error.message
        assert res.screenshot.meta["screen"] == [1024, 768]
        assert (await core.execute("default", parse_batch([{"type": "click", "x": 5, "y": 5}]), P)).ok
    finally:
        await be.close()


async def test_display_unavailable(tmp_path):
    be = X11Backend(":89")  # nothing listens there
    try:
        with pytest.raises(AgentdError) as e:
            await be.screen_size()
        assert e.value.code == "DISPLAY_UNAVAILABLE" and e.value.status == 503
    finally:
        await be.close()


async def test_xdotool_never_reaches_the_live_desktop(x11, xvfb):
    env = x11.subprocess_env()
    assert env["DISPLAY"] == xvfb.display != ":1"
    out = subprocess.run(["xdotool", "getdisplaygeometry"], env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["1280", "800"]
    await asyncio.sleep(0)


async def test_capture_falls_back_to_xlib_backend(xvfb, monkeypatch):
    # KasmVNC's Xvnc lists depth 24 twice; mss's XCB backends then fail to find
    # the root visual. Simulate that and check capture retries with Xlib.
    import mss

    real = mss.MSS
    backends = []

    def fake_mss(**kw):
        backends.append(kw.get("backend"))
        if kw.get("backend") != "xlib":
            raise mss.exception.ScreenShotError("drawable's visual not found in screen's supported visuals")
        return real(**kw)

    monkeypatch.setattr("agentd.backend.x11.mss.MSS", fake_mss)
    be = X11Backend(xvfb.display)
    try:
        img, size = await be.capture()
        assert img.size == size
        await be.capture()
        assert backends == ["default", "xlib"]
    finally:
        await be.close()
