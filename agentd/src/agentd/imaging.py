"""Image encoding, resizing and cheap change detection."""

from __future__ import annotations

import io

from PIL import Image, ImageChops

MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
THUMB = (160, 100)


def resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    if img.size == size:
        return img
    return img.resize(size, Image.Resampling.LANCZOS)


def encode(img: Image.Image, fmt: str, jpeg_quality: int = 85, webp_quality: int = 85) -> bytes:
    buf = io.BytesIO()
    if fmt == "png":
        # Level 3 is ~2x faster than the default 6 for a few % larger files.
        img.save(buf, "PNG", compress_level=3)
    elif fmt == "jpeg":
        img.convert("RGB").save(buf, "JPEG", quality=jpeg_quality, optimize=False)
    elif fmt == "webp":
        img.save(buf, "WEBP", quality=webp_quality, method=2)
    else:
        raise ValueError(f"unsupported format {fmt!r}")
    return buf.getvalue()


def thumbnail(img: Image.Image) -> Image.Image:
    """Small grayscale copy used to compare consecutive frames."""
    return img.convert("L").resize(THUMB, Image.Resampling.BOX)


def changed_fraction(a: Image.Image, b: Image.Image, level: int = 12) -> float:
    """Fraction of thumbnail cells whose brightness moved by more than `level`.

    A blinking caret touches one or two cells out of 16000 and stays under
    the default threshold; page loads, dialogs and animations do not.
    """
    if a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > level else 0)
    hist = diff.histogram()
    return hist[255] / (a.size[0] * a.size[1])
