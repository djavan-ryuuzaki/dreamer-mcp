"""Small Pillow helpers for inputs (flatten transparency, read size) and outputs (trim)."""

from __future__ import annotations

import io

from PIL import Image, ImageColor, UnidentifiedImageError


def open_image(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except (UnidentifiedImageError, OSError):
        return None


def has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA", "PA"):
        return img.getchannel("A").getextrema()[0] < 255
    return img.mode == "P" and "transparency" in img.info


def flatten(img: Image.Image, color: str) -> bytes:
    """Composite onto a solid color. ComfyUI's LoadImage drops the alpha channel, so without
    this the model sees whatever RGB happens to sit under the transparent pixels."""
    rgba = img.convert("RGBA")
    board = Image.new("RGBA", rgba.size, ImageColor.getrgb(color))
    board.alpha_composite(rgba)
    buf = io.BytesIO()
    board.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def fit_size(size: tuple[int, int], max_side: int) -> tuple[int, int]:
    """Scale (w, h) down so the longest side is at most max_side, keeping the aspect ratio."""
    w, h = size
    scale = min(1.0, max_side / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def parse_aspect_ratio(value: object) -> float:
    """ "16:9", "16x9", "16:9 (Widescreen)", "1.78" or "square" -> width / height."""
    text = str(value).strip().lower().split(" (")[0]
    if text in ("square", "quadrado"):
        return 1.0
    for sep in (":", "x", "/"):
        if sep in text:
            a, _, b = text.partition(sep)
            try:
                w, h = float(a), float(b)
            except ValueError:
                break
            if w > 0 and h > 0:
                return w / h
            break
    else:
        try:
            ratio = float(text)
            if ratio > 0:
                return ratio
        except ValueError:
            pass
    raise ValueError(f"Invalid aspect ratio {value!r}; use e.g. '16:9', '3:4' or '1.5'")


def size_for(ratio: float, megapixels: float) -> tuple[int, int]:
    """Width/height with the given ratio and area; 1 MP = 1024x1024 (as ComfyUI's
    ResolutionSelector)."""
    area = megapixels * 1024 * 1024
    width = (area * ratio) ** 0.5
    return max(1, round(width)), max(1, round(width / ratio))


def round_to_multiple(value: int, multiple: int) -> int:
    return max(multiple, int(value / multiple + 0.5) * multiple)


def trim_alpha(data: bytes, padding: int = 0, threshold: int = 8) -> bytes | None:
    """Crop a transparent PNG to its visible content plus `padding` px. None when there is
    nothing to trim (no alpha, fully transparent, or content already touches every edge)."""
    img = open_image(data)
    if img is None or not has_alpha(img):
        return None
    img = img.convert("RGBA")
    bbox = img.getchannel("A").point(lambda v: 255 if v > threshold else 0).getbbox()
    if bbox is None:
        return None
    left, top, right, bottom = bbox
    box = (max(0, left - padding), max(0, top - padding),
           min(img.width, right + padding), min(img.height, bottom + padding))
    if box == (0, 0, img.width, img.height):
        return None
    buf = io.BytesIO()
    img.crop(box).save(buf, format="PNG")
    return buf.getvalue()
