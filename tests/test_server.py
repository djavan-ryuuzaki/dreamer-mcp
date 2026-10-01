import json
from pathlib import Path

import httpx
import pytest

from dreamer_mcp import server
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.workflows import (
    WorkflowError,
    WorkflowRegistry,
    apply_overrides,
    build_graph,
)

WORKFLOWS = Path(__file__).parent / "fixtures"
PROMPT_ID = "abc-123"


class FakeComfy:
    """Minimal in-memory ComfyUI: finishes every submitted prompt immediately."""

    def __init__(self):
        self.submitted: list[dict] = []
        self.history: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={
                "system": {"ram_total": 32 * 1024**3, "ram_free": 16 * 1024**3,
                           "comfyui_version": "0.3.60", "pytorch_version": "2.8.0+cu128"},
                "devices": [{"name": "cuda:0 NVIDIA GeForce RTX 3060 : cudaMallocAsync",
                             "vram_total": 12 * 1024**3, "vram_free": int(3.6 * 1024**3)}],
            })
        if path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        if path == "/prompt":
            graph = json.loads(request.content)["prompt"]
            self.submitted.append(graph)
            self.history[PROMPT_ID] = {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"12": {"text": ["x" * 25000]}, "9": {"images": [
                    {"filename": "img_0001.png", "subfolder": "mcp", "type": "output"},
                    {"filename": "preview.png", "subfolder": "", "type": "temp"},
                ]}},
            }
            return httpx.Response(200, json={"prompt_id": PROMPT_ID, "number": 1, "node_errors": {}})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={pid: self.history[pid]} if pid in self.history else {})
        if path == "/api/userdata":
            return httpx.Response(404)
        return httpx.Response(404, json={"error": f"unexpected {path}"})


@pytest.fixture
def fake(monkeypatch):
    fake = FakeComfy()
    settings = Settings(comfyui_url="http://comfy:8188", comfyui_public_url="https://comfy.example",
                        workflows_dir=WORKFLOWS)
    client = ComfyClient(settings, transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_registry", WorkflowRegistry(client, WORKFLOWS))
    monkeypatch.setattr(server, "_media", None)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return fake


def test_gpu_name():
    assert server._gpu_name("cuda:0 NVIDIA GeForce RTX 3060 : cudaMallocAsync") == "RTX 3060"
    assert server._gpu_name("cuda:0 NVIDIA A100-SXM4-80GB : native") == "A100-SXM4-80GB"


def test_overrides_by_id_and_title():
    graph = json.loads((WORKFLOWS / "txt2img.json").read_text())
    out = apply_overrides(graph, {"6.text": "a cat", "Negative Prompt.text": "dogs",
                                  "KSampler.cfg": 5.0})
    assert out["6"]["inputs"]["text"] == "a cat"
    assert out["7"]["inputs"]["text"] == "dogs"
    assert out["3"]["inputs"]["cfg"] == 5.0
    assert graph["6"]["inputs"]["text"] != "a cat"  # original untouched


def test_overrides_with_dots_and_subgraph_ids():
    graph = {
        "461": {"class_type": "SaveImageAdvanced", "_meta": {"title": "Save v1.2"},
                "inputs": {"format": "png", "format.bit_depth": "8-bit"}},
        "459:452": {"class_type": "TextEncodeQwenImage21", "inputs": {"prompt": ""}},
    }
    out = apply_overrides(graph, {"461.format.bit_depth": "16-bit", "Save v1.2.format": "webp",
                                  "459:452.prompt": "fox"})
    assert out["461"]["inputs"] == {"format": "webp", "format.bit_depth": "16-bit"}
    assert out["459:452"]["inputs"]["prompt"] == "fox"


def test_override_errors():
    graph = json.loads((WORKFLOWS / "txt2img.json").read_text())
    with pytest.raises(WorkflowError, match="ambiguous"):
        apply_overrides(graph, {"CLIPTextEncode.text": "x"})
    with pytest.raises(WorkflowError, match="no input 'txt'"):
        apply_overrides(graph, {"6.txt": "x"})


async def test_build_graph_params(fake):
    reg = server.registry()
    entry = await reg.get("txt2img")
    graph, applied, ignored = await build_graph(
        reg, entry, params={"prompt": "a cat", "steps": "12", "foo": 1}
    )
    assert graph["6"]["inputs"]["text"] == "a cat"
    assert graph["3"]["inputs"]["steps"] == 12
    assert isinstance(applied["seed"], int)  # seed type -> randomized
    assert ignored == ["foo"]


async def test_status(fake):
    status = await server.comfyui_status()
    assert status["running"] is True
    assert status["gpu"] == "RTX 3060"
    assert status["vram_used"] == "8.4 GB"
    assert status["vram_total"] == "12.0 GB"
    assert status["queue"] == 0


async def test_status_when_down(monkeypatch):
    settings = Settings(comfyui_url="http://127.0.0.1:1")
    monkeypatch.setattr(server, "_client", ComfyClient(settings))
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    status = await server.comfyui_status()
    assert status["running"] is False and "error" in status


async def test_generate_image_waits_and_returns_outputs(fake):
    result = await server.comfyui_generate_image(prompt="a red fox", width=768, preview=False)
    info = result[0]
    assert info["status"] == "success"
    assert fake.submitted[0]["6"]["inputs"]["text"] == "a red fox"
    assert fake.submitted[0]["5"]["inputs"]["width"] == 768
    assert [o["filename"] for o in info["outputs"]] == ["img_0001.png"]  # temp excluded
    assert info["outputs"][0]["url"].startswith("https://comfy.example/view?")
    assert info["texts"][0]["node"] == "12" and info["texts"][0]["text"].endswith("[25000 chars]")


async def test_workflows_listing(fake):
    listing = await server.comfyui_workflows()
    names = {w["name"]: w for w in listing["workflows"]}
    assert names["txt2img"]["preset"] == "image"
    assert names["txt2img"]["runnable"] is True
    assert "prompt" in names["txt2img"]["params"]
