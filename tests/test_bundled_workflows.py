import json
from pathlib import Path

import httpx

from dreamer_mcp import config
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.workflows import (
    WorkflowRegistry,
    _split_target,
    load_manifest,
    normalize_graph,
    resolve_model_paths,
    resolve_node,
)

REPO_WORKFLOWS = Path(__file__).parent.parent / "workflows"


def _client(handler, **settings):
    return ComfyClient(Settings(comfyui_url="http://comfy:8188", **settings),
                       transport=httpx.MockTransport(handler))


def test_shipped_workflows_match_the_manifest():
    manifest = load_manifest(REPO_WORKFLOWS)
    fallbacks = [e for e in manifest.workflows.values() if e.fallback]
    assert {e.name for e in fallbacks} >= {manifest.presets[k] for k in ("image", "asset",
                                                                          "video", "audio")}
    for entry in fallbacks:
        raw = json.loads((REPO_WORKFLOWS / entry.fallback).read_text(encoding="utf-8"))
        graph = normalize_graph(raw, entry.name)
        for spec in entry.params.values():
            for target in spec.targets:
                _split_target(graph, target)  # raises when the node/title is gone
        for ref in entry.outputs.values():
            resolve_node(graph, ref)
        images = [n["inputs"]["image"] for n in graph.values() if n["class_type"] == "LoadImage"]
        assert set(images) <= {"example.png"}


async def test_fallback_when_not_saved_in_comfyui(tmp_path):
    (tmp_path / "workflows.yaml").write_text(
        'workflows:\n  gen:\n    file: "comfyui:api/gen.json"\n    fallback: gen.json\n',
        encoding="utf-8")
    local = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "local"}}}
    (tmp_path / "gen.json").write_text(json.dumps(local), encoding="utf-8")
    saved: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/userdata/workflows/api/gen.json" and saved:
            return httpx.Response(200, json=saved)
        if req.url.path == "/api/userdata":
            return httpx.Response(200, json=["api/gen.json"] if saved else [])
        return httpx.Response(404)

    reg = WorkflowRegistry(_client(handler), tmp_path)
    entry = await reg.get("gen")
    assert (await reg.load_graph(entry))["1"]["inputs"]["filename_prefix"] == "local"
    assert "gen.json" not in await reg.entries()  # the fallback is not listed on its own

    saved.update({"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "server"}}})
    assert (await reg.load_graph(entry))["1"]["inputs"]["filename_prefix"] == "server"


async def test_model_names_resolve_to_subfolders():
    options = ["QWEN2\\model.safetensors", "other.safetensors", "a/dup.safetensors",
               "b/dup.safetensors"]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/object_info/UNETLoader"
        return httpx.Response(200, json={"UNETLoader": {"input": {"required": {
            "unet_name": [options], "weight_dtype": [["default"]]}}}})

    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "model.safetensors"}},
        "2": {"class_type": "UNETLoader", "inputs": {"unet_name": "other.safetensors"}},
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "dup.safetensors"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "absent.safetensors"}},
    }
    changed = await resolve_model_paths(_client(handler), graph)
    assert changed == {"model.safetensors": "QWEN2\\model.safetensors"}
    assert graph["1"]["inputs"]["unet_name"] == "QWEN2\\model.safetensors"
    assert graph["2"]["inputs"]["unet_name"] == "other.safetensors"   # already valid
    assert graph["3"]["inputs"]["unet_name"] == "dup.safetensors"     # ambiguous
    assert graph["4"]["inputs"]["unet_name"] == "absent.safetensors"  # ComfyUI reports it


def test_packaged_workflows_when_dir_is_missing(tmp_path, monkeypatch):
    packaged = tmp_path / "default_workflows"
    packaged.mkdir()
    monkeypatch.setattr(config, "PACKAGED_WORKFLOWS", packaged)
    assert Settings(workflows_dir=tmp_path / "nope").workflows_path == packaged
    assert Settings(workflows_dir=tmp_path).workflows_path == tmp_path
