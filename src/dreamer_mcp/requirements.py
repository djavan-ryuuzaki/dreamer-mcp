"""Custom nodes, models and workflows the default presets need, checked against ComfyUI.

The list lives in requirements.yaml (shipped inside the package, so `uvx dreamer-mcp` and the
Docker image both have it).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

from .client import ComfyClient, ComfyUIError
from .workflows import file_basename

OK, BUNDLED, MISSING, UNKNOWN = "ok", "bundled", "missing", "unknown"


@lru_cache
def load() -> dict[str, Any]:
    text = resources.files(__package__).joinpath("requirements.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


async def check(client: ComfyClient | None, groups: list[str] | None = None) -> dict[str, Any]:
    """Requirements with a status for each item. Without a client (or when ComfyUI is
    unreachable) every status is "unknown"."""
    spec = load()
    selected = {k: v for k, v in spec["groups"].items() if not groups or k in groups}
    bundled_dir = client.settings.workflows_path if client is not None else None
    comfy: dict[str, Any] = {"url": spec["comfyui"]["url"],
                             "tested_version": spec["comfyui"]["tested_version"]}
    classes: set[str] | None = None
    models: dict[str, set[str] | None] = {}
    workflows: set[str] | None = None
    if client is not None:
        comfy["server"] = client.settings.comfyui_url
        try:
            stats, info, saved = await asyncio.gather(
                client.system_stats(), client.object_info(), client.list_user_workflows())
            comfy["reachable"] = True
            comfy["version"] = stats.get("system", {}).get("comfyui_version")
            classes, workflows = set(info), set(saved)
            folders = sorted({m["folder"] for g in selected.values() for m in g.get("models", [])})
            listings = await asyncio.gather(*(client.models(f) for f in folders),
                                            return_exceptions=True)
            for folder, files in zip(folders, listings, strict=True):
                models[folder] = (set() if isinstance(files, BaseException)
                                  else {file_basename(f) for f in files})
        except ComfyUIError as e:
            comfy |= {"reachable": False, "error": str(e)}
            classes = workflows = None

    def status(found: bool | None) -> str:
        return UNKNOWN if found is None else OK if found else MISSING

    report_groups = []
    for name, g in selected.items():
        nodes = []
        for pkg in g.get("nodes", []):
            missing = None if classes is None else [c for c in pkg["classes"] if c not in classes]
            item = {k: v for k, v in pkg.items() if k != "classes"} | {
                "classes": pkg["classes"], "status": status(None if missing is None else not missing)}
            if missing:
                item["missing_classes"] = missing
            nodes.append(item)
        model_items = []
        for m in g.get("models", []):
            listing = models.get(m["folder"])
            model_items.append(m | {"status": status(None if listing is None
                                                     else m["file"] in listing)})
        wf_items = []
        for w in g.get("workflows", []):
            st = status(None if workflows is None else w in workflows)
            # Not saved in ComfyUI: the MCP falls back to its own copy (see workflows.yaml).
            if st == MISSING and bundled_dir and (bundled_dir / file_basename(w)).is_file():
                st = BUNDLED
            wf_items.append({"file": w, "status": st})
        required = [i["status"] for i in nodes if not i.get("optional")]
        required += [i["status"] for i in model_items + wf_items]
        ready = None if UNKNOWN in required else MISSING not in required
        report_groups.append({"name": name, "title": g.get("title", name), "ready": ready,
                              "nodes": nodes, "models": model_items, "workflows": wf_items})
    return {"comfyui": comfy, "groups": report_groups}


def missing_count(report: dict[str, Any]) -> int:
    return sum(
        1
        for g in report["groups"]
        for i in g["nodes"] + g["models"] + g["workflows"]
        if i["status"] == MISSING and not i.get("optional")
    )


_MARK = {OK: "[ok]      ", BUNDLED: "[bundled] ", MISSING: "[MISSING] ", UNKNOWN: "[ ]       "}


def format_report(report: dict[str, Any]) -> str:
    comfy = report["comfyui"]
    lines = ["Dreamer MCP requirements (custom nodes, models and workflows used by the presets)"]
    if comfy.get("reachable"):
        lines.append(f"ComfyUI {comfy.get('server')}: version {comfy.get('version') or '?'} "
                     f"(tested with {comfy['tested_version']})")
    elif "error" in comfy:
        lines.append(f"ComfyUI not checked: {comfy['error']}")
    else:
        lines.append(f"ComfyUI {comfy['tested_version']}+ recommended ({comfy['url']})")

    for g in report["groups"]:
        state = {True: "ready", False: "incomplete", None: "not checked"}[g["ready"]]
        lines += ["", f"== {g['title']}: {state}"]
        for n in g["nodes"]:
            mark = "[optional]" if n.get("optional") and n["status"] != OK else _MARK[n["status"]]
            where = "core, update ComfyUI" if n["package"] == "ComfyUI" else n.get("url", "")
            names = n.get("missing_classes") or n["classes"]
            lines.append(f"  {mark} node   {n['package']} ({', '.join(names)}) - {where}")
            if n.get("note"):
                lines.append(f"{'':20}{n['note']}")
        for m in g["models"]:
            lines.append(f"  {_MARK[m['status']]} model  models/{m['folder']}/{m['file']}"
                         f" ({m['size_gb']} GB)")
            if m["status"] != OK:
                lines.append(f"{'':20}{m['url']}")
                if m.get("note"):
                    lines.append(f"{'':20}{m['note']}")
        for w in g["workflows"]:
            where = ("not in ComfyUI, using the copy shipped with the MCP"
                     if w["status"] == BUNDLED else f"user/default/workflows/{w['file']}")
            lines.append(f"  {_MARK[w['status']]} flow   {w['file']} - {where}")

    missing = missing_count(report)
    if missing:
        lines += ["", (f"{missing} required item(s) missing: the tools that need them will fail "
                       "until they are installed (restart ComfyUI after adding nodes).")]
    return "\n".join(lines)
