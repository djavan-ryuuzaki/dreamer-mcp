"""Long generations: progress while waiting, and cancelling the ComfyUI job with the MCP call."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from mcp import Client

from dreamer_mcp import server
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.workflows import WorkflowRegistry

WORKFLOWS = Path(__file__).parent / "fixtures"
PROMPT_ID = "slow-1"


class SlowComfy:
    """In-memory ComfyUI where a prompt waits in the queue, then runs, for a number of polls each."""

    def __init__(self, pending_polls: int = 2, running_polls: int = 2):
        self.pending_polls = pending_polls
        self.running_polls = running_polls
        self.polls = 0
        self.interrupted: list[dict] = []
        self.deleted: list[list[str]] = []
        self.finished = False

    def state(self) -> str:
        if self.finished:
            return "done"
        if self.polls <= self.pending_polls:
            return "pending"
        if self.polls <= self.pending_polls + self.running_polls:
            return "running"
        return "done"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt" and request.method == "POST":
            return httpx.Response(200, json={"prompt_id": PROMPT_ID, "number": 1, "node_errors": {}})
        if path.startswith("/history/"):
            self.polls += 1
            if self.state() != "done":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={PROMPT_ID: {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"9": {"images": [{"filename": "img.png", "subfolder": "", "type": "output"}]}},
            }})
        if path == "/queue" and request.method == "GET":
            item = [1, PROMPT_ID, {}]
            state = self.state()
            return httpx.Response(200, json={
                "queue_running": [item] if state == "running" else [],
                "queue_pending": [[0, "other", {}], item] if state == "pending" else [],
            })
        if path == "/queue" and request.method == "POST":
            self.deleted.append(json.loads(request.content)["delete"])
            return httpx.Response(200, json={})
        if path == "/interrupt":
            self.interrupted.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if path == "/api/userdata":
            return httpx.Response(404)
        return httpx.Response(404, json={"error": f"unexpected {path}"})


@pytest.fixture
def comfy(monkeypatch):
    comfy = SlowComfy()
    settings = Settings(comfyui_url="http://comfy:8188", comfyui_public_url="https://comfy.example",
                        workflows_dir=WORKFLOWS)
    client = ComfyClient(settings, transport=httpx.MockTransport(comfy.handler))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_registry", WorkflowRegistry(client, WORKFLOWS))
    monkeypatch.setattr(server, "_media", None)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "POLL_DELAY_MAX", 0.05)  # keep the tests fast
    return comfy


async def test_waiting_reports_queue_position_then_running_to_the_mcp_client(comfy):
    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, message))

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "comfyui_generate_image", {"prompt": "a fox", "preview": False}, progress_callback=on_progress)

    assert not result.is_error
    messages = [m for _, m in seen]
    assert "queued, position 2 of 2" in messages
    assert "running" in messages
    progress = [p for p, _ in seen]
    assert progress == sorted(progress) and len(set(progress)) == len(progress)  # always increasing


async def test_cancelling_a_running_call_interrupts_its_comfyui_job(comfy):
    comfy.running_polls = 10_000  # runs until cancelled
    task = asyncio.create_task(server.comfyui_generate_image(prompt="a fox", preview=False))
    while comfy.state() != "running":
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert comfy.interrupted == [{"prompt_id": PROMPT_ID}]


async def test_cancelling_a_queued_call_removes_it_from_the_queue(comfy):
    comfy.pending_polls = 10_000  # stays in the queue until cancelled
    task = asyncio.create_task(server.comfyui_generate_image(prompt="a fox", preview=False))
    while comfy.polls < 1:
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert comfy.deleted == [[PROMPT_ID]]
    assert comfy.interrupted == []


async def test_local_workflows_skip_hidden_folders(tmp_path, comfy):
    (tmp_path / "txt2img.json").write_text((WORKFLOWS / "txt2img.json").read_text())
    hidden = tmp_path / "..2026_09_28_18_50_22.609605339"  # how Kubernetes mounts a ConfigMap
    hidden.mkdir()
    (hidden / "txt2img.json").write_text((WORKFLOWS / "txt2img.json").read_text())

    names = set((await WorkflowRegistry(server.client(), tmp_path).entries()).keys())

    assert "txt2img" in names
    assert not any(name.startswith(".") for name in names)
