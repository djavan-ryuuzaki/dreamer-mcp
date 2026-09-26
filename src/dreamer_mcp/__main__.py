"""Entry point: `dreamer-mcp` / `python -m dreamer_mcp`."""

from __future__ import annotations

import hmac
import logging
import sys

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
    app: ASGIApp = mcp.streamable_http_app(
        streamable_http_path=s.mcp_path,
        stateless_http=True,
        json_response=True,
        host=s.mcp_host,
    )
    if s.mcp_auth_token:
        app = BearerAuthMiddleware(app, s.mcp_auth_token)
    return app


def main() -> None:
    s = get_settings()
    logging.basicConfig(level=s.log_level, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("dreamer_mcp")
    log.info("ComfyUI at %s, workflows in %s", s.comfyui_url, s.workflows_dir.resolve())

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
