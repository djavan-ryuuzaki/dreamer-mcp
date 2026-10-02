"""Live job progress from ComfyUI's WebSocket: running node, sampling step, percent."""

import asyncio
import json

import httpx
import pytest
import websockets

from dreamer_mcp import progress, server
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.progress import ProgressTracker

PID = "job-1"
GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {}},
    "3": {"class_type": "KSampler", "inputs": {}, "_meta": {"title": "Base sampler"}},
    "4": {"class_type": "VAEDecode", "inputs": {}},
    "5": {"class_type": "SaveImage", "inputs": {}},
}


def msg(kind, **data):
    return {"type": kind, "data": {"prompt_id": PID, **data}}


# Recorded from ComfyUI 0.37 for a prompt queued with the listener's client_id.
START = [
    {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 1}}}},
    msg("execution_start", timestamp=1),
    msg("execution_cached", nodes=["1", "2"]),
    msg("progress_state", nodes={"3": {"value": 0.0, "max": 1.0, "state": "running"}}),
    msg("executing", node="3", display_node="3"),
    msg("progress", value=12, max=30, node="3"),
]


def fed(*messages) -> ProgressTracker:
    t = ProgressTracker(Settings(comfyui_url="http://comfy:8188"), "cid")
    t.register(PID, GRAPH)
    for m in messages:
        t.handle(m)
    return t


def test_running_sampler_reports_node_step_and_percent():
    p = fed(*START).get(PID)
    assert p["node"] == "3" and p["node_type"] == "KSampler" and p["node_title"] == "Base sampler"
    assert (p["step"], p["steps"], p["percent"]) == (12, 30, 40)
    assert (p["nodes_done"], p["nodes_total"]) == (2, 5)  # the two cached ones
    assert p["elapsed_s"] >= 0


def test_moving_to_the_next_node_counts_the_previous_one_and_resets_the_step():
    p = fed(*START, msg("executing", node="4")).get(PID)
    assert p["node_type"] == "VAEDecode" and "step" not in p and "percent" not in p
    assert p["nodes_done"] == 3


@pytest.mark.parametrize("end", [msg("execution_success"), msg("execution_error"),
                                 msg("execution_interrupted"), msg("executing", node=None)])
def test_a_finished_job_has_no_live_progress(end):
    t = fed(*START, end)
    assert t.get(PID) is None and PID not in t.graphs


def test_jobs_this_server_did_not_see_start_have_no_progress():
    assert fed().get("someone-else") is None
    assert fed(msg("progress_state", nodes={})).get(PID) is None


def test_ws_url_follows_the_http_scheme():
    t = ProgressTracker(Settings(comfyui_url="https://comfy.example/sub/"), "abc")
    assert t.ws_url == "wss://comfy.example/sub/ws?clientId=abc"


# --- over a real socket ----------------------------------------------------------------------


@pytest.fixture
async def fake_comfy_ws():
    """A WebSocket server that records how it was reached and plays back queued messages."""
    state = {"paths": [], "auth": [], "conns": [], "outbox": asyncio.Queue()}

    async def handler(ws):
        state["paths"].append(ws.request.path)
        state["auth"].append(ws.request.headers.get("Authorization"))
        state["conns"].append(ws)
        await ws.send(json.dumps({"type": "status", "data": {"sid": "x"}}))
        await ws.send(b"\x00\x00\x00\x01preview")  # binary previews are skipped
        while True:
            m = await state["outbox"].get()
            if m is None:
                await ws.close()
                return
            await ws.send(json.dumps(m))

    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        state["url"] = f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
        yield state
        for _ in state["conns"]:
            state["outbox"].put_nowait(None)  # let the handlers return, or serve() never exits


async def until(cond, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def test_tracker_listens_with_the_client_id_and_api_key(fake_comfy_ws):
    t = ProgressTracker(Settings(comfyui_url=fake_comfy_ws["url"], comfyui_api_key="k"), "cid42")
    t.register(PID, GRAPH)
    try:
        assert await t.ensure_connected()
        assert fake_comfy_ws["paths"] == ["/ws?clientId=cid42"]
        assert fake_comfy_ws["auth"] == ["Bearer k"]
        for m in START:
            fake_comfy_ws["outbox"].put_nowait(m)
        await until(lambda: (t.get(PID) or {}).get("step") == 12)
        assert await t.ensure_connected()  # already open: no second connection
        assert len(fake_comfy_ws["paths"]) == 1
    finally:
        await t.aclose()


async def test_tracker_reconnects_and_drops_stale_state(fake_comfy_ws, monkeypatch):
    monkeypatch.setattr(progress, "RECONNECT_DELAY_MIN", 0.01)
    t = ProgressTracker(Settings(comfyui_url=fake_comfy_ws["url"]), "cid")
    t.register(PID, GRAPH)
    try:
        await t.ensure_connected()
        for m in START:
            fake_comfy_ws["outbox"].put_nowait(m)
        await until(lambda: t.get(PID) is not None)
        fake_comfy_ws["outbox"].put_nowait(None)  # server drops the connection
        await until(lambda: len(fake_comfy_ws["paths"]) == 2)
        assert t.get(PID) is None  # messages may have been missed meanwhile
    finally:
        await t.aclose()


async def test_unreachable_socket_does_not_block_for_long():
    t = ProgressTracker(Settings(comfyui_url="http://127.0.0.1:1"), "cid")
    try:
        assert await t.ensure_connected(timeout=0.2) is False
    finally:
        await t.aclose()


# --- through the MCP tools -------------------------------------------------------------------


@pytest.fixture
def running_job(monkeypatch):
    settings = Settings(comfyui_url="http://comfy:8188", comfyui_progress=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/history/"):
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [[1, PID, {}]], "queue_pending": []})
        return httpx.Response(404)

    client = ComfyClient(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    t = server.tracker()
    t.register(PID, GRAPH)
    for m in START:
        t.handle(m)
    return t


async def test_job_status_includes_live_progress(running_job):
    status = await server.comfyui_job_status(PID)
    assert status["status"] == "running"
    assert status["progress"]["percent"] == 40
    assert server._progress_message(status) == "running, Base sampler, step 12/30 (40%), node 2/5"


async def test_job_status_without_live_progress_stays_as_before(running_job, monkeypatch):
    running_job.jobs.clear()
    status = await server.comfyui_job_status(PID)
    assert status == {"prompt_id": PID, "status": "running"}
    assert server._progress_message(status) == "running"


def test_tracker_is_off_when_disabled(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: Settings(comfyui_progress=False))
    assert server.tracker() is None
