import pytest

from dreamer_mcp import server


@pytest.fixture(autouse=True)
def _no_progress_socket(monkeypatch):
    """Tests talk to a mocked HTTP ComfyUI: no WebSocket unless a test turns it on."""
    monkeypatch.setenv("COMFYUI_PROGRESS", "false")
    monkeypatch.setattr(server, "_tracker", None)
