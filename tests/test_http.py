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


def _upload(http: TestClient, size: int):
    """A tools/call of comfyui_upload_file whose JSON body is a little over `size` bytes."""
    return http.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "comfyui_upload_file", "arguments": {"source": "A" * size}}})


def test_a_body_over_the_sdk_default_of_4_mib_is_accepted(comfy, monkeypatch):  # noqa: F811
    monkeypatch.setattr(entry, "get_settings", server.get_settings)
    with TestClient(entry.build_http_app()) as http:
        response = _upload(http, 5 * 1024 * 1024)
    assert response.status_code != 413


def test_a_body_over_mcp_max_request_mb_is_refused_with_413(comfy, monkeypatch):  # noqa: F811
    settings = server.get_settings()
    monkeypatch.setattr(entry, "get_settings", lambda: settings)
    monkeypatch.setattr(settings, "mcp_max_request_mb", 1)
    with TestClient(entry.build_http_app()) as http:
        response = _upload(http, 2 * 1024 * 1024)
    assert response.status_code == 413
