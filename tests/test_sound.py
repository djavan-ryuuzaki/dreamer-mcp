import io
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile

from dreamer_mcp import server, sound
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.workflows import WorkflowRegistry

REPO_WORKFLOWS = Path(__file__).parent.parent / "workflows"
PROMPT_ID = "5ound0123456789"


def _flac(seconds: float = 0.25, rate: int = 44100) -> bytes:
    t = np.linspace(0, seconds, int(seconds * rate), endpoint=False)
    tone = 0.3 * np.sin(2 * np.pi * 440 * t)
    out = io.BytesIO()
    soundfile.write(out, np.stack([tone, tone], axis=1), rate, format="FLAC")
    return out.getvalue()


class FakeComfy:
    def __init__(self):
        self.submitted: list[dict] = []
        self.uploads: dict[str, bytes] = {}
        self.views = 0
        self.master = _flac()

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/prompt":
            self.submitted.append(json.loads(req.content)["prompt"])
            return httpx.Response(200, json={"prompt_id": PROMPT_ID, "number": 1})
        if path == "/view":
            self.views += 1
            return httpx.Response(200, content=self.master)
        if path == "/upload/image":
            name = req.content.split(b'filename="')[1].split(b'"')[0].decode()
            part = req.content.split(b'filename="')[1]
            body = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            self.uploads[name] = body
            return httpx.Response(200, json={"name": name, "subfolder": "audio"})
        if path.startswith("/history/"):
            if not self.submitted:
                return httpx.Response(200, json={})
            graph = self.submitted[-1]
            ext = graph["8"]["inputs"]["format"]
            outputs = {"8": {"audio": [
                {"filename": f"stable_audio_3_00001_.{ext}", "subfolder": "audio",
                 "type": "output"}]}}
            return httpx.Response(200, json={PROMPT_ID: {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "prompt": [1, PROMPT_ID, graph], "outputs": outputs}})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    fake = FakeComfy()
    settings = Settings(comfyui_url="http://comfy:8188", workflows_dir=REPO_WORKFLOWS,
                        workflows_from_comfyui=False)
    client = ComfyClient(settings, transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_registry", WorkflowRegistry(client, REPO_WORKFLOWS, False))
    monkeypatch.setattr(server, "_media", None)
    monkeypatch.setattr(server, "_converted", {})
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return fake


def test_prompt_gets_the_category_tag_and_the_length():
    assert sound.sound_prompt("Glass bottle shattering on concrete", "sfx", 2.4) == (
        "TrackType: SFX, Glass bottle shattering on concrete. Length: 2 seconds")
    assert sound.sound_prompt("Lo-fi beat with Rhodes. BPM: 80.", "music", 90) == (
        "TrackType: Music, VocalType: Instrumental, Lo-fi beat with Rhodes. BPM: 80. "
        "Length: 90 seconds")
    assert sound.sound_prompt("Dry clap", "one_shot", 0.5).endswith("Dry clap. Length: 1 second")
    # what the caller already wrote is kept
    assert sound.sound_prompt("TrackType: Instrument, Format: Duo, cello. Length: 30 seconds",
                              "instrument", 12) == (
        "TrackType: Instrument, Format: Duo, cello. Length: 30 seconds")
    with pytest.raises(ValueError, match="Invalid category"):
        sound.sound_prompt("x", "song", 3)
    with pytest.raises(ValueError, match="empty"):
        sound.sound_prompt("  ", "sfx", 3)


async def test_native_format_goes_to_the_save_node(fake):
    out = await server.comfyui_generate_sound(prompt="Sword swing whoosh", category="sfx",
                                              duration=2, format="opus", seed=7)
    graph = fake.submitted[0]
    assert graph["3"]["inputs"]["text"] == "TrackType: SFX, Sword swing whoosh. Length: 2 seconds"
    assert graph["5"]["inputs"]["seconds"] == 2.0
    assert graph["6"]["inputs"]["seed"] == 7
    assert graph["8"]["inputs"]["format"] == "opus"
    assert graph["8"]["inputs"]["format.quality"] == "192k"
    assert "mcp_convert" not in graph["8"]["_meta"]
    assert out[0]["applied"]["prompt"].startswith("TrackType: SFX")


async def test_default_is_mp3_and_flac_has_no_quality(fake):
    await server.comfyui_generate_sound(prompt="Kick", category="one_shot", duration=1)
    await server.comfyui_generate_sound(prompt="Kick", category="one_shot", duration=1,
                                        format="flac")
    assert fake.submitted[0]["8"]["inputs"]["format"] == "mp3"
    assert fake.submitted[0]["8"]["inputs"]["format.quality"] == "V0"
    assert "format.quality" not in fake.submitted[1]["8"]["inputs"]


@pytest.mark.parametrize("fmt, magic", [("wav", b"RIFF"), ("ogg", b"OggS"), ("aiff", b"FORM")])
async def test_converted_formats(fake, fmt, magic):
    out = await server.comfyui_generate_sound(prompt="Rain on a tin roof", category="sfx",
                                              duration=10, format=fmt, wait=True)
    graph = fake.submitted[0]
    assert graph["8"]["inputs"]["format"] == "flac"
    assert graph["8"]["_meta"]["mcp_convert"] == {"format": fmt}
    [audio] = out[0]["outputs"]
    assert audio["label"] == "audio"
    assert audio["filename"] == f"stable_audio_3_00001_.{fmt}"
    assert audio["converted_from"] == "stable_audio_3_00001_.flac"
    assert audio["mime_type"].startswith("audio/")
    data = fake.uploads[audio["filename"]]
    assert data.startswith(magic)
    assert soundfile.info(io.BytesIO(data)).channels == 2

    status = await server.comfyui_job_status(PROMPT_ID)  # converted once, then cached
    assert [o["filename"] for o in status["outputs"]] == [audio["filename"]]
    assert fake.views == 1


async def test_bad_duration_is_a_tool_error(fake):
    with pytest.raises(server.ToolError, match="between 0.5 and 380"):
        await server.comfyui_generate_sound(prompt="x", category="sfx", duration=0)
    assert fake.submitted == []


async def test_sound_tool_describes_the_prompting_guide():
    tools = {t.name: t.description or "" for t in await server.mcp.list_tools()}
    text = tools["comfyui_generate_sound"]
    assert text.startswith("Generate a sound with Stable Audio 3")
    for piece in ("there is no prompt", "one_shot (a single sample", "BPM: X", "wav/ogg"):
        assert piece in text, piece
