"""Prompts for Ming Image 0.1 Design: Figma-style captions and layer-decomposition requests.

Ming Image renders UI screens, banners and posters best from a structured JSON caption (the
format its prompt enhancer writes), and decomposes a flattened design into RGBA layers from a
"Decompose this image into N layers" specification. The MCP client is the prompt enhancer here:
the tool descriptions carry the rules below, and this module checks and completes what it
writes (e.g. the canvas aspect ratio, which must match the real width and height).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

# The text-to-image rewriter system prompt released with Ming Image (assets/
# t2i_rewriter_system_prompt.txt in github.com/inclusionAI/Ming-Image).
CAPTION_GUIDE = """\
Write `caption` as a precise, high-resolution Figma-style caption: one JSON object.

Use exactly two top-level keys. `canvas_settings` contains exactly `aspect_ratio`, \
`ambient_lighting`, and `image_style`. `layers` lists visible groups from background to topmost \
overlay. Every layer contains exactly `description`, `coordinates`, `hierarchy_and_relation`, and \
`color_specs`; `color_specs` is an array of hex colors.

`coordinates` MUST be one string, never an object or array, in exactly this form: \
`"cx: 0.500, cy: 0.500, w: 1.000, h: 1.000"`. Values are normalized; each bbox encloses its \
complete owned object and stays inside the canvas.

A layer is one selectable visible semantic group: background, full person, coherent object, \
panel, card, row, or text block. Prefer the fewest groups that preserve the layout. Keep people \
and objects intact. Never create invisible parents, guides, placeholders, empty layers, duplicate \
summaries, or multiple owners for one element.

Preserve every user-supplied rendered string character-for-character and as one contiguous \
string. Unless multiple visible copies are requested, it must occur exactly once across all \
`description` fields and zero times in `hierarchy_and_relation`. Quote it only where describing \
its visible rendering; refer to the related subject elsewhere with unquoted semantic wording. \
Enumerate intended copy, invent extra copy sparingly, and never hide content behind "other \
text", "remaining labels", or "etc."

Describe concrete composition, typography, materials, texture, lighting, pose, and camera \
treatment without literary filler. Use `hierarchy_and_relation` only for ownership, alignment, \
containment, stacking, and occlusion.

Infer structured layouts first. Use one complete layer per card and state its row and column. A \
compact secondary table may be one layer only if every header and cell is listed; otherwise use a \
visible shared frame when present, one complete header, and one complete layer per body row, \
binding values to columns and stating blanks. Enumerate sequences, schedules, spans, gaps, and \
vacant tracks in visual order. Do not mistake ordinary alignment for a table.

Silently verify schema, string coordinates, Z-order, exact-text counts, geometry, bbox validity, \
and completeness. `canvas_settings.aspect_ratio` is filled in from the real width/height."""

# The layer-decomposition prompt enhancer released with Ming Image (GUIDED_PROMPT in its README).
LAYERS_GUIDE = """\
How to write `layers` (look at the image first, e.g. with comfyui_view_image):
- Layer 1 is the FRONT-most (topmost); the last layer is the background/environment. Stacking \
the layers back-to-front must reproduce the image.
- For each layer, write a concrete one-or-two-sentence description grounded in the image: real \
colors, positions and shapes.
- TEXT goes in the front layer(s); quote any real text VERBATIM in double quotes and keep its \
original language (do not translate).
- A text-supporting CARD / PANEL / BADGE / BANNER / BUTTON plate is its OWN layer directly behind \
the text - do not merge it into the text layer or into the background.
- The MAIN SUBJECT (hero product/photo/illustration/character) is its own layer.
- The BACKGROUND/ENVIRONMENT is the LAST layer and ABSORBS supporting props and surfaces under \
the subject (tables, boards, floors, shadows, gradients, patterns) - these are NOT separate layers.
- Write directly about the content; do not mention "image" or narrate reasoning."""

# The default request of Ming's ling-ui-design skill: four coarse layers (front to back) that
# separate rendered text, artwork, UI chrome and the empty canvas.
DEFAULT_LAYERS = [
    "(text): All designed overlay text.",
    ("(image): All photographs and illustrations, complete and text-free, including pictures "
     "that appear inside cards."),
    ("(container): Empty UI chrome: navigation bars, buttons, badges, ribbons, colored section "
     "banners, and empty card or panel frames. Keep their colors and shapes. Do not include "
     "photographs."),
    "(background): Remaining empty page canvas only. No photographs. No overlay text.",
]
PRESERVE = ("Preserve the source composition and geometry exactly. Keep reusable photographs and "
            "illustrations high-detail with clean boundaries; do not resize, reposition, blur, or "
            "simplify them.")

# One of the fixed phrases Ming was trained on for RGBA output; it goes at the very start.
TRANSPARENT_PREFIX = "RGBA, 4-channel, transparent background"

_COORDS = re.compile(
    r"^\s*cx:\s*(-?[\d.]+)\s*,\s*cy:\s*(-?[\d.]+)\s*,\s*w:\s*(-?[\d.]+)\s*,\s*h:\s*(-?[\d.]+)\s*$")
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_QUOTED = re.compile(r'"([^"]+)"')
_LAYER_KEYS = ("description", "coordinates", "hierarchy_and_relation", "color_specs")
_CANVAS_KEYS = ("aspect_ratio", "ambient_lighting", "image_style")
EPS = 0.005


def aspect_label(width: int, height: int) -> str:
    """896x1152 -> "7:9, 896 × 1152 px" (the form Ming's own captions use)."""
    g = math.gcd(width, height) or 1
    return f"{width // g}:{height // g}, {width} × {height} px"


def caption_text(caption: Any) -> str:
    """A caption given as a JSON object (dict) or as text -> the text sent to the model."""
    if isinstance(caption, dict):
        return json.dumps(caption, ensure_ascii=False)
    return str(caption)


def _coords(value: Any, where: str, warnings: list[str]) -> str:
    if isinstance(value, dict):
        value = [value.get(k) for k in ("cx", "cy", "w", "h")]
    if isinstance(value, list | tuple) and len(value) == 4:
        try:
            value = "cx: {:.3f}, cy: {:.3f}, w: {:.3f}, h: {:.3f}".format(*map(float, value))
        except (TypeError, ValueError):
            pass
    m = _COORDS.match(str(value))
    if not m:
        warnings.append(f'{where}: coordinates should read "cx: 0.500, cy: 0.500, w: 1.000, '
                        f'h: 1.000", got {value!r}')
        return str(value)
    cx, cy, w, h = map(float, m.groups())
    if w <= 0 or h <= 0 or cx - w / 2 < -EPS or cy - h / 2 < -EPS or cx + w / 2 > 1 + EPS \
            or cy + h / 2 > 1 + EPS:
        warnings.append(f"{where}: bbox {value!r} is empty or leaves the canvas")
    return str(value)


def _full_canvas(layer: Any) -> bool:
    if not isinstance(layer, dict):
        return False
    m = _COORDS.match(_coords(layer.get("coordinates", ""), "", []))
    return bool(m) and float(m.group(3)) >= 0.95 and float(m.group(4)) >= 0.95


def ming_caption(caption: Any, width: Any = None, height: Any = None) -> tuple[str, list[str]]:
    """Check a Figma-style caption and set its aspect_ratio from the real size. Plain-text
    prompts are passed through (Ming also accepts them). Returns (caption text, warnings)."""
    text = caption_text(caption)
    # A short fixed prefix (e.g. TRANSPARENT_PREFIX) may precede the JSON object.
    start = text.find("{")
    prefix, body = (text[:start], text[start:]) if start >= 0 else (text, "")
    if not body or len(prefix) > 200:
        return text, []
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        if prefix.strip():
            return text, []  # plain text that happens to contain a brace
        return text, [f"caption is not valid JSON ({e}); sent as plain text"]
    if not isinstance(data, dict):
        return text, []

    warnings: list[str] = []
    extra = sorted(set(data) - {"canvas_settings", "layers"})
    if extra:
        warnings.append(f"unexpected top-level keys: {', '.join(extra)}")
    canvas = data.get("canvas_settings")
    if not isinstance(canvas, dict):
        canvas = data["canvas_settings"] = {}
        warnings.append("missing canvas_settings")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        canvas["aspect_ratio"] = aspect_label(width, height)
    missing = [k for k in _CANVAS_KEYS if k not in canvas]
    if missing:
        warnings.append(f"canvas_settings is missing {', '.join(missing)}")

    layers = data.get("layers")
    if prefix.strip() == TRANSPARENT_PREFIX and isinstance(layers, list):
        # A full-canvas layer (even an "empty transparent background") makes Ming paint an
        # opaque canvas: a transparent design lists only its objects.
        kept = [lay for lay in layers if not _full_canvas(lay)]
        if len(kept) < len(layers):
            warnings.append(f"removed {len(layers) - len(kept)} full-canvas background layer(s): "
                            "a transparent design lists only its objects")
            layers = data["layers"] = kept
    if not isinstance(layers, list) or not layers:
        warnings.append("layers should be a non-empty list, background first")
        layers = []
    descriptions, relations = [], []
    for i, layer in enumerate(layers, 1):
        where = f"layer {i}"
        if not isinstance(layer, dict):
            warnings.append(f"{where} is not an object")
            continue
        missing = [k for k in _LAYER_KEYS if k not in layer]
        if missing:
            warnings.append(f"{where} is missing {', '.join(missing)}")
        if "coordinates" in layer:
            layer["coordinates"] = _coords(layer["coordinates"], where, warnings)
        colors = layer.get("color_specs")
        if colors is not None and (not isinstance(colors, list) or not all(
                isinstance(c, str) and _HEX.match(c) for c in colors)):
            warnings.append(f"{where}: color_specs should be a list of hex colors")
        descriptions.append(str(layer.get("description", "")))
        relations.append(str(layer.get("hierarchy_and_relation", "")))

    # Every rendered string once across the descriptions, never in hierarchy_and_relation.
    quoted = [q for d in descriptions for q in _QUOTED.findall(d)]
    for q in sorted({q for q in quoted if quoted.count(q) > 1}):
        warnings.append(f'"{q}" is quoted in more than one description (it may render twice)')
    for q in sorted({q for r in relations for q in _QUOTED.findall(r)}):
        warnings.append(f'"{q}" is quoted in hierarchy_and_relation (keep rendered text in '
                        "descriptions only)")
    prefix = prefix.strip()
    return (f"{prefix}\n" if prefix else "") + json.dumps(data, ensure_ascii=False), warnings


_COUNT = re.compile(r"(?:Number of layers:\s*|into\s+)(\d+)(?:\s+layers)?", re.IGNORECASE)


def layers_prompt(layers: list[str] | str | None = None, num_layers: int | None = None
                  ) -> tuple[str, int]:
    """Build the decomposition request. `layers` (front-most first) gives one description per
    layer; a ready "Decompose this image into N layers..." text is used as is; with only
    `num_layers` the model plans the layers itself; with neither, the default 4-layer request
    (text, image, container, background). Returns (prompt, layer count)."""
    if isinstance(layers, str):
        text = layers.strip()
        if text.lower().startswith("decompose"):
            m = _COUNT.search(text)
            if not m:
                raise ValueError('the decomposition text must say "Number of layers: N"')
            return text, int(m.group(1))
        layers = [line for line in text.splitlines() if line.strip()]
    if not layers and num_layers:
        return f"Decompose this image into {int(num_layers)} layers.", int(num_layers)
    preset = not layers
    items = DEFAULT_LAYERS if preset else [
        re.sub(r"^\s*Layer\s*\d+\s*:\s*", "", str(d)).strip() for d in layers]
    n = len(items)
    lines = [f"Decompose this image into {n} layers with the following specifications:", "",
             f"Number of layers: {n}"]
    if preset:
        lines.append(PRESERVE)
    sep = " " if preset else ": "  # the preset reads "Layer 1 (text): ..."
    lines += [f"Layer {i}{sep}{d}" for i, d in enumerate(items, 1)]
    return "\n".join(lines), n
