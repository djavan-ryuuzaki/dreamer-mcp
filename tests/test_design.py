import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from dreamer_mcp import design, imaging
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.workflows import (
    WorkflowRegistry,
    build_graph,
    extract_outputs,
    load_manifest,
    resolve_node,
)

REPO_WORKFLOWS = Path(__file__).parent.parent / "workflows"

CAPTION = {
    "canvas_settings": {"aspect_ratio": "1:1", "ambient_lighting": "soft", "image_style": "flat UI"},
    "layers": [
        {"description": "Plain navy background.", "coordinates": "cx: 0.500, cy: 0.500, w: 1.000, h: 1.000",
         "hierarchy_and_relation": "Base layer.", "color_specs": ["#101830"]},
        {"description": 'Button labelled "PLAY".', "coordinates": {"cx": 0.5, "cy": 0.8, "w": 0.3, "h": 0.1},
         "hierarchy_and_relation": "Centered near the bottom.", "color_specs": ["#FFAA00"]},
    ],
}


def test_caption_gets_the_real_aspect_ratio_and_string_coordinates():
    text, warnings = design.ming_caption(json.dumps(CAPTION), 896, 1152)
    data = json.loads(text)
    assert data["canvas_settings"]["aspect_ratio"] == "7:9, 896 × 1152 px"
    assert data["layers"][1]["coordinates"] == "cx: 0.500, cy: 0.800, w: 0.300, h: 0.100"
    assert warnings == []


def test_caption_warnings():
    bad = json.loads(json.dumps(CAPTION))
    bad["layers"][1]["coordinates"] = "cx: 0.950, cy: 0.5, w: 0.3, h: 0.1"  # leaves the canvas
    bad["layers"][1]["color_specs"] = ["orange"]
    bad["layers"][1]["hierarchy_and_relation"] = 'Below the "PLAY" label.'
    bad["layers"][0]["description"] = 'Navy background with "PLAY" faintly embossed.'
    del bad["canvas_settings"]["image_style"]
    _, warnings = design.ming_caption(json.dumps(bad), 1024, 1024)
    text = "\n".join(warnings)
    assert "leaves the canvas" in text
    assert "hex colors" in text
    assert "hierarchy_and_relation" in text
    assert "more than one description" in text
    assert "image_style" in text


def test_prefix_before_the_caption_is_kept():
    text, warnings = design.ming_caption(
        design.TRANSPARENT_PREFIX + "\n" + json.dumps(CAPTION), 1024, 1024)
    prefix, _, body = text.partition("\n")
    assert prefix == design.TRANSPARENT_PREFIX and "full-canvas" in warnings[0]
    assert json.loads(body)["canvas_settings"]["aspect_ratio"] == "1:1, 1024 × 1024 px"
    assert design.ming_caption("Logo with {curly} braces", 64, 64) == (
        "Logo with {curly} braces", [])


def test_plain_text_and_invalid_json_pass_through():
    assert design.ming_caption("A poster of a cat", 1024, 1024) == ("A poster of a cat", [])
    text, warnings = design.ming_caption("{not json", 1024, 1024)
    assert text == "{not json" and "not valid JSON" in warnings[0]


def test_layers_prompt():
    prompt, n = design.layers_prompt(['Text "GO"', "Layer 2: Blue background"])
    assert n == 2
    assert prompt.splitlines() == [
        "Decompose this image into 2 layers with the following specifications:", "",
        "Number of layers: 2", 'Layer 1: Text "GO"', "Layer 2: Blue background"]
    assert design.layers_prompt(prompt) == (prompt, 2)  # a ready specification is kept
    assert design.layers_prompt(None, 3) == ("Decompose this image into 3 layers.", 3)
    preset, n = design.layers_prompt()
    assert n == 4 and "Layer 1 (text): All designed overlay text." in preset
    assert design.PRESERVE in preset
    with pytest.raises(ValueError):
        design.layers_prompt("Decompose this image into some layers")


def _registry():
    client = ComfyClient(Settings(comfyui_url="http://comfy:8188"),
                         transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    return WorkflowRegistry(client, REPO_WORKFLOWS, include_comfyui=False)


async def test_generate_ui_design_derives_size_and_fills_the_caption():
    reg = _registry()
    entry = load_manifest(REPO_WORKFLOWS).workflows["generate_ui_design"]
    graph, applied, _ = await build_graph(
        reg, entry, {"prompt": json.dumps(CAPTION), "aspect_ratio": "16:9"})
    w, h = (graph[resolve_node(graph, t)]["inputs"]["value"] for t in ("[$WIDTH]", "[$HEIGHT]"))
    assert (w, h) == (1936, 1088)  # 2 MP by default, multiples of 16
    caption = json.loads(graph[resolve_node(graph, "[$PROMPT]")]["inputs"]["value"])
    assert caption["canvas_settings"]["aspect_ratio"] == "121:68, 1936 × 1088 px"
    assert applied["prompt"] == graph[resolve_node(graph, "[$PROMPT]")]["inputs"]["value"]


async def test_edit_ui_design_prunes_unused_reference_slots():
    reg = _registry()
    entry = load_manifest(REPO_WORKFLOWS).workflows["edit_ui_design"]
    graph, _, _ = await build_graph(reg, entry, {"prompt": "Make it red.",
                                                 "images": ["a.png", "logo.png"]})
    loads = sorted(n["_meta"]["title"] for n in graph.values() if n["class_type"] == "LoadImage")
    assert loads == ["[$IMAGEM_0]", "[$IMAGEM_1]"]
    encoder = next(n for n in graph.values() if n["class_type"] == "TextEncodeMingImageEdit")
    assert sorted(k for k in encoder["inputs"] if k.startswith("images.")) == [
        "images.image_1", "images.image_2"]


async def test_decompose_ui_labels_each_layer():
    reg = _registry()
    entry = load_manifest(REPO_WORKFLOWS).workflows["decompose_ui"]
    prompt, n = design.layers_prompt(["Logo", "Background"])
    graph, _, _ = await build_graph(reg, entry, {"prompt": prompt, "num_layers": n,
                                                 "images": ["x.png"]})
    out = resolve_node(graph, "[$OUTPUT]")
    assert graph[resolve_node(graph, "[$LAYERS]")]["inputs"]["layers"] == 2
    history = {"prompt": [0, "id", graph, {}, [out]],
               "outputs": {out: {"images": [{"filename": f"l_{i}.png", "subfolder": "",
                                             "type": "output"} for i in range(3)]}}}
    labels = [f["label"] for f in extract_outputs(history, lambda *a: {})]
    assert labels == ["composite", "layer_01", "layer_02"]


def test_contact_sheet():
    def png(mode, color):
        buf = io.BytesIO()
        Image.new(mode, (200, 100), color).save(buf, format="PNG")
        return buf.getvalue()

    sheet = imaging.contact_sheet([("a", png("RGB", "red")), ("b", png("RGBA", (0, 0, 0, 0)))], 600)
    img = Image.open(io.BytesIO(sheet))
    assert img.format == "JPEG" and img.width <= 600


def _png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_split_alpha_cuts_objects_and_keeps_labels_whole():
    img = Image.new("RGBA", (1000, 600), (0, 0, 0, 0))
    paint = lambda box: img.paste((200, 30, 30, 255), box)
    paint((50, 50, 350, 150))      # button 1
    paint((50, 162, 350, 262))     # button 2, 12 px below (not glued by the gap)
    img.paste((255, 255, 255, 40), (50, 150, 350, 162))  # faint shadow between them
    for x in (500, 530, 560, 610, 640):  # two "words" of 12 px letters on one line
        paint((x, 400, x + 20, 418))
    pieces = imaging.split_alpha(_png(img))
    boxes = [b for b, _ in pieces]
    assert len(pieces) == 3
    assert boxes[0][:2] == (46, 46)  # reading order, 4 px padding
    assert 150 <= boxes[1][1] + 4 <= 162  # separate, keeping part of the shadow as soft edge
    assert boxes[2][0] == 496 and boxes[2][2] == 660 - 500 + 8  # the label is one asset
    assert Image.open(io.BytesIO(pieces[0][1])).mode == "RGBA"
    assert len(imaging.split_alpha(_png(img), whole=True)) == 1
    assert imaging.split_alpha(_png(Image.new("RGB", (64, 64), "red"))) == []


async def test_transparent_prefix_goes_first():
    reg = _registry()
    entry = load_manifest(REPO_WORKFLOWS).workflows["generate_ui_design"]
    graph, applied, _ = await build_graph(
        reg, entry, {"prompt": json.dumps(CAPTION), "transparent": True})
    text = graph[resolve_node(graph, "[$PROMPT]")]["inputs"]["value"]
    assert text.startswith(design.TRANSPARENT_PREFIX + "\n{")
    assert json.loads(text.partition("\n")[2])["canvas_settings"]["aspect_ratio"].endswith("px")
    assert entry.split_alpha["param"] == "transparent" and applied["transparent"] is True
    # the full-canvas background layer would make Ming paint an opaque canvas
    assert len(json.loads(text.partition("\n")[2])["layers"]) == 1
    assert "full-canvas" in applied["prompt_warnings"][0]


def test_whole_layers_by_number():
    from dreamer_mcp.server import _is_whole
    assert _is_whole("layer_03", [3]) and _is_whole("layer_03", ["layer_03"])
    assert not _is_whole("layer_03", [1, 2]) and not _is_whole("layer_03", None)


def test_transparent_fraction():
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (0, 0, 10, 5))
    assert imaging.transparent_fraction(_png(img)) == 0.5
    assert imaging.transparent_fraction(_png(Image.new("RGB", (4, 4)))) == 0.0
