import json
from pathlib import Path

import httpx
import pytest

from dreamer_mcp import score, server
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.workflows import WorkflowRegistry

REPO_WORKFLOWS = Path(__file__).parent.parent / "workflows"
USER_ABC = "X:1\nT:Minha Música\nM:4/4\nL:1/8\nK:G\n%%text VERSO\n| G2 A2 B2 c2 |\nw: la la la la\n"
YUE2_ABC = 'X:1\nT:\nL:1/16\nV: Vocal\nV: Ins\nK:C\n% verse\nV: Vocal\n"C"c16|\nV: Ins\nZ1|\n'


class FakeComfy:
    def __init__(self):
        self.submitted: list[dict] = []
        self.uploads: dict[str, bytes] = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/prompt":
            self.submitted.append(json.loads(req.content)["prompt"])
            return httpx.Response(200, json={"prompt_id": "abcdef0123456789", "number": 1})
        if path == "/upload/image":
            name = req.content.split(b'filename="')[1].split(b'"')[0].decode()
            self.uploads[name] = b"%PDF"
            return httpx.Response(200, json={"name": name, "subfolder": "dreamer-mcp/scores"})
        if path.startswith("/history/"):
            if not self.submitted:
                return httpx.Response(200, json={})
            outputs = {"5": {"text": [YUE2_ABC], "files": [
                {"filename": "abc.md", "subfolder": "m", "type": "output"}]},
                       "13": {"audio": [{"filename": "s.flac", "subfolder": "m", "type": "output"}]}}
            return httpx.Response(200, json={"abcdef0123456789": {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "prompt": [1, "abcdef0123456789", self.submitted[-1]], "outputs": outputs}})
        if path.startswith("/object_info/"):
            return httpx.Response(404)
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
    monkeypatch.setattr(server, "_scores", {})
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    rendered: list[str] = []

    async def render(abc_text: str) -> bytes:
        rendered.append(abc_text)
        return b"%PDF-1.4"

    monkeypatch.setattr(score, "render_pdf", render)
    fake.rendered = rendered
    return fake


async def test_score_of_the_callers_abc_is_made_at_submit(fake):
    out = await server.comfyui_generate_music(style="pagode", lyrics="[Verse]\nla",
                                              abc_notation=USER_ABC)
    result = out[0]
    assert fake.rendered == [USER_ABC]  # the original text (with w: lyrics), not the YuE2 one
    assert result["score"]["filename"].startswith("minha-musica_")
    assert result["score"]["mime_type"] == "application/pdf"
    meta = fake.submitted[0]["5"]["_meta"]["mcp_score"]
    assert meta == {"file": result["score"]["filename"]}

    status = await server.comfyui_job_status("abcdef0123456789")
    scores = [o for o in status["outputs"] if o["label"] == "score"]
    assert [o["filename"] for o in scores] == [result["score"]["filename"]]
    assert len(fake.rendered) == 1  # not rendered again


async def test_score_of_the_workflows_abc_when_the_job_finishes(fake):
    out = await server.comfyui_generate_music(style="pop", lyrics="[Verse]\nla", wait=True)
    result = out[0]
    assert fake.submitted[0]["5"]["_meta"]["mcp_score"] == {"from": "workflow"}
    assert fake.rendered == [YUE2_ABC]
    score_out = next(o for o in result["outputs"] if o["label"] == "score")
    assert score_out["filename"] == "score_abcdef01.pdf"
    assert any(getattr(link, "name", "") == "score_abcdef01.pdf" for link in out[1:])
    await server.comfyui_get_output("abcdef0123456789")
    assert len(fake.rendered) == 1  # cached


async def test_score_errors_do_not_fail_the_job(fake, monkeypatch):
    async def broken(_: str) -> bytes:
        raise score.ScoreError("abcm2ps missing")

    monkeypatch.setattr(score, "render_pdf", broken)
    out = await server.comfyui_generate_music(style="pop", lyrics="x", abc_notation=USER_ABC)
    assert out[0]["score_error"] == "sheet music not created: abcm2ps missing"
    assert out[0]["status"] == "queued"


def test_prepare_and_names():
    assert score.prepare("T:x\nK:C\nabc|").startswith("%abc-2.1\n%%encoding utf-8\nX:1\nT:x")
    with pytest.raises(score.ScoreError):
        score.prepare("X:1\nT:no key\n")
    assert score.slug("Vem Comigo Ver o Sol Nascer (Pagode R&B)") == \
        "vem-comigo-ver-o-sol-nascer-pagode-r-b"
    assert score.slug("") == "score"
