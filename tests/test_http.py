"""The HTTP app as deployed: a waiting tool streams its progress over SSE."""

import json

from starlette.testclient import TestClient
from test_progress import comfy  # noqa: F401  (fixture)

from dreamer_mcp import __main__ as entry
from dreamer_mcp import server

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def events(body: str) -> list[dict]:
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]


def test_a_waiting_tool_streams_progress_before_its_result(comfy, monkeypatch):  # noqa: F811
    monkeypatch.setattr(entry, "get_settings", server.get_settings)
    with TestClient(entry.build_http_app()) as http:
        init = http.post("/mcp", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "teste", "version": "1"}}})
        assert init.status_code == 200
        call = http.post("/mcp", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "comfyui_generate_image",
                       "arguments": {"prompt": "a fox", "preview": False},
                       "_meta": {"progressToken": 2}}})

    assert call.headers["content-type"].startswith("text/event-stream")
    received = events(call.text)
    progress = [e for e in received if e.get("method") == "notifications/progress"]
    assert progress and all(p["params"]["progressToken"] == 2 for p in progress)
    assert received[-1]["id"] == 2 and "result" in received[-1]
