"""Live progress of running jobs, from ComfyUI's WebSocket (/ws).

ComfyUI only reports step progress over the WebSocket, and only to the client_id that queued the
prompt: a listener with another id just sees the queue size. So the tracker connects with the
same client_id as ComfyClient.submit and keeps, per prompt_id, the node being executed and its
sampling step. Jobs queued by other clients (or another replica) have no live progress; their
status still comes from /history and /queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import websockets

from .config import Settings

log = logging.getLogger(__name__)

RECONNECT_DELAY_MIN = 1.0
RECONNECT_DELAY_MAX = 30.0
CONNECT_WAIT = 2.0  # seconds a submit waits for the socket, so the job's first messages arrive
MAX_JOBS = 256  # forget the oldest jobs beyond this (finished ones are dropped as they end)
FINISHED = {"execution_success", "execution_error", "execution_interrupted"}


class ProgressTracker:
    def __init__(self, settings: Settings, client_id: str):
        self.settings = settings
        self.client_id = client_id
        self.jobs: dict[str, dict[str, Any]] = {}
        self.graphs: dict[str, dict] = {}  # prompt_id -> API graph, for node types and counts
        self._connected = asyncio.Event()
        self._task: asyncio.Task | None = None

    # --- state -------------------------------------------------------------------------------

    def register(self, prompt_id: str, graph: dict) -> None:
        """Remember the graph of a job this server queued (node class names, node count)."""
        self.graphs[prompt_id] = graph
        while len(self.graphs) > MAX_JOBS:
            self.graphs.pop(next(iter(self.graphs)))

    def handle(self, message: dict[str, Any]) -> None:
        """Apply one WebSocket message (already JSON-decoded) to the job state."""
        kind, data = message.get("type"), message.get("data") or {}
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return
        if kind in FINISHED:
            self.jobs.pop(prompt_id, None)
            self.graphs.pop(prompt_id, None)
            return
        job = self.jobs.get(prompt_id)
        if job is None:
            if kind not in ("execution_start", "execution_cached", "executing", "progress"):
                return
            job = self.jobs[prompt_id] = {"started": time.monotonic(), "cached": set(),
                                          "executed": [], "node": None, "step": None}
            while len(self.jobs) > MAX_JOBS:
                self.jobs.pop(next(iter(self.jobs)))
        if kind == "execution_cached":
            job["cached"].update(data.get("nodes") or [])
        elif kind == "executing":
            node = data.get("node")
            if node is None:  # older ComfyUI: end of the prompt
                self.jobs.pop(prompt_id, None)
                self.graphs.pop(prompt_id, None)
                return
            if node != job["node"]:
                job["node"], job["step"] = node, None
                if node not in job["executed"]:
                    job["executed"].append(node)
        elif kind == "progress":
            node = data.get("node") or job["node"]
            if node is not None and node != job["node"]:
                job["node"] = node
                if node not in job["executed"]:
                    job["executed"].append(node)
            job["step"] = (data.get("value"), data.get("max"))

    def get(self, prompt_id: str) -> dict[str, Any] | None:
        """Progress of a running job, or None when this server has nothing live for it."""
        job = self.jobs.get(prompt_id)
        if job is None:
            return None
        out: dict[str, Any] = {"elapsed_s": round(time.monotonic() - job["started"], 1)}
        graph = self.graphs.get(prompt_id) or {}
        node = job["node"]
        if node is not None:
            out["node"] = node
            spec = graph.get(node) or {}
            if spec.get("class_type"):
                out["node_type"] = spec["class_type"]
            title = (spec.get("_meta") or {}).get("title")
            if title and title != spec.get("class_type"):
                out["node_title"] = title
        if job["step"]:
            value, maximum = job["step"]
            out["step"], out["steps"] = value, maximum
            if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum:
                out["percent"] = round(100 * value / maximum)
        done = len(job["cached"]) + max(len(job["executed"]) - 1, 0)
        out["nodes_done"] = done
        if graph:
            out["nodes_total"] = len(graph)
        return out

    # --- connection --------------------------------------------------------------------------

    @property
    def ws_url(self) -> str:
        base = self.settings.comfyui_url
        scheme, rest = base.split("://", 1) if "://" in base else ("http", base)
        return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/ws?clientId={self.client_id}"

    async def ensure_connected(self, timeout: float = CONNECT_WAIT) -> bool:
        """Start the listener if needed and wait (briefly) until the socket is open."""
        loop = asyncio.get_running_loop()
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._connected = asyncio.Event()
            self._task = loop.create_task(self._run(), name="comfyui-ws")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        headers = {}
        if self.settings.comfyui_api_key:
            headers["Authorization"] = f"Bearer {self.settings.comfyui_api_key}"
        delay = RECONNECT_DELAY_MIN
        while True:
            try:
                async with websockets.connect(self.ws_url, additional_headers=headers,
                                              max_size=None, open_timeout=10) as ws:
                    self._connected.set()
                    delay = RECONNECT_DELAY_MIN
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            continue  # latent previews
                        try:
                            self.handle(json.loads(raw))
                        except (ValueError, TypeError, AttributeError) as e:
                            log.debug("Ignoring WebSocket message: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep retrying, status falls back to polling
                log.debug("ComfyUI WebSocket: %s", e)
            self._connected.clear()
            self.jobs.clear()  # messages were missed; stale steps would mislead
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
