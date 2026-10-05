import math

import pytest

from agentd.geometry import (
    Frame,
    box_from_screen,
    fit_size,
    fit_within,
    from_screen,
    region_to_screen,
    to_screen,
)

LIMITS = (2576, 3_750_000)


def tokens(w: int, h: int) -> int:
    return math.ceil(w / 28) * math.ceil(h / 28)


@pytest.mark.parametrize("screen", [(1280, 800), (1440, 900), (1920, 1080), (1024, 768)])
def test_common_screens_are_not_downscaled(screen):
    assert fit_size(*screen, *LIMITS) == screen


@pytest.mark.parametrize("screen", [(3440, 1440), (3840, 2160), (2560, 1600), (2236, 1677), (5120, 1440)])
def test_large_screens_fit_both_limits(screen):
    w, h = fit_size(*screen, *LIMITS)
    assert max(w, h) <= 2576
    assert w * h <= 3_750_000
    assert tokens(w, h) <= 4784  # Anthropic's visual-token budget
    assert abs(w / h - screen[0] / screen[1]) < 0.01


def test_old_limits_match_the_workstation_server():
    # 1568 px / 1.15 MP were the workstation defaults: 3440x1440 -> 1568x656.
    w, h = fit_size(3440, 1440, 1568, 1_150_000)
    assert (w, h) == (1568, 656)


def test_fit_within_upscales_and_keeps_aspect():
    assert fit_within(100, 50, 1280, 800) == (1280, 640)
    assert fit_within(400, 800, 1280, 800) == (400, 800)


def frame(screen=(2560, 1600), image=None):
    image = image or fit_size(*screen, *LIMITS)
    return Frame(1, 1, screen, image)


def test_image_space_scales_to_screen():
    f = frame()
    iw, ih = f.image
    assert to_screen(0, 0, "image", f.screen, f) == (0, 0)
    sx, sy = to_screen(iw - 1, ih - 1, "image", f.screen, f)
    assert (sx, sy) == (2559, 1599)
    # The centre maps to the centre.
    cx, cy = to_screen(iw / 2, ih / 2, "image", f.screen, f)
    assert abs(cx - 1280) <= 2 and abs(cy - 800) <= 2


def test_image_round_trip_is_exact_when_downscaled():
    f = frame((3440, 1440))
    iw, ih = f.image
    for x in range(0, iw, 37):
        for y in range(0, ih, 41):
            sx, sy = to_screen(x, y, "image", f.screen, f)
            assert from_screen(sx, sy, "image", f.screen, f) == (x, y)


def test_identity_at_scale_one():
    f = frame((1280, 800), (1280, 800))
    assert to_screen(640, 412, "image", f.screen, f) == (640, 412)
    assert from_screen(640, 412, "image", f.screen, f) == (640, 412)


def test_normalized_space_uses_gemini_formula():
    screen = (1440, 900)
    assert to_screen(0, 0, "normalized", screen, None) == (0, 0)
    assert to_screen(500, 500, "normalized", screen, None) == (720, 450)
    assert to_screen(999, 999, "normalized", screen, None) == (int(999 / 1000 * 1440), int(999 / 1000 * 900))
    assert from_screen(720, 450, "normalized", screen, None) == (500, 500)


def test_screen_space_clamps():
    assert to_screen(5000, -3, "screen", (1280, 800), None) == (1279, 0)


def test_image_space_needs_a_frame():
    with pytest.raises(ValueError):
        to_screen(1, 1, "image", (100, 100), None)


def test_region_conversion():
    f = frame((2560, 1600), (1280, 800))
    assert region_to_screen([10, 20, 110, 220], "image", f.screen, f) == (20, 40, 220, 440)
    assert region_to_screen([0, 0, 500, 500], "normalized", (1000, 800), None) == (0, 0, 500, 400)
    with pytest.raises(ValueError):
        region_to_screen([10, 10, 5, 20], "screen", (100, 100), None)


def test_box_from_screen_into_image_space():
    f = frame((2560, 1600), (1280, 800))
    assert box_from_screen((20, 40, 220, 440), "image", f.screen, f) == [10, 20, 110, 220]
    assert box_from_screen((0, 0, 1280, 800), "normalized", (2560, 1600), None) == [0, 0, 500, 500]
