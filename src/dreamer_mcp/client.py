"""Thin async client for the ComfyUI HTTP API."""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .config import Settings


class ComfyUIError(Exception):
    """Raised when ComfyUI is unreachable or rejects a request."""


class ComfyClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client_id = uuid.uuid4().hex
        headers = {}
        if settings.comfyui_api_key:
            headers["Authorization"] = f"Bearer {settings.comfyui_api_key}"
        self._http = httpx.AsyncClient(
            base_url=settings.comfyui_url,
            headers=headers,
            timeout=settings.comfyui_timeout,
            transport=transport,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- low level ---------------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise ComfyUIError(f"Cannot reach ComfyUI at {self.settings.comfyui_url}: {e}") from e
        if resp.status_code >= 400:
            detail: Any = resp.text
            try:
                detail = resp.json()
            except ValueError:
                pass
            raise ComfyUIError(f"ComfyUI {method} {path} -> HTTP {resp.status_code}: {detail}")
        return resp

    async def _get_json(self, path: str, **params: Any) -> Any:
        return (await self._request("GET", path, params=params or None)).json()

    async def _post(self, path: str, payload: Any = None) -> httpx.Response:
        return await self._request("POST", path, json=payload)

    # --- API ---------------------------------------------------------------------------------

    async def system_stats(self) -> dict:
        return await self._get_json("/system_stats")

    async def queue(self) -> dict:
        return await self._get_json("/queue")

    async def history(self, prompt_id: str | None = None, max_items: int | None = None) -> dict:
        if prompt_id:
            return await self._get_json(f"/history/{prompt_id}")
        params = {"max_items": max_items} if max_items else {}
        return await self._get_json("/history", **params)

    async def submit(self, graph: dict) -> dict:
        """Queue an API-format workflow. Returns {"prompt_id", "number", "node_errors"}."""
        try:
            resp = await self._post("/prompt", {"prompt": graph, "client_id": self.client_id})
        except ComfyUIError as e:
            raise ComfyUIError(f"Workflow rejected by ComfyUI: {e}") from e
        return resp.json()

    async def interrupt(self, prompt_id: str | None = None) -> None:
        await self._post("/interrupt", {"prompt_id": prompt_id} if prompt_id else {})

    async def delete_from_queue(self, prompt_ids: list[str]) -> None:
        await self._post("/queue", {"delete": prompt_ids})

    async def clear_queue(self) -> None:
        await self._post("/queue", {"clear": True})

    async def model_folders(self) -> list[str]:
        return await self._get_json("/models")

    async def models(self, folder: str) -> list[str]:
        return await self._get_json(f"/models/{quote(folder)}")

    async def object_info(self, node_class: str | None = None) -> dict:
        path = f"/object_info/{quote(node_class)}" if node_class else "/object_info"
        return await self._get_json(path)

    async def view(self, filename: str, subfolder: str = "", type_: str = "output") -> httpx.Response:
        params = {"filename": filename, "subfolder": subfolder, "type": type_}
        return await self._request("GET", "/view", params=params)

    async def upload(self, data: bytes, filename: str, overwrite: bool = False,
                     type_: str = "input", subfolder: str = "") -> dict:
        """Upload a file into ComfyUI's input (or output/temp) dir. Works for images, audio
        and video."""
        files = {"image": (filename, data)}
        form = {"type": type_, "subfolder": subfolder, "overwrite": "true" if overwrite else "false"}
        resp = await self._request("POST", "/upload/image", files=files, data=form)
        return resp.json()

    async def free_memory(self, unload_models: bool = True, free_memory: bool = True) -> None:
        await self._post("/free", {"unload_models": unload_models, "free_memory": free_memory})

    async def list_user_workflows(self) -> list[str]:
        try:
            return await self._get_json("/api/userdata", dir="workflows", recurse="true")
        except ComfyUIError:
            return []  # dir does not exist yet

    async def get_user_workflow(self, relpath: str) -> Any:
        return await self._get_json(f"/api/userdata/{quote('workflows/' + relpath, safe='')}")

    async def save_user_workflow(self, relpath: str, data: Any, overwrite: bool = False) -> None:
        path = f"/api/userdata/{quote('workflows/' + relpath, safe='')}"
        params = {"overwrite": "true" if overwrite else "false", "full_info": "false"}
        try:
            await self._request("POST", path, params=params,
                                content=json.dumps(data, indent=2).encode(),
                                headers={"Content-Type": "application/json"})
        except ComfyUIError as e:
            if "HTTP 409" in str(e):
                raise ComfyUIError(f"workflows/{relpath} already exists (use overwrite=true)") from e
            raise

    async def fetch_url(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=self.settings.comfyui_timeout, follow_redirects=True) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            return resp.content

    # --- helpers -----------------------------------------------------------------------------

    def view_url(self, filename: str, subfolder: str = "", type_: str = "output") -> str:
        query = urlencode({"filename": filename, "subfolder": subfolder, "type": type_})
        return f"{self.settings.public_url}/view?{query}"
