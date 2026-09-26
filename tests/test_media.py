import time

import httpx
import pytest

from dreamer_mcp import server
from dreamer_mcp.__main__ import BearerAuthMiddleware
from dreamer_mcp.client import ComfyClient
from dreamer_mcp.config import Settings
from dreamer_mcp.media import MediaLinks, MediaSigner

VIDEO = bytes(range(256)) * 40  # 10 KiB fake mp4


def fake_view(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/view"
    if request.url.params["filename"] != "clip.mp4":
        return httpx.Response(404)
    if rng := request.headers.get("range"):
        start, end = rng.removeprefix("bytes=").split("-")
        start, end = int(start), int(end or len(VIDEO) - 1)
        return httpx.Response(206, content=VIDEO[start:end + 1], headers={
            "content-range": f"bytes {start}-{end}/{len(VIDEO)}", "accept-ranges": "bytes"})
    return httpx.Response(200, content=VIDEO, headers={"content-type": "application/octet-stream",
                                                       "accept-ranges": "bytes"})


@pytest.fixture
def app(monkeypatch):
    settings = Settings(comfyui_url="http://comfy:8188", mcp_transport="http",
                        mcp_public_url="https://mcp.example", mcp_auth_token="tok")
    client = ComfyClient(settings, transport=httpx.MockTransport(fake_view))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_media", MediaLinks(settings, client,
                                                     MediaSigner.from_settings(settings)))
    asgi = BearerAuthMiddleware(server.mcp.streamable_http_app(), "tok")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="https://mcp.example")


def test_signer_roundtrip_tamper_and_expiry():
    signer = MediaSigner(b"k")
    token = signer.sign("a.mp4", "video", "output", ttl=60)
    ref = signer.verify(token)
    assert (ref.filename, ref.subfolder, ref.type) == ("a.mp4", "video", "output")
    assert MediaSigner(b"other").verify(token) is None
    payload, sig = token.split(".")
    assert signer.verify(payload[:-2] + "xx." + sig) is None
    assert signer.verify(signer.sign("a.mp4", "", "output", ttl=-1)) is None
    assert ref.expires > time.time()


def test_links_fallback_to_comfyui_without_public_url():
    s = Settings(comfyui_url="http://comfy:8188")
    links = MediaLinks(s, ComfyClient(s), signer=None).links("a.flac", "audio", "output")
    assert links == {"url": "http://comfy:8188/view?filename=a.flac&subfolder=audio&type=output"}


async def test_stream_with_range_and_player(app):
    links = server.media().links("clip.mp4", "video", "output")
    assert links["url"].startswith("https://mcp.example/media/") and links["url"].endswith("/clip.mp4")

    full = await app.get(links["url"])  # no bearer needed: the signed link is the credential
    assert full.status_code == 200 and full.content == VIDEO
    assert full.headers["content-type"] == "video/mp4"

    part = await app.get(links["url"], headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == VIDEO[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(VIDEO)}"

    page = await app.get(links["player"])
    assert page.status_code == 200 and '<video src="./clip.mp4"' in page.text


async def test_rejects_bad_links_and_keeps_mcp_protected(app):
    links = server.media().links("clip.mp4", "video", "output")
    assert (await app.get(links["url"].replace("/media/", "/media/x"))).status_code == 403
    assert (await app.post("/mcp", json={})).status_code == 401
