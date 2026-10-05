"""Image fitting and coordinate-space conversion (pure functions).

Coordinate spaces:
- "image": pixels of the session's last full screenshot (default).
- "screen": physical X screen pixels.
- "normalized": a 1000x1000 grid (values 0-999) over the whole screen; this is
  Gemini's convention and maps with ``int(v / 1000 * size)`` like Google's
  reference implementation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

COORD_SPACES = ("image", "screen", "normalized")
NORMALIZED = 1000


def fit_scale(w: int, h: int, max_long_edge: int, max_pixels: int) -> float:
    return min(1.0, max_long_edge / max(w, h), math.sqrt(max_pixels / (w * h)))


PATCH = 28  # Anthropic bills images in 28x28 patches: ceil(w/28) * ceil(h/28)


def _padded_area(w: int, h: int) -> int:
    return math.ceil(w / PATCH) * PATCH * math.ceil(h / PATCH) * PATCH


def fit_size(w: int, h: int, max_long_edge: int, max_pixels: int) -> tuple[int, int]:
    """Largest size with the screen's aspect ratio inside both limits.

    `max_pixels` is enforced on the 28-px patch-padded area so that the
    visual-token budget (max_pixels / 784) holds exactly, not just roughly.
    """
    s = fit_scale(w, h, max_long_edge, max_pixels)
    iw, ih = (w, h) if s >= 1.0 else (max(1, math.floor(w * s)), max(1, math.floor(h * s)))
    while _padded_area(iw, ih) > max_pixels and iw > 1 and ih > 1:
        s = min(s, 1.0) * 0.995
        iw, ih = max(1, math.floor(w * s)), max(1, math.floor(h * s))
    return iw, ih


def fit_within(w: int, h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Largest size with w:h aspect that fits in box (may upscale)."""
    s = min(box_w / w, box_h / h)
    return max(1, round(w * s)), max(1, round(h * s))


@dataclass(frozen=True)
class Frame:
    """What a session's model saw: the basis for its image coordinates."""

    frame_id: int
    epoch: int
    screen: tuple[int, int]
    image: tuple[int, int]

    @property
    def scale(self) -> float:
        return self.image[0] / self.screen[0]


def _clamp(v: int, hi: int) -> int:
    return min(max(v, 0), hi - 1)


def _img_to_screen(v: float, img: int, scr: int) -> int:
    # Map to the centre of the image pixel, then to the screen pixel containing it.
    return math.floor((v + 0.5) * scr / img)


def _screen_to_img(v: float, img: int, scr: int) -> int:
    return math.floor((v + 0.5) * img / scr)


def to_screen(x: float, y: float, space: str, screen: tuple[int, int], frame: Frame | None) -> tuple[int, int]:
    sw, sh = screen
    if space == "screen":
        sx, sy = round(x), round(y)
    elif space == "normalized":
        sx, sy = int(x / NORMALIZED * sw), int(y / NORMALIZED * sh)
    elif space == "image":
        if frame is None:
            raise ValueError("image coordinates need a screenshot first")
        iw, ih = frame.image
        fw, fh = frame.screen
        sx, sy = _img_to_screen(x, iw, fw), _img_to_screen(y, ih, fh)
    else:
        raise ValueError(f"unknown coord_space {space!r}")
    return _clamp(sx, sw), _clamp(sy, sh)


def from_screen(sx: int, sy: int, space: str, screen: tuple[int, int], frame: Frame | None) -> tuple[int, int]:
    sw, sh = screen
    if space == "screen":
        return sx, sy
    if space == "normalized":
        return (
            min(NORMALIZED - 1, math.floor(sx * NORMALIZED / sw)),
            min(NORMALIZED - 1, math.floor(sy * NORMALIZED / sh)),
        )
    if space == "image":
        if frame is None:
            raise ValueError("image coordinates need a screenshot first")
        iw, ih = frame.image
        fw, fh = frame.screen
        return _clamp(_screen_to_img(sx, iw, fw), iw), _clamp(_screen_to_img(sy, ih, fh), ih)
    raise ValueError(f"unknown coord_space {space!r}")


def region_to_screen(
    region: list[float], space: str, screen: tuple[int, int], frame: Frame | None
) -> tuple[int, int, int, int]:
    """Convert [x0, y0, x1, y1] (x1/y1 exclusive edges) to a screen box."""
    x0, y0, x1, y1 = region
    sw, sh = screen
    if space == "screen":
        box = (round(x0), round(y0), round(x1), round(y1))
    elif space == "normalized":
        box = (
            int(x0 / NORMALIZED * sw),
            int(y0 / NORMALIZED * sh),
            math.ceil(x1 / NORMALIZED * sw),
            math.ceil(y1 / NORMALIZED * sh),
        )
    elif space == "image":
        if frame is None:
            raise ValueError("image coordinates need a screenshot first")
        iw, ih = frame.image
        fw, fh = frame.screen
        box = (math.floor(x0 * fw / iw), math.floor(y0 * fh / ih), math.ceil(x1 * fw / iw), math.ceil(y1 * fh / ih))
    else:
        raise ValueError(f"unknown coord_space {space!r}")
    bx0, by0 = min(max(box[0], 0), sw), min(max(box[1], 0), sh)
    bx1, by1 = min(max(box[2], 0), sw), min(max(box[3], 0), sh)
    if bx1 <= bx0 or by1 <= by0:
        raise ValueError("region must have x1 > x0 and y1 > y0 inside the screen")
    return bx0, by0, bx1, by1


def box_from_screen(
    box: tuple[int, int, int, int], space: str, screen: tuple[int, int], frame: Frame | None
) -> list[int]:
    """Screen box [x0, y0, x1, y1) to the given space (used for a11y bounds)."""
    x0, y0, x1, y1 = box
    sw, sh = screen
    if space == "screen":
        return [x0, y0, x1, y1]
    if space == "normalized":
        return [
            math.floor(x0 * NORMALIZED / sw),
            math.floor(y0 * NORMALIZED / sh),
            math.ceil(x1 * NORMALIZED / sw),
            math.ceil(y1 * NORMALIZED / sh),
        ]
    if frame is None:
        raise ValueError("image coordinates need a screenshot first")
    iw, ih = frame.image
    fw, fh = frame.screen
    return [math.floor(x0 * iw / fw), math.floor(y0 * ih / fh), math.ceil(x1 * iw / fw), math.ceil(y1 * ih / fh)]
