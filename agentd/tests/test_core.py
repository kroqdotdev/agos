"""Core semantics on a fake backend: frames, stale guard, lease, batches, modifiers."""

import asyncio
import json

import pytest
from conftest import make_config
from fakes import FakeBackend

from agentd.actions import parse_batch
from agentd.audit import AuditLog
from agentd.auth import trusted
from agentd.core import Core

pytestmark = pytest.mark.anyio
P = trusted("internal", "test")


def make_core(tmp_path, width=1280, height=800, **cfg):
    backend = FakeBackend(width, height)
    config = make_config(tmp_path, **cfg)
    audit = AuditLog(config.audit_path)
    return Core(config, backend, audit), backend


async def run(core, actions, sid="default", after=False):
    return await core.execute(sid, parse_batch(actions), P, screenshot_after=after)


# ------------------------------------------------------------- screenshots


async def test_screenshot_metadata_and_frames(tmp_path):
    core, be = make_core(tmp_path)
    be.pointer = (640, 412)
    shot = await core.screenshot("default")
    assert shot.meta == {
        "session": "default",
        "frame_id": 1,
        "epoch": 1,
        "screen": [1280, 800],
        "image": [1280, 800],
        "scale": 1.0,
        "coord_space": "image",
        "cursor": [640, 412],
        "format": "png",
    }
    assert shot.data.startswith(b"\x89PNG")
    second = await core.screenshot("other", "jpeg")
    assert second.meta["frame_id"] == 2 and second.mime == "image/jpeg"
    webp = await core.screenshot("other", "webp")
    assert webp.data[8:12] == b"WEBP"


async def test_downscaled_screenshot_reports_scale_and_cursor(tmp_path):
    core, be = make_core(tmp_path, 3440, 1440)
    be.pointer = (3439, 1439)
    shot = await core.screenshot(None)
    w, h = shot.meta["image"]
    assert w <= 2576 and shot.meta["scale"] == pytest.approx(w / 3440, abs=1e-5)
    assert shot.meta["cursor"] == [w - 1, h - 1]


# -------------------------------------------------------- coordinate spaces


async def test_image_coordinates_are_scaled_to_screen(tmp_path):
    core, be = make_core(tmp_path, 2560, 1600, max_image_long_edge=1280, max_image_pixels=1_100_000)
    shot = await core.screenshot("default")
    assert shot.meta["image"] == [1280, 800]
    res = await run(core, [{"type": "click", "x": 640, "y": 400}])
    assert res.ok, res.to_json()
    assert be.pointer == (1281, 801)  # centre of image pixel (640, 400) in screen space
    assert ("click", 1, 1) in be.calls


async def test_screen_and_normalized_spaces(tmp_path):
    core, be = make_core(tmp_path, 1440, 900)
    await core.screenshot("default")
    res = await run(core, [{"type": "move", "x": 100, "y": 50, "coord_space": "screen"}])
    assert res.ok and be.pointer == (100, 50)
    res = await run(core, [{"type": "move", "x": 500, "y": 500, "coord_space": "normalized"}])
    assert res.ok and be.pointer == (720, 450)
    res = await run(core, [{"type": "cursor_position", "coord_space": "normalized"}])
    assert res.results[0].data["x"] == 500 and res.results[0].data["y"] == 500


async def test_cursor_position_without_a_screenshot_is_explained(tmp_path):
    core, be = make_core(tmp_path, 3440, 1440)
    be.pointer = (100, 100)
    res = await run(core, [{"type": "cursor_position"}])
    step = res.results[0]
    assert step.ok and "no screenshot" in step.data["note"]
    assert step.data["x"] < 100  # reported in the (downscaled) space of the next screenshot


# ------------------------------------------------------------- stale guard


async def test_coordinates_need_a_screenshot_first(tmp_path):
    core, be = make_core(tmp_path)
    res = await core.execute("s1", parse_batch([{"type": "click", "x": 5, "y": 5}]), P)
    assert res.error.code == "STALE_FRAME"
    assert res.screenshot is not None and res.screenshot.meta["frame_id"] == 1
    assert be.calls == []  # nothing was clicked
    # The fresh screenshot became the basis, so a retry works.
    res = await run(core, [{"type": "click", "x": 5, "y": 5}], "s1")
    assert res.ok


async def test_geometry_change_makes_frames_stale(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    be.size = (1024, 768)
    res = await run(core, [{"type": "click", "x": 5, "y": 5}])
    assert res.error.code == "STALE_FRAME" and "1280x800 to 1024x768" in res.error.message
    assert res.screenshot.meta["screen"] == [1024, 768]
    # Screen-space input is not tied to a screenshot.
    be.size = (800, 600)
    res = await run(core, [{"type": "click", "x": 5, "y": 5, "coord_space": "screen"}])
    assert res.ok
    # Coordinate-free input does not care about geometry.
    res = await run(core, [{"type": "key", "keys": "Tab"}])
    assert res.ok


async def test_sessions_have_separate_bases(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("a")
    be.size = (1024, 768)
    await core.screenshot("b")
    assert (await run(core, [{"type": "click", "x": 1, "y": 1}], "b")).ok
    assert (await run(core, [{"type": "click", "x": 1, "y": 1}], "a")).error.code == "STALE_FRAME"


async def test_expect_frame_id(tmp_path):
    core, be = make_core(tmp_path)
    s1 = await core.screenshot("default")
    s2 = await core.screenshot("default")
    ok = await run(core, [{"type": "click", "x": 1, "y": 1, "expect_frame_id": s1.meta["frame_id"]}])
    assert ok.ok  # same geometry: an older frame of this epoch is still a valid basis
    bad = await run(core, [{"type": "click", "x": 1, "y": 1, "expect_frame_id": 9999}])
    assert bad.error.code == "STALE_FRAME"
    await core.takeover("alice")
    await core.handback("alice")
    old = await run(core, [{"type": "key", "keys": "a", "expect_frame_id": s2.meta["frame_id"]}])
    assert old.error.code == "STALE_FRAME" and "predates epoch 2" in old.error.message


async def test_expect_frame_id_uses_that_frames_scale(tmp_path):
    core, be = make_core(tmp_path, 2560, 1600, max_image_long_edge=1280, max_image_pixels=1_100_000)
    small = await core.screenshot("default")
    core.config.max_image_long_edge, core.config.max_image_pixels = 2560, 2560 * 1600
    await core.screenshot("default")  # now at scale 1
    await run(core, [{"type": "move", "x": 100, "y": 100, "expect_frame_id": small.meta["frame_id"]}])
    assert be.pointer == (201, 201)


# ------------------------------------------------------------------- lease


async def test_lease_blocks_input_but_not_observation(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    await core.takeover("alice", "fixing a dialog")
    res = await run(core, [{"type": "screenshot"}, {"type": "cursor_position"}, {"type": "type", "text": "x"}])
    assert [r.ok for r in res.results] == [True, True, False]
    assert res.error.code == "HUMAN_IN_CONTROL" and res.error.status == 409
    assert "alice" in res.error.message
    assert not any(c[0] == "type_text" for c in be.calls)
    lease = core.lease_json()
    assert lease["held"] and lease["by"] == "alice" and lease["reason"] == "fixing a dialog"


async def test_handback_bumps_epoch_and_stales_frames(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    await core.takeover("alice")
    out = await core.handback("alice")
    assert out == {"held": False, "epoch": 2, "changed": True}
    res = await run(core, [{"type": "type", "text": "hello"}])
    assert res.error.code == "STALE_FRAME"
    assert res.screenshot.meta["epoch"] == 2
    assert (await run(core, [{"type": "type", "text": "hello"}])).ok
    assert (await core.handback())["changed"] is False


async def test_takeover_releases_held_keys_and_buttons(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    assert (await run(core, [{"type": "key_down", "keys": "shift"}, {"type": "mouse_down"}])).ok
    assert be.pressed == ["shift"] and be.buttons == [1]
    await core.takeover("bob")
    assert be.pressed == [] and be.buttons == []


async def test_takeover_interrupts_hold_key(tmp_path):
    core, be = make_core(tmp_path)

    async def take_soon():
        await asyncio.sleep(0.1)
        await core.takeover("carol")

    task = asyncio.create_task(take_soon())
    t0 = asyncio.get_running_loop().time()
    res = await run(core, [{"type": "hold_key", "keys": "shift", "duration": 30}])
    await task
    assert asyncio.get_running_loop().time() - t0 < 5
    assert res.error.code == "HUMAN_IN_CONTROL"
    assert be.pressed == []


async def test_takeover_stops_long_typing_between_chunks(tmp_path):
    core, be = make_core(tmp_path)
    original = be.type_text
    typed = []

    async def slow_type(text):
        typed.append(text)
        if len(typed) == 2:
            await core.takeover("dave")
        await original(text)

    be.type_text = slow_type
    res = await run(core, [{"type": "type", "text": "x" * 500}])
    assert res.error.code == "HUMAN_IN_CONTROL"
    assert len(typed) == 2


async def test_lease_ttl_expires(tmp_path):
    core, be = make_core(tmp_path)
    await core.takeover("erin", ttl=0.1)
    assert core.lease is not None
    await asyncio.sleep(0.3)
    assert core.lease is None and core.epoch == 2


async def test_hooks_run_with_lease_env(tmp_path):
    out = tmp_path / "hooks.txt"
    core, be = make_core(
        tmp_path,
        on_takeover=["/bin/sh", "-c", f'echo "take $AGENTD_LEASE_BY $AGENTD_EVENT" >> {out}'],
        on_handback=["/bin/sh", "-c", f'echo "back $AGENTD_EVENT" >> {out}'],
    )
    await core.takeover("frank")
    await core.handback("frank")
    assert out.read_text().splitlines() == ["take frank on_takeover", "back on_handback"]


async def test_missing_hook_does_not_break_takeover(tmp_path):
    core, be = make_core(tmp_path, on_takeover=["/nonexistent/viewer-perm", "control"])
    await core.takeover("gina")
    assert core.lease is not None
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(e["event"] == "hook" and "error" in e for e in entries)


# ----------------------------------------------------------------- batches


async def test_batch_stops_at_first_failure(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    be.fail_on[("key_sequence",)] = 1
    res = await run(
        core,
        [
            {"type": "move", "x": 1, "y": 1},
            {"type": "key", "keys": "Tab"},
            {"type": "type", "text": "never"},
            {"type": "click"},
        ],
    )
    assert [r.ok for r in res.results] == [True, False]
    assert res.skipped == [2, 3]
    assert res.error.code == "INTERNAL"
    assert not any(c[0] in ("type_text", "click") for c in be.calls)
    body = res.to_json()
    assert body["ok"] is False and body["error"]["code"] == "INTERNAL"


async def test_screenshot_after_batch(tmp_path):
    core, be = make_core(tmp_path, settle_ms=0)
    await core.screenshot("default")
    res = await core.execute("default", parse_batch([{"type": "move", "x": 3, "y": 4}]), P, screenshot_after=True)
    assert res.ok and res.screenshot.meta["frame_id"] == 2 and res.screenshot.meta["cursor"] == [3, 4]


async def test_batch_is_audited(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    await run(core, [{"type": "type", "text": "secret-ish"}, {"type": "click", "x": 2, "y": 2}])
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    actions = [e for e in entries if e["event"] == "action"]
    assert [a["action"]["type"] for a in actions] == ["type", "click"]
    assert actions[0]["principal"] == "test" and actions[0]["result"] == "ok"
    assert actions[0]["action"]["text_len"] == 10


# ---------------------------------------------------------- modifier safety


async def test_modifier_released_when_a_later_modifier_fails(tmp_path):
    """The workstation server left ctrl stuck if the shift keydown failed."""
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    be.fail_on[("key_down", "shift")] = 1
    res = await run(core, [{"type": "click", "x": 1, "y": 1, "modifiers": ["ctrl", "shift"]}])
    assert res.error.code == "INTERNAL"
    assert be.pressed == []
    assert ("key_up", "ctrl") in be.calls
    assert not any(c[0] == "click" for c in be.calls)


async def test_modifiers_released_when_click_fails(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    be.fail_on[("click",)] = 1
    res = await run(core, [{"type": "scroll", "x": 1, "y": 1, "dy": 3, "modifiers": ["ctrl"]}])
    assert res.error is not None and be.pressed == []


async def test_drag_holds_modifiers_and_releases_button_on_failure(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    res = await run(core, [{"type": "drag", "path": [[10, 10], [200, 120]], "modifiers": ["shift"]}])
    assert res.ok
    names = [c[0] for c in be.calls]
    assert names.index("key_down") < names.index("button_down") < names.index("motion_path")
    assert names.index("button_up") < names.index("key_up")
    assert be.pointer == (200, 120) and be.pressed == [] and be.buttons == []

    be.calls.clear()
    be.fail_on[("motion_path",)] = 1
    res = await run(core, [{"type": "drag", "path": [[10, 10], [300, 300]], "modifiers": ["ctrl"]}])
    assert res.error is not None
    assert be.buttons == [] and be.pressed == []


async def test_one_point_drag_starts_at_cursor(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    be.pointer = (50, 60)
    assert (await run(core, [{"type": "drag", "path": [[70, 80]]}])).ok
    assert ("move", 50, 60) in be.calls and be.pointer == (70, 80)


async def test_input_lock_serializes_batches(tmp_path):
    core, be = make_core(tmp_path)
    order = []
    original = be.type_text

    async def slow(text):
        order.append(("start", text))
        await asyncio.sleep(0.05)
        order.append(("end", text))
        await original(text)

    be.type_text = slow
    await asyncio.gather(run(core, [{"type": "type", "text": "a"}]), run(core, [{"type": "type", "text": "b"}]))
    assert order in (
        [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")],
        [("start", "b"), ("end", "b"), ("start", "a"), ("end", "a")],
    )


async def test_wait_for_stable(tmp_path):
    core, be = make_core(tmp_path)
    res = await core.wait_for_stable(timeout=2, settle_ms=150)
    assert res["stable"] and 140 <= res["waited_ms"] < 1000

    async def flicker():
        for i in range(30):
            be.color = (255, 255, 255) if i % 2 else (0, 0, 0)
            await asyncio.sleep(0.03)

    task = asyncio.create_task(flicker())
    res = await core.wait_for_stable(timeout=0.4, settle_ms=200)
    task.cancel()
    assert res["stable"] is False and res["waited_ms"] >= 390


async def test_zoom_fits_crop_into_screenshot_size(tmp_path):
    core, be = make_core(tmp_path)
    await core.screenshot("default")
    res = await run(core, [{"type": "zoom", "region": [0, 0, 320, 100]}])
    shot = res.results[0].shot
    assert shot.meta["region_screen"] == [0, 0, 320, 100]
    assert shot.meta["image"] == [1280, 400]  # upscaled to fit 1280x800, aspect kept
    assert shot.meta["frame_id"] == 1  # zoom does not change the basis


async def test_takeover_during_mouse_down_leaves_nothing_pressed(tmp_path):
    core, be = make_core(tmp_path)
    original = be.button_down

    async def racing_down(button):
        await core.takeover("hal")  # lands while the button goes down
        await original(button)

    be.button_down = racing_down
    res = await run(core, [{"type": "mouse_down"}, {"type": "key_down", "keys": "shift"}])
    assert res.error.code == "HUMAN_IN_CONTROL" and res.skipped == [1]
    assert be.buttons == [] and be.pressed == [] and core.held_buttons == []
