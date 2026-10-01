import httpx

from dreamer_mcp import requirements
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings


def _everything(spec):
    classes, models, flows = set(), {}, set()
    for g in spec["groups"].values():
        for n in g.get("nodes", []):
            classes.update(n["classes"])
        for m in g.get("models", []):
            models.setdefault(m["folder"], []).append(m["file"])
        flows.update(g.get("workflows", []))
    return classes, models, flows


def _client(classes, models, flows, workflows_dir="workflows"):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.37.0"}})
        if path == "/object_info":
            return httpx.Response(200, json={c: {} for c in classes})
        if path == "/api/userdata":
            return httpx.Response(200, json=sorted(flows))
        if path.startswith("/models/"):
            folder = path.removeprefix("/models/")
            if folder not in models:
                return httpx.Response(404)
            return httpx.Response(200, json=models[folder])
        return httpx.Response(404)

    return ComfyClient(Settings(comfyui_url="http://comfy:8188", workflows_dir=workflows_dir),
                       transport=httpx.MockTransport(handler))


def test_bundled_list_is_well_formed():
    spec = requirements.load()
    assert set(spec["groups"]) == {"image", "video", "audio"}
    for g in spec["groups"].values():
        for m in g["models"]:
            assert {"file", "folder", "url", "size_gb"} <= set(m)
            assert m["url"].startswith("https://")


async def test_all_installed_models_in_subfolders():
    classes, models, flows = _everything(requirements.load())
    # Windows-style subfolders (e.g. "QWEN2\\model.safetensors") still count.
    models = {k: [f"SUB\\{f}" for f in v] for k, v in models.items()}
    report = await requirements.check(_client(classes, models, flows))
    assert report["comfyui"]["version"] == "0.37.0"
    assert all(g["ready"] for g in report["groups"])
    assert requirements.missing_count(report) == 0
    assert "MISSING" not in requirements.format_report(report)


async def test_missing_items_and_optional_nodes(tmp_path):
    classes, models, flows = _everything(requirements.load())
    classes -= {"easy ifElse", "INTConstant"}  # INTConstant = optional KJNodes
    models.pop("checkpoints")                    # folder unknown to ComfyUI -> 404
    flows.discard("api/minimax_h3_r2v.json")
    # tmp_path: no shipped copies of the workflows to fall back to
    report = await requirements.check(_client(classes, models, flows, tmp_path))
    ready = {g["name"]: g["ready"] for g in report["groups"]}
    assert ready == {"image": False, "video": False, "audio": False}
    # easy ifElse counts twice: image and audio both need ComfyUI-Easy-Use
    assert requirements.missing_count(report) == 4
    image = next(g for g in report["groups"] if g["name"] == "image")
    easy = next(n for n in image["nodes"] if n["package"] == "ComfyUI-Easy-Use")
    assert easy["missing_classes"] == ["easy ifElse"]
    text = requirements.format_report(report)
    assert "[optional]" in text
    assert "https://huggingface.co/Comfy-Org/YuE2/" in text


async def test_unreachable_and_offline():
    offline = await requirements.check(None, ["audio"])
    assert [g["name"] for g in offline["groups"]] == ["audio"]
    assert offline["groups"][0]["ready"] is None

    down = await requirements.check(ComfyClient(Settings(comfyui_url="http://127.0.0.1:1")))
    assert down["comfyui"]["reachable"] is False
    assert "not checked" in requirements.format_report(down)
    assert requirements.missing_count(down) == 0


async def test_workflow_missing_in_comfyui_but_bundled():
    classes, models, _ = _everything(requirements.load())
    report = await requirements.check(_client(classes, models, set()))  # repo's workflows/
    assert all(g["ready"] for g in report["groups"])
    statuses = {w["status"] for g in report["groups"] for w in g["workflows"]}
    assert statuses == {requirements.BUNDLED}
    assert "[bundled]" in requirements.format_report(report)
