import io
from pathlib import Path

import pytest
from PIL import Image

from dreamer_mcp import imaging
from dreamer_mcp.workflows import (
    ParamSpec,
    WorkflowEntry,
    WorkflowError,
    WorkflowRegistry,
    build_graph,
    parse_file_ref,
)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _sprite(size=(100, 60)) -> Image.Image:
    img = Image.new("RGBA", size, (255, 0, 255, 0))  # transparent magenta background
    img.paste((10, 200, 10, 255), (40, 20, 60, 40))  # 20x20 opaque square
    return img


def test_trim_alpha_crops_to_content_with_padding():
    out = Image.open(io.BytesIO(imaging.trim_alpha(_png(_sprite()), padding=5)))
    assert out.size == (30, 30) and out.mode == "RGBA"
    assert imaging.trim_alpha(_png(Image.new("RGB", (10, 10)))) is None  # no alpha


def test_flatten_hides_transparent_color():
    flat = Image.open(io.BytesIO(imaging.flatten(_sprite(), "#ffffff")))
    assert flat.mode == "RGB" and flat.getpixel((0, 0)) == (255, 255, 255)
    assert flat.getpixel((50, 30)) == (10, 200, 10)


def test_sizes():
    assert imaging.fit_size((3000, 1500), 1536) == (1536, 768)
    assert imaging.fit_size((800, 600), 1536) == (800, 600)
    assert imaging.round_to_multiple(1000, 16) == 1008 and imaging.round_to_multiple(3, 16) == 16
    assert parse_file_ref("dreamer-mcp/a.png [output]") == ("a.png", "dreamer-mcp", "output")
    assert parse_file_ref("b.png") == ("b.png", "", "input")


class _Comfy:
    def __init__(self):
        self.uploads: dict[str, bytes] = {}

    async def view(self, filename, subfolder="", type_="output"):
        class R:
            content = _png(_sprite((1000, 750)))
        return R()

    async def upload(self, data, filename, overwrite=False, type_="input", subfolder=""):
        self.uploads[filename] = data
        return {"name": filename, "subfolder": ""}


async def test_size_from_image_flatten_and_multiple(tmp_path: Path):
    graph = {
        "1": {"class_type": "LoadImage", "_meta": {"title": "[$IMG]"}, "inputs": {"image": "x"}},
        "2": {"class_type": "PrimitiveInt", "_meta": {"title": "[$W]"}, "inputs": {"value": 0}},
        "3": {"class_type": "PrimitiveInt", "_meta": {"title": "[$H]"}, "inputs": {"value": 0}},
        "4": {"class_type": "PrimitiveBoolean", "_meta": {"title": "[$KEEP]"}, "inputs": {"value": True}},
    }
    entry = WorkflowEntry(
        name="asset", file="",
        params={
            "images": ParamSpec(targets=["[$IMG].image"], type="file_list", flatten_alpha="#fff"),
            "width": ParamSpec(targets=["[$W].value"], type="int", default=1024, multiple_of=16),
            "height": ParamSpec(targets=["[$H].value"], type="int", default=1024, multiple_of=16),
        },
        when_set={"width": {"[$KEEP].value": False}},
        size={"from_image": "images", "max_side": 900},
    )
    fake = _Comfy()
    reg = WorkflowRegistry(client=fake, workflows_dir=tmp_path, include_comfyui=False)

    g, applied, _ = await build_graph(reg, entry, {"images": ["sprite.png"]}, graph=graph)
    assert (applied["width"], applied["height"]) == (896, 672)  # 900x675 -> multiples of 16
    assert g["4"]["inputs"]["value"] is False
    uploaded = g["1"]["inputs"]["image"]
    assert uploaded.startswith("mcp_") and Image.open(io.BytesIO(fake.uploads[uploaded])).mode == "RGB"

    g, applied, _ = await build_graph(reg, entry, {"images": ["sprite.png"], "width": 500}, graph=graph)
    assert applied["width"] == 496 and applied["height"] == 1024  # explicit size wins


def test_aspect_ratio_and_megapixels_to_pixels():
    assert imaging.parse_aspect_ratio("16:9 (Widescreen)") == 16 / 9
    assert imaging.parse_aspect_ratio("3x4") == 0.75 and imaging.parse_aspect_ratio("1.5") == 1.5
    assert imaging.size_for(16 / 9, 1.0) == (1365, 768)
    for bad in ("wide", "0:1", "-2"):
        try:
            imaging.parse_aspect_ratio(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


async def test_size_rule_precedence(tmp_path: Path):
    graph = {
        "2": {"class_type": "PrimitiveInt", "_meta": {"title": "[$W]"}, "inputs": {"value": 0}},
        "3": {"class_type": "PrimitiveInt", "_meta": {"title": "[$H]"}, "inputs": {"value": 0}},
        "1": {"class_type": "LoadImage", "_meta": {"title": "[$IMG]"}, "inputs": {"image": "x"}},
    }
    entry = WorkflowEntry(
        name="img", file="",
        params={
            "images": ParamSpec(targets=["[$IMG].image"], type="file_list"),
            "width": ParamSpec(targets=["[$W].value"], type="int", default=1024, multiple_of=16),
            "height": ParamSpec(targets=["[$H].value"], type="int", default=1024, multiple_of=16),
            "aspect_ratio": ParamSpec(targets=[], type="string"),
            "megapixels": ParamSpec(targets=[], type="float"),
        },
        size={"aspect_ratio": "aspect_ratio", "megapixels": "megapixels", "from_image": "images",
              "max_side": 1536},
    )
    reg = WorkflowRegistry(client=_Comfy(), workflows_dir=tmp_path, include_comfyui=False)

    async def size(**params):
        _, applied, ignored = await build_graph(reg, entry, params, graph=graph)
        assert not ignored
        return applied["width"], applied["height"]

    assert await size() == (1024, 1024)                                    # defaults
    assert await size(aspect_ratio="16:9") == (1360, 768)                  # 1 MP
    assert await size(aspect_ratio="1:1", megapixels=2) == (1456, 1456)
    assert await size(images=["a.png"]) == (1008, 752)                     # 1000x750 image
    assert await size(images=["a.png"], megapixels=0.5) == (832, 624)      # image ratio, 0.5 MP
    assert await size(images=["a.png"], aspect_ratio="16:9", width=640) == (640, 1024)  # explicit
    with pytest.raises(WorkflowError, match="aspect ratio"):
        await size(aspect_ratio="wide")
