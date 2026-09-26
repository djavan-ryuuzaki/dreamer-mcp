"""Signed, expiring media links served by the MCP server itself.

`/media/<token>/<filename>` streams a ComfyUI output (with HTTP Range support, so video/audio
players can seek) and `/media/<token>/player` wraps it in a minimal HTML player. The token is
an HMAC over (type, subfolder, filename, expiry): players and <video> tags cannot send the MCP
bearer token, so the link itself is the credential. ComfyUI never has to be exposed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import secrets
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse

from .client import ComfyClient
from .config import Settings

log = logging.getLogger(__name__)

MEDIA_PREFIX = "/media/"
PASSTHROUGH_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges",
                       "last-modified", "etag")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("video/webm", ".webm")


def mime_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def media_kind(filename: str) -> str:
    return mime_type(filename).split("/")[0]  # image | video | audio | application | text


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


@dataclass(frozen=True)
class MediaRef:
    filename: str
    subfolder: str
    type: str
    expires: int


class MediaSigner:
    def __init__(self, secret: bytes):
        self._secret = secret

    @classmethod
    def from_settings(cls, s: Settings) -> MediaSigner:
        if s.media_secret:
            return cls(s.media_secret.encode())
        if s.mcp_auth_token:
            return cls(hashlib.sha256(b"comfyui-mcp-media:" + s.mcp_auth_token.encode()).digest())
        log.warning("Neither MEDIA_SECRET nor MCP_AUTH_TOKEN is set: media links will break "
                    "on restart and will not work across replicas")
        return cls(secrets.token_bytes(32))

    def _sig(self, payload: str) -> str:
        return _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()[:18])

    def sign(self, filename: str, subfolder: str, type_: str, ttl: int) -> str:
        payload = _b64(json.dumps([type_, subfolder, filename, int(time.time()) + ttl],
                                  separators=(",", ":")).encode())
        return f"{payload}.{self._sig(payload)}"

    def verify(self, token: str) -> MediaRef | None:
        payload, _, sig = token.partition(".")
        if not sig or not hmac.compare_digest(sig, self._sig(payload)):
            return None
        try:
            type_, subfolder, filename, expires = json.loads(_unb64(payload))
        except (ValueError, TypeError):
            return None
        if expires < time.time():
            return None
        return MediaRef(filename=filename, subfolder=subfolder, type=type_, expires=expires)


class MediaLinks:
    """Builds the links returned by tools: proxied+signed when MCP_PUBLIC_URL is set (HTTP mode),
    otherwise direct ComfyUI /view URLs."""

    def __init__(self, settings: Settings, client: ComfyClient, signer: MediaSigner | None):
        self.settings = settings
        self.client = client
        self.signer = signer

    @property
    def proxied(self) -> bool:
        return self.signer is not None and bool(self.settings.mcp_public_url)

    def links(self, filename: str, subfolder: str = "", type_: str = "output") -> dict[str, str]:
        if not self.proxied:
            return {"url": self.client.view_url(filename, subfolder, type_)}
        token = self.signer.sign(filename, subfolder, type_, self.settings.media_link_ttl)
        base = f"{self.settings.mcp_public_url}{MEDIA_PREFIX}{token}"
        result = {"url": f"{base}/{quote(PurePosixPath(filename).name)}"}
        if media_kind(filename) in ("video", "audio", "image"):
            result["player"] = f"{base}/player"
        return result


# --- HTTP handlers ---------------------------------------------------------------------------


def make_media_handler(signer: MediaSigner, client: ComfyClient):
    async def handler(request: Request) -> Response:
        ref = signer.verify(request.path_params["token"])
        if ref is None:
            return PlainTextResponse("Link invalid or expired", status_code=403)
        if request.path_params["name"] == "player":
            return _player(request, ref)

        headers = {h: request.headers[h] for h in ("range", "if-range") if h in request.headers}
        headers["accept-encoding"] = "identity"  # keep byte ranges / content-length exact
        req = client._http.build_request(
            "GET", "/view",
            params={"filename": ref.filename, "subfolder": ref.subfolder, "type": ref.type},
            headers=headers, timeout=httpx.Timeout(30.0, read=None),
        )
        try:
            upstream = await client._http.send(req, stream=True)
        except httpx.HTTPError as e:
            return PlainTextResponse(f"ComfyUI unavailable: {e}", status_code=502)
        if upstream.status_code >= 400:
            await upstream.aclose()
            return PlainTextResponse("File not found", status_code=upstream.status_code)

        out_headers = {h: upstream.headers[h] for h in PASSTHROUGH_HEADERS if h in upstream.headers}
        out_headers["content-type"] = mime_type(ref.filename)  # ComfyUI may send octet-stream
        out_headers["content-disposition"] = f'inline; filename="{PurePosixPath(ref.filename).name}"'
        out_headers["cache-control"] = "private, max-age=3600"
        if request.method == "HEAD":
            await upstream.aclose()
            return Response(status_code=upstream.status_code, headers=out_headers)
        return StreamingResponse(upstream.aiter_bytes(), status_code=upstream.status_code,
                                 headers=out_headers, background=BackgroundTask(upstream.aclose))

    return handler


def _player(request: Request, ref: MediaRef) -> HTMLResponse:
    name = PurePosixPath(ref.filename).name
    src = html.escape(f"./{quote(name)}")
    kind = media_kind(ref.filename)
    if kind == "video":
        media = f'<video src="{src}" controls autoplay playsinline loop></video>'
    elif kind == "audio":
        media = f'<audio src="{src}" controls autoplay></audio>'
    elif kind == "image":
        media = f'<img src="{src}" alt="{html.escape(name)}">'
    else:
        media = f'<p><a href="{src}">{html.escape(name)}</a></p>'
    expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ref.expires))
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(name)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
         justify-content: center; gap: 16px; background: #111; color: #ddd;
         font: 14px/1.4 system-ui, sans-serif; padding: 16px; box-sizing: border-box; }}
  video, img {{ max-width: 100%; max-height: 80vh; border-radius: 8px; background: #000; }}
  audio {{ width: min(640px, 100%); }}
  a {{ color: #8ab4f8; }}
  .meta {{ opacity: .7; text-align: center; word-break: break-all; }}
</style></head>
<body>
  {media}
  <div class="meta">{html.escape(name)} · <a href="{src}" download>download</a><br>
  link valid until {expires}</div>
</body></html>"""
    return HTMLResponse(page, headers={"cache-control": "no-store"})
