"""Small Pillow helpers for inputs (flatten transparency, read size) and outputs (trim)."""

from __future__ import annotations

import io

from PIL import (
    Image,
    ImageChops,
    ImageColor,
    ImageDraw,
    ImageFilter,
    ImageFont,
    UnidentifiedImageError,
)


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


def transparent_fraction(data: bytes, threshold: int = 16) -> float:
    """Share of the pixels that are (nearly) fully transparent; 0.0 when opaque."""
    img = open_image(data)
    if img is None or not has_alpha(img):
        return 0.0
    hist = img.convert("RGBA").getchannel("A").histogram()
    return sum(hist[:threshold]) / max(1, sum(hist))


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


def split_alpha(data: bytes, padding: int = 4, gap: float = 0.004, whole: bool = False,
                keep_full: bool = False, threshold: int = 8, core: int = 128, min_area: float = 0.0004,
                max_items: int = 32) -> list[tuple[tuple[int, int, int, int], bytes]]:
    """Cut a transparent layer into one PNG per object. Objects are connected regions of solid
    alpha (> `core`, so faint shadows or glows don't glue neighbors together) closer than
    `gap` (fraction of the longest side); small text-sized pieces on one line are also joined
    across word spaces, so a label stays whole. `whole` keeps the layer as one object (a logo,
    a character). Pieces keep their soft edges (alpha > `threshold`) plus `padding`.
    Returns [((x, y, w, h), png)] in reading order; empty for opaque images. Regions under
    `min_area` (fraction of the canvas) are noise, and one covering almost the whole canvas
    is a background, not an asset, unless `keep_full` (a front layer of a decomposed region
    that is the object itself)."""
    img = open_image(data)
    if img is None or not has_alpha(img):
        return []
    img = img.convert("RGBA")
    width, height = img.size
    long_side = max(width, height)
    alpha = img.getchannel("A")

    # Label on a reduced grid (fast in pure Python), dilated so near parts join.
    step = max(1, round(long_side / 768))
    gw, gh = -(-width // step), -(-height // step)
    solid = alpha.point(lambda v: 255 if v > core else 0)
    # Majority coverage, so a cell does not grow an object by up to `step` px per side.
    grid = solid.resize((gw, gh), Image.Resampling.BOX).point(lambda v: 255 if v >= 128 else 0)
    if whole:
        groups = [[i for i, v in enumerate(grid.tobytes()) if v]]
    else:
        # Dilating both sides by `radius` joins parts up to ~2 * radius cells apart.
        radius = max(1, round(gap * long_side / (2 * step)))
        groups = _merge_text_lines(_components(
            grid.filter(ImageFilter.MaxFilter(2 * radius + 1)), gw, gh),
            gw, max_text=0.04 * long_side / step)

    items = []
    for members in filter(None, groups):
        xs, ys = [i % gw for i in members], [i // gw for i in members]
        gx0, gy0, gx1, gy1 = min(xs), min(ys), max(xs) + 1, max(ys) + 1
        ow = gx1 - gx0
        pixels = bytearray(ow * (gy1 - gy0))
        for x, y in zip(xs, ys, strict=True):
            pixels[(y - gy0) * ow + x - gx0] = 255
        own = Image.frombytes("L", (ow, gy1 - gy0), bytes(pixels))
        if whole:  # the soft edges around the solid core belong to the object too
            own = own.filter(ImageFilter.MaxFilter(5))
            gx0, gy0, gx1, gy1 = gx0 - 2, gy0 - 2, gx1 + 2, gy1 + 2
        box = (gx0 * step, gy0 * step, gx1 * step, gy1 * step)
        own = own.resize((box[2] - box[0], box[3] - box[1]), Image.Resampling.NEAREST)
        mine = ImageChops.multiply(alpha.crop(box), own)
        tight = mine.point(lambda v: 255 if v > threshold else 0).getbbox()
        if tight is None:
            continue
        area = sum(mine.point(lambda v: 1 if v > threshold else 0).histogram()[1:])
        if area < max(64, min_area * width * height):
            continue
        x0, y0 = max(0, box[0] + tight[0] - padding), max(0, box[1] + tight[1] - padding)
        x1 = min(width, box[0] + tight[2] + padding)
        y1 = min(height, box[1] + tight[3] + padding)
        if not (whole or keep_full) and (x1 - x0) * (y1 - y0) >= 0.9 * width * height:
            continue
        piece = img.crop((x0, y0, x1, y1))
        keep = own.crop((x0 - box[0], y0 - box[1], x1 - box[0], y1 - box[1]))
        piece.putalpha(ImageChops.multiply(piece.getchannel("A"), keep))
        items.append(((x0, y0, x1 - x0, y1 - y0), area, piece))

    items = sorted(items, key=lambda t: -t[1])[:max_items]
    row = max(1, round(height * 0.02))
    items.sort(key=lambda t: (t[0][1] // row, t[0][0]))
    result = []
    for box, _, piece in items:
        buf = io.BytesIO()
        piece.save(buf, format="PNG")
        result.append((box, buf.getvalue()))
    return result


def _components(grid: Image.Image, gw: int, gh: int) -> list[list[int]]:
    """4-connected regions of a 0/255 grid, as lists of cell indexes."""
    cells = bytearray(grid.tobytes())
    regions = []
    for start in range(gw * gh):
        if cells[start] != 255:
            continue
        cells[start] = 0
        stack, members = [start], []
        while stack:
            i = stack.pop()
            members.append(i)
            x, y = i % gw, i // gw
            for j, ok in ((i - 1, x > 0), (i + 1, x < gw - 1), (i - gw, y > 0),
                          (i + gw, y < gh - 1)):
                if ok and cells[j] == 255:
                    cells[j] = 0
                    stack.append(j)
        regions.append(members)
    return regions


def _merge_text_lines(regions: list[list[int]], gw: int, max_text: float) -> list[list[int]]:
    """Join text-sized regions (height <= max_text cells) that sit on one line with at most
    about one letter height between them: the words of a label."""
    boxes = [(min(i % gw for i in r), min(i // gw for i in r),
              max(i % gw for i in r) + 1, max(i // gw for i in r) + 1) for r in regions]
    parent = list(range(len(regions)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, (ax0, ay0, ax1, ay1) in enumerate(boxes):
        for b, (bx0, by0, bx1, by1) in enumerate(boxes[a + 1:], a + 1):
            ha, hb = ay1 - ay0, by1 - by0
            if max(ha, hb) > max_text:
                continue
            overlap = min(ay1, by1) - max(ay0, by0)
            space = max(bx0 - ax1, ax0 - bx1)
            if overlap >= 0.6 * min(ha, hb) and space <= 1.2 * max(ha, hb):
                parent[find(a)] = find(b)
    merged: dict[int, list[int]] = {}
    for i, r in enumerate(regions):
        merged.setdefault(find(i), []).extend(r)
    return list(merged.values())
