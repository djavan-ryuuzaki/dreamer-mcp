"""Runtime configuration, loaded from environment variables (or a local .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGED_WORKFLOWS = Path(__file__).parent / "default_workflows"
"""The repo's workflows/ folder, copied into the wheel (so `uvx dreamer-mcp` has the presets)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- ComfyUI -----------------------------------------------------------------------------
    comfyui_url: str = "http://127.0.0.1:8188"
    """Base URL the MCP server uses to reach ComfyUI (e.g. http://comfyui.ai.svc:8188)."""

    comfyui_public_url: str | None = None
    """URL used in links returned to the user/LLM. Defaults to COMFYUI_URL.
    Useful in Kubernetes, where the internal service URL is not reachable from outside."""

    comfyui_api_key: str | None = None
    """Optional bearer token sent to ComfyUI (for installs behind an authenticating proxy)."""

    comfyui_timeout: float = 30.0
    """Timeout in seconds for individual HTTP requests to ComfyUI."""

    # --- Workflows ---------------------------------------------------------------------------
    workflows_dir: Path = Path("workflows")
    """Directory with API-format workflow JSON files and the optional workflows.yaml manifest."""

    workflows_from_comfyui: bool = True
    """Also list workflows saved in ComfyUI's user directory (user/default/workflows)."""

    default_wait_timeout: int = 300
    """Default seconds to wait for a job when a tool is called with wait=true."""

    preview_max_size: int = 768
    """Maximum width/height (px) of image previews returned inline to the LLM."""

    # --- MCP transport -----------------------------------------------------------------------
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    mcp_auth_token: str | None = None
    """When set, HTTP clients must send `Authorization: Bearer <token>`."""

    mcp_public_url: str | None = None
    """External base URL of this MCP server (e.g. https://dreamer-mcp.example.com). In HTTP mode
    it enables /media: outputs are returned as signed, expiring links streamed through the MCP
    server (with player pages), so ComfyUI itself never needs to be exposed."""

    media_secret: str | None = None
    """HMAC key for media links. Defaults to one derived from MCP_AUTH_TOKEN. Must be the same
    on every replica."""

    media_link_ttl: int = 86400
    """Lifetime of media links in seconds."""

    check_requirements: bool = True
    """Check at startup that ComfyUI has the custom nodes/models/workflows the presets need
    (see `dreamer-mcp requirements`) and log the result."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("comfyui_url", "comfyui_public_url", "mcp_public_url")
    @classmethod
    def _strip_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    @property
    def workflows_path(self) -> Path:
        """WORKFLOWS_DIR, or the workflows shipped in the package when that folder is missing."""
        if self.workflows_dir.is_dir() or not PACKAGED_WORKFLOWS.is_dir():
            return self.workflows_dir
        return PACKAGED_WORKFLOWS

    @property
    def public_url(self) -> str:
        return self.comfyui_public_url or self.comfyui_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
