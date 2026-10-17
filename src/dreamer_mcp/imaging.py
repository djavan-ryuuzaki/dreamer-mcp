"""Small Pillow helpers for inputs (flatten transparency, read size) and outputs (trim)."""

from __future__ import annotations

import io

from PIL import Image, ImageColor, ImageDraw, ImageFont, UnidentifiedImageError


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


def on_checkerboard(img: Image.Image, cell: int = 16,
                    colors: tuple[tuple[int, int, int], tuple[int, int, int]] = (
                        (255, 255, 255), (204, 204, 204))) -> Image.Image:
    """Show transparency the way image editors do, so it survives a JPEG preview."""
    img = img.convert("RGBA")
    board = Image.new("RGB", img.size, colors[0])
    grey = Image.new("RGB", (cell, cell), colors[1])
    for y in range(0, img.height, cell):
        for x in range((y // cell) % 2 * cell, img.width, 2 * cell):
            board.paste(grey, (x, y))
    board.paste(img, (0, 0), img)
    return board


def contact_sheet(items: list[tuple[str, bytes]], max_size: int, columns: int = 4) -> bytes:
    """One JPEG with every image (transparency on a checkerboard) and its label below it, so
    a whole batch (e.g. decomposed layers) fits in a single preview. The checkerboard is dark,
    so light content such as white text stays visible."""
    images = [(label, img) for label, data in items if (img := open_image(data)) is not None]
    if not images:
        raise ValueError("no images")
    columns = max(1, min(columns, len(images)))
    rows = -(-len(images) // columns)
    gap, caption = 8, 22
    tile = max(64, (max_size - gap * (columns + 1)) // columns)
    tile_h = max(32, round(tile * max(img.height / img.width for _, img in images)))
    font = ImageFont.load_default(size=14)
    sheet = Image.new("RGB", (columns * tile + gap * (columns + 1),
                              rows * (tile_h + caption) + gap * (rows + 1)), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    for i, (label, img) in enumerate(images):
        img.thumbnail((tile, tile_h))
        x = gap + (i % columns) * (tile + gap)
        y = gap + (i // columns) * (tile_h + caption + gap)
        board = on_checkerboard(img, cell=8, colors=((70, 70, 70), (100, 100, 100)))
        sheet.paste(board, (x + (tile - img.width) // 2, y + (tile_h - img.height) // 2))
        draw.text((x + 2, y + tile_h + 3), label, fill=(235, 235, 235), font=font)
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
