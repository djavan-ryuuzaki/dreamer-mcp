from pathlib import Path

import pytest

from dreamer_mcp.convert import convert_ui_to_api
from dreamer_mcp.workflows import (
    ParamSpec,
    WorkflowEntry,
    WorkflowError,
    WorkflowRegistry,
    build_graph,
    extract_outputs,
    match_choice,
    prune_nodes,
)

OBJECT_INFO = {
    "LoadImage": {"input": {"required": {"image": [["a.png"], {"image_upload": True}]}},
                  "output": ["IMAGE", "MASK"]},
    "PrimitiveStringMultiline": {"input": {"required": {"value": ["STRING", {}]}},
                                 "output": ["STRING"]},
    "KSampler": {"input": {"required": {"seed": ["INT", {"control_after_generate": True}],
                                        "steps": ["INT", {}], "model": ["MODEL"]}},
                 "output": ["LATENT"]},
    "Encode": {"input": {"required": {"prompt": ["STRING", {}], "images": ["COMFY_AUTOGROW_V3", {}]}},
               "output": ["CONDITIONING"]},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {}]}},
                  "output": [], "output_node": True},
    "ImageCompare": {"input": {"required": {"compare_view": ["IMAGECOMPARE", {}]},
                               "optional": {"image_a": ["IMAGE"]}},
                     "output": [], "output_node": True},
}


def _node(id_, type_, *, title=None, mode=0, widgets=None, inputs=(), outputs=()):
    return {"id": id_, "type": type_, "title": title, "mode": mode, "widgets_values": widgets,
            "inputs": list(inputs), "outputs": list(outputs)}


def _ui():
    # 1 LoadImage -> SetNode(REF) ; GetNode(REF) -> 4 Encode.images.image_1
    # 2 LoadImage (bypassed) -> 4 Encode.images.image_2 ; 3 prompt -> Encode.prompt
    # 4 Encode -> 5 SaveImage (via a pretend IMAGE link) ; 6 ImageCompare (UI-only)
    nodes = [
        _node(1, "LoadImage", title="[$IMAGEM_0]", widgets=["cat.png", "image"],
              inputs=[{"name": "image", "widget": {"name": "image"}, "link": None},
                      {"name": "upload", "widget": {"name": "upload"}, "link": None}],
              outputs=[{"name": "IMAGE", "type": "IMAGE"}]),
        _node(2, "LoadImage", title="[$IMAGEM_1]", mode=4, widgets=["dog.png", "image"],
              inputs=[{"name": "image", "widget": {"name": "image"}, "link": None}],
              outputs=[{"name": "IMAGE", "type": "IMAGE"}]),
        _node(3, "PrimitiveStringMultiline", title="[$PROMPT]", widgets=["hello"],
              inputs=[{"name": "value", "widget": {"name": "value"}, "link": None}],
              outputs=[{"name": "STRING", "type": "STRING"}]),
        _node(4, "Encode", widgets=["", ],
              inputs=[{"name": "prompt", "widget": {"name": "prompt"}, "link": 12},
                      {"name": "images.image_1", "type": "IMAGE", "link": 11},
                      {"name": "images.image_2", "type": "IMAGE", "link": 13}],
              outputs=[{"name": "CONDITIONING", "type": "CONDITIONING"}]),
        _node(5, "SaveImage", widgets=["out"],
              inputs=[{"name": "images", "type": "IMAGE", "link": 14},
                      {"name": "filename_prefix", "widget": {"name": "filename_prefix"},
                       "link": None}]),
        _node(6, "ImageCompare", widgets=[], inputs=[{"name": "image_a", "link": 15}]),
        _node(10, "SetNode", widgets=["REF"], inputs=[{"name": "IMAGE", "link": 10}]),
        _node(11, "GetNode", widgets=["REF"], outputs=[{"name": "IMAGE", "type": "IMAGE"}]),
    ]
    links = [[10, 1, 0, 10, 0, "IMAGE"], [11, 11, 0, 4, 1, "IMAGE"], [12, 3, 0, 4, 0, "STRING"],
             [13, 2, 0, 4, 2, "IMAGE"], [14, 4, 0, 5, 0, "IMAGE"], [15, 1, 0, 6, 0, "IMAGE"]]
    return {"nodes": nodes, "links": links}


def test_convert_resolves_set_get_and_bypass():
    graph, warnings = convert_ui_to_api(_ui(), OBJECT_INFO)
    assert graph["4"]["inputs"] == {"prompt": ["3", 0], "images.image_1": ["1", 0]}
    assert graph["1"]["inputs"] == {"image": "cat.png"}  # frontend-only "upload" dropped
    assert graph["1"]["_meta"]["title"] == "[$IMAGEM_0]"
    assert "2" not in graph and "6" not in graph
    assert any("ImageCompare" in w for w in warnings)

    graph, _ = convert_ui_to_api(_ui(), OBJECT_INFO, include_bypassed=True)
    assert graph["4"]["inputs"]["images.image_2"] == ["2", 0]


def test_convert_skips_seed_control_value():
    ui = {"nodes": [_node(1, "KSampler", widgets=[42, "randomize", 20],
                          inputs=[{"name": "seed", "widget": {"name": "seed"}, "link": None},
                                  {"name": "steps", "widget": {"name": "steps"}, "link": None}]),
                    _node(2, "SaveImage", widgets=["x"],
                          inputs=[{"name": "images", "link": 1},
                                  {"name": "filename_prefix", "widget": {"name": "filename_prefix"},
                                   "link": None}])],
          "links": [[1, 1, 0, 2, 0, "IMAGE"]]}
    graph, _ = convert_ui_to_api(ui, OBJECT_INFO)
    assert graph["1"]["inputs"] == {"seed": 42, "steps": 20}


def test_convert_rejects_subgraphs():
    with pytest.raises(WorkflowError, match="subgraphs"):
        convert_ui_to_api({"nodes": [], "links": [], "definitions": {"subgraphs": [{}]}}, {})


def _graph():
    return {
        "1": {"class_type": "LoadImage", "_meta": {"title": "[$IMAGEM_0]"}, "inputs": {"image": "x"}},
        "2": {"class_type": "LoadImage", "_meta": {"title": "[$IMAGEM_1]"}, "inputs": {"image": "x"}},
        "3": {"class_type": "Resize", "inputs": {"input": ["2", 0]}},
        "4": {"class_type": "Encode", "inputs": {"images.image_1": ["1", 0],
                                                  "images.image_2": ["3", 0]}},
        "5": {"class_type": "PrimitiveBoolean", "_meta": {"title": "[$KEEP]"}, "inputs": {"value": True}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
    }


def test_prune_cascades_through_required_inputs_only():
    g = _graph()
    prune_nodes(g, {"2"})
    assert set(g) == {"1", "4", "5", "9"}
    assert g["4"]["inputs"] == {"images.image_1": ["1", 0]}


async def test_file_list_prunes_unused_slots_and_applies_when_missing(tmp_path: Path):
    entry = WorkflowEntry(
        name="edit", file="",
        params={"images": ParamSpec(targets=["[$IMAGEM_0].image", "[$IMAGEM_1].image"],
                                    type="file_list", prune_missing=True)},
        when_missing={"images": {"[$KEEP].value": False}},
    )
    reg = WorkflowRegistry(client=None, workflows_dir=tmp_path, include_comfyui=False)

    g, applied, _ = await build_graph(reg, entry, {"images": ["cat.png"]}, graph=_graph())
    assert applied["images"] == ["cat.png"] and "2" not in g and "3" not in g
    assert g["5"]["inputs"]["value"] is True

    g, _, _ = await build_graph(reg, entry, {}, graph=_graph())
    assert "1" not in g and "2" not in g and g["4"]["inputs"] == {}
    assert g["5"]["inputs"]["value"] is False

    with pytest.raises(WorkflowError, match="at most 2"):
        await build_graph(reg, entry, {"images": ["a", "b", "c"]}, graph=_graph())


class _InfoClient:
    async def object_info(self, node_class=None):
        return {"ResolutionSelector": {"input": {"required": {"aspect_ratio": ["COMBO", {
            "options": ["1:1 (Square)", "3:4 (Portrait Standard)", "16:9 (Widescreen)"]}]}}}}


def _two_outputs():
    return {
        "2": {"class_type": "ResolutionSelector", "_meta": {"title": "[$RESOLUCAO]"},
              "inputs": {"aspect_ratio": "1:1 (Square)"}},
        "5": {"class_type": "PrimitiveBoolean", "_meta": {"title": "[$KEEP]"}, "inputs": {"value": True}},
        "7": {"class_type": "Decode", "inputs": {"size": ["2", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["7", 0]}},
    }


def test_match_choice():
    opts = ["1:1 (Square)", "3:4 (Portrait Standard)", "16:9 (Widescreen)"]
    assert match_choice("16:9", opts) == "16:9 (Widescreen)"
    assert match_choice("square", opts) == "1:1 (Square)"
    assert match_choice("3:4 (portrait standard)", opts) == "3:4 (Portrait Standard)"
    with pytest.raises(WorkflowError, match="Options: 1:1"):
        match_choice("5:4", opts)


async def test_choice_toggle_labels_and_when_set(tmp_path: Path):
    entry = WorkflowEntry(
        name="gen", file="",
        params={
            "aspect_ratio": ParamSpec(targets=["[$RESOLUCAO].aspect_ratio"], type="choice"),
            "keep": ParamSpec(targets=["[$KEEP].value"], type="bool"),
            "upscale": ParamSpec(targets=[], type="bool", default=True, remove_when_false=["9"]),
        },
        when_set={"aspect_ratio": {"[$KEEP].value": False}},
        outputs={"base": "8", "upscale": "9"},
    )
    reg = WorkflowRegistry(client=_InfoClient(), workflows_dir=tmp_path, include_comfyui=False)

    g, applied, _ = await build_graph(reg, entry, {"aspect_ratio": "16:9"}, graph=_two_outputs())
    assert g["2"]["inputs"]["aspect_ratio"] == "16:9 (Widescreen)"
    assert g["5"]["inputs"]["value"] is False  # when_set
    assert g["9"]["_meta"]["mcp_output"] == "upscale" and applied["upscale"] is True

    g, _, _ = await build_graph(reg, entry, {"aspect_ratio": "16:9", "keep": True, "upscale": False},
                                graph=_two_outputs())
    assert g["5"]["inputs"]["value"] is True  # explicit param wins over when_set
    assert "9" not in g and "8" in g

    entry.params["base"] = ParamSpec(targets=[], type="bool", remove_when_false=["8"])
    with pytest.raises(WorkflowError, match="enable at least one"):
        await build_graph(reg, entry, {"upscale": False, "base": False}, graph=_two_outputs())


def test_outputs_carry_labels():
    graph = _two_outputs()
    graph["8"]["_meta"] = {"mcp_output": "base"}
    graph["9"]["_meta"] = {"title": "[$SAIDA_UPSCALE]"}
    entry = {"prompt": [1, "id", graph, {}, []],
             "outputs": {"8": {"images": [{"filename": "a.png"}]},
                         "9": {"images": [{"filename": "b.png"}]}}}
    files = extract_outputs(entry, lambda *a: {})
    assert [f.get("label") for f in files] == ["base", "[$SAIDA_UPSCALE]"]


async def test_append_when_flag_and_remove_when_true(tmp_path: Path):
    entry = WorkflowEntry(name="asset", file="", params={
        "prompt": ParamSpec(targets=["[$KEEP].value"], type="string",
                            append_when={"transparent": "Transparent background."}),
        "transparent": ParamSpec.parse("transparent", {"type": "bool", "default": True}),
        "fast": ParamSpec(targets=[], type="bool", default=False, remove_when_true=["9"]),
    })
    reg = WorkflowRegistry(client=None, workflows_dir=tmp_path, include_comfyui=False)

    g, _, _ = await build_graph(reg, entry, {"prompt": "a chest"}, graph=_two_outputs())
    assert g["5"]["inputs"]["value"] == "a chest. Transparent background."
    assert "9" in g

    g, _, _ = await build_graph(reg, entry, {"prompt": "a chest!", "transparent": False, "fast": True},
                                graph=_two_outputs())
    assert g["5"]["inputs"]["value"] == "a chest!" and "9" not in g


def test_transparent_preview_uses_checkerboard():
    import io

    from PIL import Image as PILImage

    from dreamer_mcp.server import _thumbnail

    src = PILImage.new("RGBA", (64, 64), (255, 0, 255, 0))  # fully transparent magenta
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    thumb = PILImage.open(io.BytesIO(_thumbnail(buf.getvalue(), 64).data))
    r, g, b = thumb.getpixel((2, 2))
    assert abs(r - g) < 10 and abs(g - b) < 10  # grey/white board, not the hidden magenta
