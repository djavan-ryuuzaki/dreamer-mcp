"""MCP tools exposed for ComfyUI."""

from __future__ import annotations

import asyncio
import functools
import io
import re
import time
import uuid
from pathlib import PurePosixPath
from typing import Any, Literal

import anyio
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ResourceLink, ToolAnnotations
from PIL import Image as PILImage
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from . import __version__, abc, imaging, requirements, score
from .client import ComfyClient, ComfyUIError
from .config import get_settings
from .convert import convert_ui_to_api
from .media import MEDIA_PREFIX, MediaLinks, MediaSigner, make_media_handler, media_kind
from .workflows import (
    WorkflowEntry,
    WorkflowError,
    WorkflowRegistry,
    build_graph,
    editable_inputs,
    extract_error,
    extract_outputs,
    extract_texts,
    normalize_graph,
    param_choices,
    upload_input_file,
)

INSTRUCTIONS = """\
Tools for a remote ComfyUI server.
Typical flow: comfyui_workflows -> comfyui_workflow_info (see params/inputs) -> comfyui_run
(wait=false returns a prompt_id) -> comfyui_job_status -> comfyui_get_output / comfyui_view_image.
For everyday tasks prefer the presets: comfyui_generate_image, comfyui_generate_asset, comfyui_upscale,
comfyui_generate_video, comfyui_generate_music (songs, covers), comfyui_song_to_abc.
Generations can take minutes: for video/audio prefer wait=false and poll comfyui_job_status.
If a preset fails because a node or model is missing, comfyui_requirements lists what to install.
"""

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DONE = {"success", "error", "interrupted"}
POLL_DELAY_MAX = 3.0  # seconds between polls of a job, so progress goes out at least this often
NON_MODEL_FOLDERS = {"custom_nodes"}
SAVED_WORKFLOWS_SUBDIR = "api"
SCORES_SUBFOLDER = "dreamer-mcp/scores"
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

mcp = MCPServer(name="dreamer-mcp", instructions=INSTRUCTIONS, version=__version__)

_client: ComfyClient | None = None
_registry: WorkflowRegistry | None = None
_media: MediaLinks | None = None
_scores: dict[str, dict] = {}  # prompt_id -> score output rendered from the workflow's ABC


def client() -> ComfyClient:
    global _client
    if _client is None:
        _client = ComfyClient(get_settings())
    return _client


def registry() -> WorkflowRegistry:
    global _registry
    if _registry is None:
        s = get_settings()
        _registry = WorkflowRegistry(client(), s.workflows_path, s.workflows_from_comfyui)
    return _registry


def media() -> MediaLinks:
    global _media
    if _media is None:
        s = get_settings()
        signer = MediaSigner.from_settings(s) if s.mcp_transport == "http" else None
        _media = MediaLinks(s, client(), signer)
    return _media


def tool_errors(fn):
    """Turn expected failures into ToolErrors with a readable message for the LLM."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ComfyUIError, WorkflowError) as e:
            raise ToolError(str(e)) from e

    return wrapper


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route(MEDIA_PREFIX + "{token}/{name}", methods=["GET", "HEAD"])
async def media_route(request: Request) -> Response:
    m = media()
    if not m.proxied:
        return PlainTextResponse("Media proxy disabled (set MCP_PUBLIC_URL)", status_code=404)
    return await make_media_handler(m.signer, client())(request)


# --- helpers ---------------------------------------------------------------------------------


def _gb(n: float | None) -> str | None:
    return None if n is None else f"{n / 1024**3:.1f} GB"


def _gpu_name(raw: str) -> str:
    # "cuda:0 NVIDIA GeForce RTX 3060 : cudaMallocAsync" -> "RTX 3060"
    name = re.sub(r"^\w+:\d+\s+", "", raw)
    name = re.sub(r"\s+:\s+.*$", "", name)
    return re.sub(r"^(NVIDIA\s+)?(GeForce\s+)?", "", name).strip() or raw


def _thumbnail(data: bytes, max_size: int) -> Image:
    img = PILImage.open(io.BytesIO(data))
    img.seek(0)
    img.thumbnail((max_size, max_size))
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        img = _on_checkerboard(img.convert("RGBA"))
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


def _on_checkerboard(img: PILImage.Image, cell: int = 16) -> PILImage.Image:
    """Show transparency the way image editors do, so it survives the JPEG preview."""
    board = PILImage.new("RGB", img.size, (255, 255, 255))
    grey = PILImage.new("RGB", (cell, cell), (204, 204, 204))
    for y in range(0, img.height, cell):
        for x in range((y // cell) % 2 * cell, img.width, 2 * cell):
            board.paste(grey, (x, y))
    board.paste(img, (0, 0), img)
    return board


def _is_image(filename: str) -> bool:
    return PurePosixPath(filename).suffix.lower() in IMAGE_EXTS


def _resource_links(outputs: list[dict]) -> list[ResourceLink]:
    """Typed links so MCP clients that support it can render a player/download."""
    return [
        ResourceLink(
            uri=o["url"], name=o["filename"], mime_type=o["mime_type"],
            description=f"Player: {o['player']}" if "player" in o else None,
        )
        for o in outputs
        if media_kind(o["filename"]) in ("video", "audio") or o["mime_type"] == "application/pdf"
    ]


async def _job_status(prompt_id: str, include_temp: bool = False,
                      scores: bool = False) -> dict[str, Any]:
    """`scores`: once the job succeeded, add its sheet music (rendered on first request)."""
    c = client()
    history = await c.history(prompt_id)
    if prompt_id in history:
        entry = history[prompt_id]
        status = entry.get("status") or {}
        messages = [m[0] for m in status.get("messages") or [] if isinstance(m, list)]
        if "execution_interrupted" in messages:
            state = "interrupted"
        elif status.get("status_str") == "error":
            state = "error"
        else:
            state = "success" if status.get("completed", True) else "running"
        result: dict[str, Any] = {"prompt_id": prompt_id, "status": state}
        if state == "error":
            result["error"] = extract_error(entry)
        result["outputs"] = extract_outputs(entry, media().links, include_temp)
        if texts := extract_texts(entry):
            result["texts"] = texts
        if scores and state == "success":
            out = await _workflow_score(prompt_id, entry)
            if isinstance(out, str):
                result["score_error"] = out
            elif out:
                result["outputs"].append(out)
        return result

    q = await c.queue()
    if any(item[1] == prompt_id for item in q.get("queue_running", [])):
        return {"prompt_id": prompt_id, "status": "running"}
    pending = sorted(q.get("queue_pending", []), key=lambda item: item[0])
    for pos, item in enumerate(pending, start=1):
        if item[1] == prompt_id:
            return {"prompt_id": prompt_id, "status": "pending", "queue_position": pos,
                    "queue_size": len(pending)}
    return {"prompt_id": prompt_id, "status": "not_found"}


def _progress_message(status: dict[str, Any]) -> str:
    if status["status"] == "pending":
        position = status.get("queue_position")
        total = status.get("queue_size")
        return f"queued, position {position} of {total}" if total else f"queued, position {position}"
    return status["status"]


async def _cancel_job(prompt_id: str) -> dict[str, Any]:
    """Remove a job from the queue if pending, interrupt it if running."""
    c = client()
    status = await _job_status(prompt_id)
    if status["status"] == "pending":
        await c.delete_from_queue([prompt_id])
        return {"prompt_id": prompt_id, "cancelled": True, "was": "pending"}
    if status["status"] == "running":
        await c.interrupt(prompt_id)
        return {"prompt_id": prompt_id, "cancelled": True, "was": "running"}
    return {"prompt_id": prompt_id, "cancelled": False, "status": status["status"]}


async def _wait(prompt_id: str, timeout: int, ctx: Context | None = None) -> dict[str, Any]:
    """Poll the job until it is done or `timeout` passes. Every poll is reported as MCP progress
    (seconds waited, with the queue position or state), which also keeps the response stream alive
    through proxies. If the MCP call is cancelled (notifications/cancelled or the client going
    away), the ComfyUI job is cancelled too."""
    started = time.monotonic()
    deadline = started + timeout
    delay = 0.5
    try:
        while True:
            status = await _job_status(prompt_id, scores=True)
            if status["status"] in DONE or time.monotonic() >= deadline:
                if status["status"] not in DONE:
                    status["note"] = (
                        f"Still {status['status']} after {timeout}s; poll comfyui_job_status later."
                    )
                return status
            if ctx is not None:
                await ctx.report_progress(round(time.monotonic() - started, 3),
                                          message=_progress_message(status))
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, POLL_DELAY_MAX)
    except anyio.get_cancelled_exc_class():
        with anyio.CancelScope(shield=True):
            try:
                await _cancel_job(prompt_id)
            except ComfyUIError:
                pass  # the call is over either way
        raise


async def _previews(outputs: list[dict], limit: int = 4) -> list[Image]:
    size = get_settings().preview_max_size
    images = []
    for out in [o for o in outputs if _is_image(o["filename"])][:limit]:
        resp = await client().view(out["filename"], out["subfolder"], out["type"])
        try:
            images.append(_thumbnail(resp.content, size))
        except OSError:
            continue
    return images


async def _execute(
    workflow: str | None,
    *,
    params: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    workflow_json: dict | None = None,
    randomize_seed: bool = True,
    wait: bool = False,
    timeout: int | None = None,
    preview: bool = False,
    ctx: Context | None = None,
) -> list[Any]:
    reg = registry()
    if workflow_json is not None:
        entry, graph = WorkflowEntry(name="inline", file=""), normalize_graph(workflow_json, "inline")
    elif workflow:
        entry, graph = await reg.get(workflow), None
    else:
        raise ToolError("Provide either 'workflow' (a name from comfyui_workflows) or 'workflow_json'.")

    graph, applied, ignored = await build_graph(
        reg, entry, params=params, overrides=inputs, randomize=randomize_seed, graph=graph
    )
    score_out = await _prepare_score(entry, graph, params or {})
    submitted = await client().submit(graph)
    prompt_id = submitted["prompt_id"]
    result: dict[str, Any] = {"workflow": entry.name, "prompt_id": prompt_id}
    if isinstance(score_out, str):
        result["score_error"] = score_out
    elif score_out and not wait:
        result["score"] = score_out
    if applied:
        result["applied"] = applied
    if ignored:
        result["ignored_params"] = ignored
        result["hint"] = "These params are not mapped in workflows.yaml for this workflow."

    if not wait:
        result["status"] = "queued"
        return [result]

    result.update(await _wait(prompt_id, timeout or get_settings().default_wait_timeout, ctx))
    if result.get("status") == "success" and entry.trim_alpha:
        result["outputs"] = result.get("outputs", []) + await _trim_outputs(
            entry, applied, result.get("outputs", []))
    content: list[Any] = [result]
    if result.get("status") == "success":
        content.extend(_resource_links(result.get("outputs", [])))
        if preview:
            content.extend(await _previews(result.get("outputs", [])))
    return content


def _score_output(node: str, filename: str) -> dict[str, Any]:
    return {"node": node, "label": "score", "kind": "score", "filename": filename,
            "subfolder": SCORES_SUBFOLDER, "type": "output", "mime_type": "application/pdf",
            **media().links(filename, SCORES_SUBFOLDER, "output")}


async def _save_score(abc_text: str, tag: str) -> str:
    """Render the ABC to PDF and store it in ComfyUI's output dir. Returns the file name."""
    pdf = await score.render_pdf(abc_text)
    name = f"{score.slug(score.title(abc_text))}_{tag}.pdf"
    saved = await client().upload(pdf, name, overwrite=True, type_="output",
                                  subfolder=SCORES_SUBFOLDER)
    return saved.get("name", name)


async def _prepare_score(entry: WorkflowEntry, graph: dict, params: dict[str, Any]
                         ) -> dict[str, Any] | str | None:
    """Workflows with `score`: when the caller gave the ABC, render that text now (it keeps
    lyrics and layout the caller wrote); otherwise mark the job so the ABC the workflow writes
    is rendered when it finishes. The choice is stored in the submitted graph (_meta), so later
    comfyui_job_status / comfyui_get_output calls know it too. Returns the score output, an
    error message, or None."""
    cfg = entry.score
    node = next((nid for nid, n in graph.items()
                 if (n.get("_meta") or {}).get("mcp_output") == cfg.get("output")), None)
    if not cfg or node is None:
        return None
    meta = graph[node].setdefault("_meta", {})
    given = params.get(cfg.get("param", ""))
    if not (isinstance(given, str) and given.strip()):
        meta["mcp_score"] = {"from": "workflow"}
        return None
    try:
        filename = await _save_score(given, uuid.uuid4().hex[:8])
    except (score.ScoreError, ComfyUIError, OSError) as e:  # never block the music itself
        return f"sheet music not created: {e}"
    meta["mcp_score"] = {"file": filename}
    return _score_output(node, filename)


async def _workflow_score(prompt_id: str, entry: dict) -> dict[str, Any] | str | None:
    prompt = entry.get("prompt") or []
    graph = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else {}
    node, how = next(((nid, n["_meta"]["mcp_score"]) for nid, n in graph.items()
                      if isinstance((n.get("_meta") or {}).get("mcp_score"), dict)), (None, None))
    if node is None:
        return None
    if "file" in how:
        return _score_output(node, how["file"])
    if prompt_id in _scores:
        return _scores[prompt_id]
    texts = ((entry.get("outputs") or {}).get(node) or {}).get("text") or []
    abc_text = next((t for t in texts if isinstance(t, str) and t.strip()), None)
    if abc_text is None:
        return "sheet music not created: the workflow returned no ABC text"
    try:
        filename = await _save_score(abc_text, prompt_id[:8])
    except (score.ScoreError, ComfyUIError, OSError) as e:
        return f"sheet music not created: {e}"
    _scores[prompt_id] = _score_output(node, filename)
    return _scores[prompt_id]


async def _trim_outputs(entry: WorkflowEntry, applied: dict[str, Any],
                        outputs: list[dict]) -> list[dict]:
    """Crop transparent outputs to their content and save the copies next to the originals."""
    cfg = entry.trim_alpha
    param = cfg.get("param")
    if param and not applied.get(param, True):
        return []
    labels = cfg.get("outputs")
    c, trimmed = client(), []
    for out in outputs:
        if not _is_image(out["filename"]) or (labels and out.get("label") not in labels):
            continue
        data = imaging.trim_alpha((await c.view(out["filename"], out["subfolder"], out["type"])).content,
                                  padding=int(cfg.get("padding", 0)))
        if data is None:
            continue
        name = f"{PurePosixPath(out['filename']).stem.rstrip('_')}_trimmed.png"
        saved = await c.upload(data, name, overwrite=True, type_="output", subfolder=out["subfolder"])
        filename, subfolder = saved.get("name", name), saved.get("subfolder", out["subfolder"])
        trimmed.append({
            "node": out["node"],
            "label": f"{out['label']}_trimmed" if out.get("label") else "trimmed",
            "kind": out["kind"], "filename": filename, "subfolder": subfolder, "type": "output",
            "mime_type": "image/png", **media().links(filename, subfolder, "output"),
        })
    return trimmed


async def _run_preset(kind: str, workflow: str | None, params: dict[str, Any],
                      extra: dict[str, Any] | None, **kwargs: Any) -> list[Any]:
    name = workflow or registry().manifest().presets.get(kind)
    if not name:
        raise ToolError(
            f"No '{kind}' workflow configured. Set 'presets.{kind}' in workflows.yaml "
            "(WORKFLOWS_DIR) or pass the 'workflow' argument."
        )
    params = dict(params)
    inputs = {}
    for key, value in (extra or {}).items():
        (inputs if "." in key else params)[key] = value
    return await _execute(name, params=params, inputs=inputs, **kwargs)


# --- tools: server ---------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def comfyui_status() -> dict[str, Any]:
    """ComfyUI health: whether it is reachable, queue size, GPU and VRAM/RAM usage, versions."""
    c = client()
    try:
        stats, queue = await asyncio.gather(c.system_stats(), c.queue())
    except ComfyUIError as e:
        return {"running": False, "url": get_settings().public_url, "error": str(e)}

    running, pending = len(queue.get("queue_running", [])), len(queue.get("queue_pending", []))
    system = stats.get("system", {})
    result: dict[str, Any] = {
        "running": True,
        "url": get_settings().public_url,
        "queue": running + pending,
        "queue_running": running,
        "queue_pending": pending,
    }
    devices = stats.get("devices", [])
    if devices:
        d = devices[0]
        total, free = d.get("vram_total"), d.get("vram_free")
        result |= {
            "gpu": _gpu_name(d.get("name", "")),
            "vram_used": _gb(total - free) if total is not None and free is not None else None,
            "vram_total": _gb(total),
        }
        if len(devices) > 1:
            result["devices"] = [
                {"gpu": _gpu_name(x.get("name", "")), "vram_total": _gb(x.get("vram_total")),
                 "vram_free": _gb(x.get("vram_free"))}
                for x in devices
            ]
    if "ram_total" in system:
        result["ram_used"] = _gb(system["ram_total"] - system.get("ram_free", 0))
        result["ram_total"] = _gb(system["ram_total"])
    for key in ("comfyui_version", "pytorch_version", "python_version"):
        if key in system:
            result[key] = system[key].split(" ")[0]
    return result


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def comfyui_requirements(
    group: Literal["image", "video", "audio"] | None = None,
) -> dict[str, Any]:
    """Custom nodes, models (with download links and target folders) and workflows the presets
    need, each checked against the ComfyUI server: status ok / missing. group: image (also
    assets), video or audio; all when omitted."""
    report = await requirements.check(client(), [group] if group else None)
    report["missing"] = requirements.missing_count(report)
    return report


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def comfyui_models(folder: str | None = None, search: str | None = None) -> dict[str, Any]:
    """List installed models.

    Args:
        folder: Model folder, e.g. "checkpoints", "loras", "vae", "upscale_models",
            "diffusion_models", "text_encoders". Omit to list every folder.
        search: Case-insensitive substring filter on file names.
    """
    c = client()
    folders = [folder] if folder else [
        f for f in await c.model_folders() if f not in NON_MODEL_FOLDERS
    ]
    lists = await asyncio.gather(*(c.models(f) for f in folders), return_exceptions=True)
    result: dict[str, list[str]] = {}
    for name, files in zip(folders, lists, strict=True):
        if isinstance(files, Exception):
            if folder:
                raise files
            continue
        if search:
            files = [f for f in files if search.lower() in f.lower()]
        if files:
            result[name] = files
    return {"models": result, "total": sum(len(v) for v in result.values())}


@mcp.tool()
@tool_errors
async def comfyui_free_memory(unload_models: bool = True) -> dict[str, Any]:
    """Ask ComfyUI to unload models and free cached VRAM/RAM."""
    await client().free_memory(unload_models=unload_models, free_memory=True)
    return {"ok": True}


# --- tools: workflows ------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def comfyui_workflows() -> dict[str, Any]:
    """List available workflows (local files and those saved in ComfyUI), their friendly params
    and which preset (image/upscale/video/audio) each one backs."""
    reg = registry()
    items = await reg.describe_all()
    return {"workflows": sorted(items, key=lambda i: i["name"]), "presets": reg.manifest().presets}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def comfyui_workflow_info(workflow: str) -> dict[str, Any]:
    """Show a workflow's friendly params and every node's literal inputs, i.e. what can be
    changed via comfyui_run `params` or `inputs` ("<node id or title>.<input>")."""
    reg = registry()
    entry = await reg.get(workflow)
    graph = await reg.load_graph(entry)
    params = {}
    for name, p in entry.params.items():
        info = {"targets": p.targets, "type": p.type, "default": p.default,
                "description": p.description}
        if p.type == "choice":
            info["choices"] = await param_choices(reg.client, graph, p)
        if p.remove_when_false:
            info["removes_when_false"] = p.remove_when_false
        if p.remove_when_true:
            info["removes_when_true"] = p.remove_when_true
        if p.append_when:
            info["appends_when"] = p.append_when
        params[name] = {k: v for k, v in info.items() if v not in (None, "", [])}
    result = {"name": entry.name, "description": entry.description, "params": params}
    if entry.outputs:
        result["outputs"] = entry.outputs
    result["nodes"] = editable_inputs(graph)
    return result


@mcp.tool(structured_output=False)
@tool_errors
async def comfyui_run(
    workflow: str | None = None,
    params: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    workflow_json: dict[str, Any] | None = None,
    randomize_seed: bool = True,
    wait: bool = False,
    timeout: int | None = None,
    preview: bool = False,
    ctx: Context | None = None,
) -> list[Any]:
    """Queue a workflow on ComfyUI.

    Args:
        workflow: Workflow name from comfyui_workflows.
        params: Friendly params defined in workflows.yaml, e.g. {"prompt": "a cat", "steps": 20}.
        inputs: Raw node overrides, keyed "<node id or title>.<input>", e.g. {"6.text": "a cat",
            "KSampler.cfg": 6.5}.
        workflow_json: An inline API-format workflow, used instead of `workflow`.
        randomize_seed: Give seed/noise_seed inputs that were not set explicitly a new random
            value (otherwise ComfyUI may return a cached result and skip execution).
        wait: Block until the job finishes (or `timeout` seconds) and return its outputs.
        timeout: Seconds to wait when wait=true (default DEFAULT_WAIT_TIMEOUT).
        preview: With wait=true, also return downscaled previews of the resulting images.
    """
    return await _execute(workflow, params=params, inputs=inputs, workflow_json=workflow_json,
                          randomize_seed=randomize_seed, wait=wait, timeout=timeout,
                          preview=preview, ctx=ctx)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def comfyui_history(limit: int = 10) -> dict[str, Any]:
    """Recent finished jobs (newest first), including ones run from the ComfyUI web UI.
    Use a prompt_id from here with comfyui_save_workflow to turn that run into a workflow."""
    c = client()
    history = await c.history(max_items=limit)
    jobs = []
    for prompt_id, entry in reversed(list(history.items())):
        prompt = entry.get("prompt") or []
        graph = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else {}
        status = entry.get("status") or {}
        jobs.append({
            "prompt_id": prompt_id,
            "status": status.get("status_str"),
            "nodes": sorted({n.get("class_type", "") for n in graph.values()}),
            "outputs": [o["filename"] for o in extract_outputs(entry, media().links)],
        })
    return {"jobs": jobs}


@mcp.tool()
@tool_errors
async def comfyui_save_workflow(prompt_id: str, name: str, overwrite: bool = False) -> dict[str, Any]:
    """Save the exact API-format workflow of a past job as a reusable workflow, stored in
    ComfyUI under workflows/api/<name>.json. This is how UI-format workflows become runnable:
    run it once in the ComfyUI web UI, then save it from comfyui_history."""
    if not re.fullmatch(r"[\w\-. ]+", name):
        raise ToolError("name may only contain letters, digits, spaces, '-', '_' and '.'")
    history = await client().history(prompt_id)
    if prompt_id not in history:
        raise ToolError(f"Job {prompt_id} not found in history")
    prompt = history[prompt_id].get("prompt") or []
    graph = prompt[2] if len(prompt) > 2 else None
    graph = normalize_graph(graph, name)
    relpath = f"{SAVED_WORKFLOWS_SUBDIR}/{name.removesuffix('.json')}.json"
    await client().save_user_workflow(relpath, graph, overwrite=overwrite)
    workflow = f"comfyui:{relpath}"
    return {
        "workflow": workflow,
        "nodes": len(graph),
        "hint": f"Run it with comfyui_run(workflow='{workflow}', inputs={{...}}); see "
                "comfyui_workflow_info for editable inputs, or map params/presets in "
                f"workflows.yaml with file: '{workflow}'.",
    }


@mcp.tool()
@tool_errors
async def comfyui_convert_workflow(
    workflow: str, name: str, include_bypassed: bool = False, overwrite: bool = False
) -> dict[str, Any]:
    """Convert a UI-format workflow saved in ComfyUI (e.g. "comfyui:MY_FLOW.json") to API
    format and save it as workflows/api/<name>.json, without having to run it first.
    Resolves Set/Get nodes, reroutes and bypassed nodes. Not for workflows with subgraphs.

    Args:
        include_bypassed: Treat bypassed nodes as active (e.g. optional LoadImage slots that are
            bypassed in the editor but should exist in the API version).
    """
    if not re.fullmatch(r"[\w\-. ]+", name):
        raise ToolError("name may only contain letters, digits, spaces, '-', '_' and '.'")
    reg = registry()
    raw = await reg.load_raw(await reg.get(workflow))
    graph, warnings = convert_ui_to_api(raw, await client().object_info(), include_bypassed)
    relpath = f"{SAVED_WORKFLOWS_SUBDIR}/{name.removesuffix('.json')}.json"
    await client().save_user_workflow(relpath, graph, overwrite=overwrite)
    return {
        "workflow": f"comfyui:{relpath}",
        "nodes": len(graph),
        "inputs": [n for n in editable_inputs(graph) if n["title"].startswith("[$")] or None,
        "hint": "Check comfyui_workflow_info, then map params in workflows.yaml.",
        **({"warnings": warnings} if warnings else {}),
    }


# --- tools: jobs -----------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def comfyui_job_status(prompt_id: str) -> dict[str, Any]:
    """Status of a job: pending (with queue_position), running, success, error, interrupted,
    or not_found. Finished jobs include their output files (and sheet music for music jobs)."""
    return await _job_status(prompt_id, scores=True)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
@tool_errors
async def comfyui_get_output(prompt_id: str, include_temp: bool = False) -> list[Any]:
    """List the files produced by a finished job (images, videos, audio...). Each file has a
    `url` that supports streaming/seeking and, for media, a `player` page to open in a browser.

    Args:
        prompt_id: Job id returned by comfyui_run or a preset tool.
        include_temp: Also include temporary preview files (PreviewImage nodes).
    """
    status = await _job_status(prompt_id, include_temp=include_temp, scores=True)
    if status["status"] not in DONE:
        status["note"] = "Job has not finished yet."
    return [status, *_resource_links(status.get("outputs", []))]


@mcp.tool(annotations=READ_ONLY, structured_output=False)
@tool_errors
async def comfyui_view_image(
    prompt_id: str | None = None,
    index: int = 0,
    filename: str | None = None,
    subfolder: str = "",
    type: Literal["output", "input", "temp"] = "output",
    max_size: int | None = None,
) -> list[Any]:
    """Return an image so it can be seen inline (downscaled to max_size px).

    Either pass `prompt_id` (+ `index` among its image outputs) or an explicit `filename`
    (+ `subfolder`/`type`), e.g. to look at an uploaded input image.
    """
    if filename is None:
        if not prompt_id:
            raise ToolError("Pass prompt_id or filename.")
        status = await _job_status(prompt_id, include_temp=True)
        images = [o for o in status.get("outputs", []) if _is_image(o["filename"])]
        if not images:
            raise ToolError(f"Job {prompt_id} has no image outputs (status: {status['status']}).")
        if not 0 <= index < len(images):
            raise ToolError(f"index must be between 0 and {len(images) - 1}")
        out = images[index]
        filename, subfolder, type = out["filename"], out["subfolder"], out["type"]

    meta = {"filename": filename, "subfolder": subfolder, "type": type,
            **media().links(filename, subfolder, type)}
    if not _is_image(filename):
        return [meta | {"note": "Not an image; open `player` or `url` to play/download it."}]
    resp = await client().view(filename, subfolder, type)
    return [meta, _thumbnail(resp.content, max_size or get_settings().preview_max_size)]


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
@tool_errors
async def comfyui_cancel(prompt_id: str | None = None) -> dict[str, Any]:
    """Cancel a job: removes it from the queue if pending, interrupts it if running.
    Without prompt_id, interrupts whatever is running now."""
    if not prompt_id:
        await client().interrupt()
        return {"cancelled": "current"}
    return await _cancel_job(prompt_id)


@mcp.tool()
@tool_errors
async def comfyui_queue(action: Literal["list", "clear"] = "list") -> dict[str, Any]:
    """Show the running and pending jobs, or clear all pending jobs (action="clear";
    the running job is not affected)."""
    c = client()
    if action == "clear":
        await c.clear_queue()
    q = await c.queue()

    def item(i: list, pos: int | None = None) -> dict:
        d = {"prompt_id": i[1], "number": i[0], "nodes": len(i[2]) if len(i) > 2 else None}
        return d | ({"position": pos} if pos else {})

    pending = sorted(q.get("queue_pending", []), key=lambda i: i[0])
    return {
        "running": [item(i) for i in q.get("queue_running", [])],
        "pending": [item(i, p) for p, i in enumerate(pending, start=1)],
        **({"cleared": True} if action == "clear" else {}),
    }


@mcp.tool()
@tool_errors
async def comfyui_upload_file(source: str) -> dict[str, Any]:
    """Upload an image/audio/video into ComfyUI's input folder, for use in Load* nodes.

    Args:
        source: An http(s) URL, a data: URI or raw base64 content.
    """
    name = await upload_input_file(client(), source)
    return {"filename": name, "hint": "Use this value in a LoadImage/LoadAudio/LoadVideo input."}


# --- tools: presets --------------------------------------------------------------------------

@mcp.tool(structured_output=False)
@tool_errors
async def comfyui_generate_image(
    prompt: str,
    images: list[str] | None = None,
    negative_prompt: str | None = None,
    width: int | None = None,
    height: int | None = None,
    aspect_ratio: str | None = None,
    megapixels: float | None = None,
    upscale: bool | None = None,
    seed: int | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    batch_size: int | None = None,
    extra: dict[str, Any] | None = None,
    workflow: str | None = None,
    wait: bool = True,
    timeout: int | None = None,
    preview: bool = True,
    ctx: Context | None = None,
) -> list[Any]:
    """Generate or edit images with the default image workflow (preset "image").
    Without `images` it generates from the prompt. With `images` (URLs, base64, or ComfyUI
    input filenames) it edits them following the prompt, if the workflow supports it; refer to
    them in the prompt as <image1>, <image2>... in the order given.
    Size: width/height in pixels, or aspect_ratio (any ratio: "16:9", "5:4", "1.5") and/or
    megapixels (1.0 = ~1024x1024), converted to pixels. Without any size it generates
    1024x1024; when editing it uses the size of <image1> (scaled down if very large).
    `upscale` toggles the extra upscaled output when the workflow has one. Each output file has a
    `label` (e.g. "base", "upscale") when the workflow defines them.

    Empty arguments keep the workflow's own values. `extra` sets more workflow params or raw
    "<node>.<input>" overrides; `workflow` swaps the preset for another workflow."""
    return await _run_preset(
        "image", workflow,
        {"prompt": prompt, "images": images or None, "negative_prompt": negative_prompt,
         "width": width, "height": height, "aspect_ratio": aspect_ratio,
         "megapixels": megapixels, "upscale": upscale, "seed": seed,
         "steps": steps, "cfg": cfg, "batch_size": batch_size},
        extra, wait=wait, timeout=timeout, preview=preview, ctx=ctx,
    )


@mcp.tool(structured_output=False)
@tool_errors
async def comfyui_generate_asset(
    prompt: str,
    images: list[str] | None = None,
    width: int | None = None,
    height: int | None = None,
    transparent: bool = True,
    upscale: bool | None = None,
    trim: bool | None = None,
    seed: int | None = None,
    extra: dict[str, Any] | None = None,
    workflow: str | None = None,
    wait: bool = True,
    timeout: int | None = None,
    preview: bool = True,
    ctx: Context | None = None,
) -> list[Any]:
    """Generate or edit a single asset (object, sprite, icon, character cut-out) as a PNG with a
    transparent background, using the default asset workflow (preset "asset").
    Describe only the subject; the workflow adds the transparency instructions itself.
    Size is exact pixels: width/height (default 1024x1024, rounded to multiples of 16). With
    `images` and no width/height, the size of <image1> is used (scaled down if very large).
    Refer to the images as <image1>, <image2>... in the prompt; transparent input images are
    flattened onto white first. `transparent=false` keeps a normal background. `upscale`
    toggles the extra upscaled output; `trim` (default on) also returns copies cropped to the
    object. Each output file has a `label` ("base", "upscale", "base_trimmed"...). Previews
    show transparency as a checkerboard.

    `extra` sets more workflow params or raw "<node>.<input>" overrides; `workflow` swaps the
    preset for another workflow."""
    return await _run_preset(
        "asset", workflow,
        {"prompt": prompt, "images": images or None, "width": width, "height": height,
         "transparent": transparent, "upscale": upscale, "trim": trim, "seed": seed},
        extra, wait=wait, timeout=timeout, preview=preview, ctx=ctx,
    )


@mcp.tool(structured_output=False)
@tool_errors
async def comfyui_upscale(
    image: str,
    scale: float | None = None,
    extra: dict[str, Any] | None = None,
    workflow: str | None = None,
    wait: bool = True,
    timeout: int | None = None,
    preview: bool = True,
    ctx: Context | None = None,
) -> list[Any]:
    """Upscale an image with the default upscale workflow (preset "upscale").

    `image` may be a URL, data URI, base64, or the name of a file already in ComfyUI's input
    folder (e.g. a previous output uploaded with comfyui_upload_file).

    Empty arguments keep the workflow's own values. `extra` sets more workflow params or raw
    "<node>.<input>" overrides; `workflow` swaps the preset for another workflow."""
    return await _run_preset("upscale", workflow, {"image": image, "scale": scale}, extra,
                             wait=wait, timeout=timeout, preview=preview, ctx=ctx)


@mcp.tool(structured_output=False)
@tool_errors
async def comfyui_generate_video(
    prompt: str,
    negative_prompt: str | None = None,
    image: str | None = None,
    image2: str | None = None,
    width: int | None = None,
    height: int | None = None,
    aspect_ratio: str | None = None,
    duration: float | None = None,
    length: int | None = None,
    fps: int | None = None,
    seed: int | None = None,
    steps: int | None = None,
    extra: dict[str, Any] | None = None,
    workflow: str | None = None,
    wait: bool = False,
    timeout: int | None = None,
    ctx: Context | None = None,
) -> list[Any]:
    """Generate a video with the default video workflow (preset "video"). `image`/`image2` are
    start or reference images (URL, base64 or ComfyUI input filename) if the workflow uses them.
    Use `duration` (seconds) or `length` (frames), whichever the workflow maps.
    Video is slow: by default returns immediately; poll comfyui_job_status.

    Empty arguments keep the workflow's own values. `extra` sets more workflow params or raw
    "<node>.<input>" overrides; `workflow` swaps the preset for another workflow."""
    return await _run_preset(
        "video", workflow,
        {"prompt": prompt, "negative_prompt": negative_prompt, "image": image, "image2": image2,
         "width": width, "height": height, "aspect_ratio": aspect_ratio, "duration": duration,
         "length": length, "fps": fps, "seed": seed, "steps": steps},
        extra, wait=wait, timeout=timeout, ctx=ctx,
    )


@mcp.tool(structured_output=False)
@tool_errors
async def comfyui_generate_audio(
    prompt: str,
    negative_prompt: str | None = None,
    lyrics: str | None = None,
    duration: float | None = None,
    seed: int | None = None,
    steps: int | None = None,
    extra: dict[str, Any] | None = None,
    workflow: str | None = None,
    wait: bool = False,
    timeout: int | None = None,
    ctx: Context | None = None,
) -> list[Any]:
    """Generate audio/music with the default audio workflow (preset "audio").
    `prompt` describes the sound/style, `lyrics` is for song models, `duration` in seconds.

    Empty arguments keep the workflow's own values. `extra` sets more workflow params or raw
    "<node>.<input>" overrides; `workflow` swaps the preset for another workflow."""
    return await _run_preset(
        "audio", workflow,
        {"prompt": prompt, "negative_prompt": negative_prompt, "lyrics": lyrics,
         "duration": duration, "seed": seed, "steps": steps},
        extra, wait=wait, timeout=timeout, ctx=ctx,
    )


ABC_DIALECT = """YuE2 does not parse ABC: the score is fed to the model as text, so it must look like the scores
YuE2 itself writes. Any ABC is rewritten into that dialect automatically; writing it directly
gives the most control:
  header X:1 / T: / M:4/4 / L:1/16 / Q:1/4=<bpm> / V: Vocal ... / V: Ins ... / K:<key>
  sections as comment lines "% intro", "% verse", "% pre-chorus", "% chorus", "% bridge",
  "% interlude", "% outro"; inside them, up to 4 bars of "V: Vocal" (sung melody, chord symbol
  in quotes at the start of a bar, rests z16) followed by the same bars of "V: Ins"
  (instrumental line, or Z4 = 4 bars of rest); no w: lyric lines (lyrics are a separate input,
  with [Verse]/[Chorus] tags in the same order as the sung sections); mode "melody" uses no
  chord symbols.
An instrumental line under the vocals (fills, comping, counter-melody) makes the arrangement
much richer than "Z4" rests, which tend to sound like a karaoke backing: in plain ABC write it as
a second voice (e.g. V:Voz + V:Cavaco / V:Piano), it becomes the Ins voice.
Plain ABC may keep T: (title), C: (authors), %%text section names and w: lyrics under the notes:
they are dropped for YuE2 but printed on the sheet music (PDF), which is what a song
registration needs.
Writing a score YuE2 sings faithfully (when it doesn't fit, the model improvises and the song
runs longer than the score):
  - give the lyrics enough notes: about one note per sung syllable, more for melismas; Portuguese
    and Spanish need more notes than the written syllables suggest, so avoid squeezing vowels
    (do~a) into one note;
  - use the genre's rhythm, not only straight eighths: dance-pop/funk/reggaeton 3+3+2
    (e.g. g3f3e2 at L:1/16) and anticipations tied over the beat or bar (e2-|e2...); samba/pagode
    syncopation; ballads with longer notes;
  - make the chorus the peak: its highest note above the verse and the pre-chorus;
  - give the pre-chorus its own harmony (e.g. the IV or ii chord, a dominant) to build tension;
  - keep regular phrases (2 or 4 bars, sections of 4 or 8 bars) and vary the instrumental line
    between sections (riff in the intro, comping in verses, fuller in choruses)."""


def _with_abc_dialect(fn):
    """Append ABC_DIALECT to the tool description (a docstring cannot be an expression)."""
    fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n\n" + ABC_DIALECT
    return fn


@mcp.tool(annotations=READ_ONLY)
@_with_abc_dialect
@tool_errors
async def comfyui_normalize_abc(abc_notation: str, lyrics: str | None = None,
                                mode: Literal["full", "melody"] = "full") -> dict[str, Any]:
    """Rewrite ABC notation (e.g. written by an LLM: one voice, L:1/8, w: lyrics, %%text or P:
    sections) into the dialect YuE2 understands, and report problems (bars of the wrong length,
    sections that don't follow the [Verse]/[Chorus] tags of `lyrics`...).
    comfyui_generate_music does this by itself; use this tool to check a score first.

    """
    try:
        result, warnings = abc.for_yue2(abc_notation, lyrics, mode)
    except abc.AbcError as e:
        raise ToolError(str(e)) from e
    return {"abc": result, "warnings": warnings,
            "sung_sections": abc.abc_vocal_sections(result)}


@mcp.tool(structured_output=False)
@_with_abc_dialect
@tool_errors
async def comfyui_generate_music(
    style: str,
    lyrics: str,
    abc_notation: str | None = None,
    song: str | None = None,
    mode: Literal["full", "melody"] | None = None,
    duration: float | None = None,
    seed: int | None = None,
    extra: dict[str, Any] | None = None,
    workflow: str | None = None,
    wait: bool = False,
    timeout: int | None = None,
    ctx: Context | None = None,
) -> list[Any]:
    """Generate a song with vocals (YuE2). Returns the audio (label "audio"), the ABC score
    that was used (label "abc", an .md file; its text is also in `texts`) and its sheet music
    (label "score", a PDF): of `abc_notation` exactly as given (with its w: lyrics) when you pass
    one, else of the score the workflow wrote (created when the job finishes; with wait=false it
    shows up in comfyui_job_status / comfyui_get_output).

    - Only style + lyrics (preset "music"): YuE2 writes the score itself, then sings it.
    - With `abc_notation`: YuE2 follows your score (melody, chords, structure, tempo).
    - With `song` (URL, base64 or ComfyUI input filename; preset "music_cover"): a cover -
      SheetSage2 transcribes the melody of that song and YuE2 sings it with the new style and
      lyrics (`abc_notation`, if given, replaces the transcription).
    `lyrics` use [Verse]/[Chorus]/[Bridge] tags. `mode`: "full" (melody + chords, default for
    songs) or "melody" (melody only, default for covers). `duration` caps the length in seconds.
    Takes a few minutes: by default returns a prompt_id to poll with comfyui_job_status.

    """
    return await _run_preset(
        "music_cover" if song else "music", workflow,
        {"prompt": style, "lyrics": lyrics, "abc": abc_notation, "song": song, "mode": mode,
         "duration": duration, "seed": seed},
        extra, wait=wait, timeout=timeout, ctx=ctx,
    )


@mcp.tool(structured_output=False)
@tool_errors
async def comfyui_song_to_abc(
    song: str,
    mode: Literal["full", "melody"] | None = None,
    workflow: str | None = None,
    timeout: int | None = None,
    ctx: Context | None = None,
) -> list[Any]:
    """Transcribe a song (URL, base64 or ComfyUI input filename) into ABC notation with SheetSage2
    (preset "song_abc"), in the dialect YuE2 uses: edit it and pass it to comfyui_generate_music
    as `abc_notation`. mode "full" = melody + chords, "melody" = melody only (for covers).
    Also returns the sheet music of the transcription (label "score", a PDF)."""
    content = await _run_preset("song_abc", workflow, {"song": song, "mode": mode}, None,
                                wait=True, timeout=timeout, ctx=ctx)
    result = content[0]
    scores = [t["text"] for t in result.get("texts", []) if t["text"].lstrip().startswith("X:")]
    if scores:
        result["abc"] = scores[0] if len(scores) == 1 else scores
        result.pop("texts", None)
    return content

