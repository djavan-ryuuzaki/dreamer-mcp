"""Entry point: `dreamer-mcp` / `python -m dreamer_mcp`.

`dreamer-mcp requirements [--offline]` prints the custom nodes/models the presets need (checked
against COMFYUI_URL unless --offline) and exits with 1 when something is missing.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import sys
import threading

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import get_settings

PUBLIC_PATHS = {"/healthz"}
PUBLIC_PREFIXES = ("/media/",)  # protected by signed links instead of the bearer token


class BearerAuthMiddleware:
    """Requires `Authorization: Bearer <MCP_AUTH_TOKEN>` on every HTTP route except health."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (scope["type"] == "http" and path not in PUBLIC_PATHS
                and not path.startswith(PUBLIC_PREFIXES)):
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app() -> ASGIApp:
    from .server import mcp

    s = get_settings()
    # Stateless: every request is independent, so the Deployment can scale to N replicas.
    # Not JSON-only: a tool that waits streams its progress over SSE (see server._wait), which
    # also keeps long generations alive behind proxies. Quick answers still come as plain JSON.
    # Stateless means a `notifications/cancelled` POST cannot reach a running call; a client
    # cancels by closing the response stream, which cancels the tool (and its ComfyUI job).
    app: ASGIApp = mcp.streamable_http_app(
        streamable_http_path=s.mcp_path,
        stateless_http=True,
        json_response=False,
        host=s.mcp_host,
        # Uploads (comfyui_upload_file) carry whole images as base64 in the JSON body.
        max_request_body_size=s.mcp_max_request_mb * 1024 * 1024,
    )
    if s.mcp_auth_token:
        app = BearerAuthMiddleware(app, s.mcp_auth_token)
    return app


async def _check_requirements(offline: bool = False) -> tuple[str, int]:
    from . import requirements
    from .client import ComfyClient

    client = None if offline else ComfyClient(get_settings())
    try:
        report = await requirements.check(client)
    finally:
        if client is not None:
            await client.aclose()
    return requirements.format_report(report), requirements.missing_count(report)


def _log_requirements() -> None:
    log = logging.getLogger("dreamer_mcp.requirements")
    try:
        text, missing = asyncio.run(_check_requirements())
    except Exception as e:  # noqa: BLE001 - never break startup over the check
        log.warning("Requirements check failed: %s", e)
        return
    log.log(logging.WARNING if missing else logging.INFO, "%s", text)


def requirements_command(args: list[str]) -> int:
    text, missing = asyncio.run(_check_requirements(offline="--offline" in args))
    print(text)
    return 1 if missing else 0


def main() -> None:
    if sys.argv[1:2] == ["requirements"]:
        sys.exit(requirements_command(sys.argv[2:]))
    s = get_settings()
    logging.basicConfig(level=s.log_level, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("dreamer_mcp")
    log.info("ComfyUI at %s, workflows in %s", s.comfyui_url, s.workflows_path.resolve())
    if s.check_requirements:
        # In the background: the MCP server starts right away even if ComfyUI is slow or down.
        threading.Thread(target=_log_requirements, name="requirements", daemon=True).start()

    if s.mcp_transport == "stdio":
        from .server import mcp

        mcp.run("stdio")
        return

    import uvicorn

    if not s.mcp_auth_token:
        log.warning("MCP_AUTH_TOKEN is not set: the HTTP endpoint is unauthenticated")
    if s.mcp_public_url:
        log.info("Media links served via %s/media (ttl %ss)", s.mcp_public_url, s.media_link_ttl)
    else:
        log.info("MCP_PUBLIC_URL not set: output links point directly to %s", s.public_url)
    log.info("Serving MCP on http://%s:%s%s", s.mcp_host, s.mcp_port, s.mcp_path)
    uvicorn.run(build_http_app(), host=s.mcp_host, port=s.mcp_port,
                log_level=s.log_level.lower(), proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
