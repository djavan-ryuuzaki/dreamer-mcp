"""Convert a ComfyUI *UI-format* workflow (what the editor saves) into *API format*.

Uses the live /object_info to tell real inputs from frontend-only widgets. Handles:
  * frontend-only nodes: SetNode/GetNode (KJNodes), Reroute, Note/MarkdownNote, PrimitiveNode
  * bypassed (mode 4) nodes: links are passed through to an input of the same type
  * muted (mode 2) nodes: dropped
  * widget values, including control_after_generate ("randomize"...) and v3 dynamic inputs
    such as "resize_type.megapixels" or autogrow "images.image_1"
Subgraphs are not supported: for those, run the workflow once and use comfyui_save_workflow.
"""

from __future__ import annotations

from typing import Any

from .workflows import WorkflowError

VIRTUAL_TYPES = {"SetNode", "GetNode", "Reroute", "Note", "MarkdownNote", "PrimitiveNode"}
CONTROL_VALUES = {"fixed", "increment", "decrement", "randomize"}
MODE_MUTED, MODE_BYPASS = 2, 4


def _is_widget_spec(spec: Any) -> bool:
    return isinstance(spec, list) and bool(spec) and (
        isinstance(spec[0], list) or spec[0] in ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO")
    )


def convert_ui_to_api(
    ui: dict, object_info: dict, include_bypassed: bool = False
) -> tuple[dict, list[str]]:
    """Returns (api graph, warnings)."""
    if (ui.get("definitions") or {}).get("subgraphs"):
        raise WorkflowError(
            "Workflows with subgraphs cannot be converted reliably. Run it once in ComfyUI and "
            "use comfyui_history + comfyui_save_workflow instead."
        )
    nodes = {n["id"]: n for n in ui.get("nodes", [])}
    links = {lk[0]: lk for lk in ui.get("links", [])}  # [id, src, src_slot, dst, dst_slot, type]
    setters = {
        (n.get("widgets_values") or [None])[0]: n for n in nodes.values() if n["type"] == "SetNode"
    }
    missing = sorted({n["type"] for n in nodes.values()
                      if n["type"] not in VIRTUAL_TYPES and n["type"] not in object_info})
    if missing:
        raise WorkflowError(f"Node types not installed on this ComfyUI: {', '.join(missing)}")

    def mode(n: dict) -> int:
        m = n.get("mode", 0)
        return 0 if (m == MODE_BYPASS and include_bypassed) else m

    def resolve_link(link_id: int | None, seen: frozenset = frozenset()) -> list | None:
        """Follow a link back to a real, active node output: [node_id, slot] or None."""
        if link_id is None or link_id not in links or link_id in seen:
            return None
        seen = seen | {link_id}
        _, src_id, src_slot, *_ = links[link_id]
        src = nodes.get(src_id)
        if src is None:
            return None
        if src["type"] in ("SetNode", "Reroute"):
            return resolve_link((src.get("inputs") or [{}])[0].get("link"), seen)
        if src["type"] == "GetNode":
            setter = setters.get((src.get("widgets_values") or [None])[0])
            return resolve_link((setter.get("inputs") or [{}])[0].get("link"), seen) if setter else None
        if src["type"] in VIRTUAL_TYPES or mode(src) == MODE_MUTED:
            return None
        if mode(src) == MODE_BYPASS:
            out_type = src["outputs"][src_slot]["type"]
            for inp in src.get("inputs", []):
                if inp.get("link") is not None and inp.get("type") in (out_type, "*"):
                    return resolve_link(inp["link"], seen)
            return None
        return [str(src_id), src_slot]

    graph: dict[str, dict] = {}
    warnings: list[str] = []
    for n in nodes.values():
        if n["type"] in VIRTUAL_TYPES or mode(n) in (MODE_MUTED, MODE_BYPASS):
            continue
        info = object_info[n["type"]]
        specs = {**(info["input"].get("required") or {}), **(info["input"].get("optional") or {})}
        inputs: dict[str, Any] = {}

        values = list(n.get("widgets_values") or [])
        if isinstance(n.get("widgets_values"), dict):  # some custom nodes store a dict
            values = []
            inputs.update({k: v for k, v in n["widgets_values"].items() if k.split(".")[0] in specs})
        widget_inputs = [i for i in n.get("inputs", []) if i.get("widget")]
        vi = 0
        for inp in widget_inputs:
            if vi >= len(values):
                break
            name = inp["widget"]["name"]
            value = values[vi]
            vi += 1
            spec = specs.get(name)
            opts = spec[1] if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict) else {}
            if (opts.get("control_after_generate") or name in ("seed", "noise_seed")) \
                    and vi < len(values) and values[vi] in CONTROL_VALUES:
                vi += 1  # skip the "randomize"/"fixed" companion value
            if name in specs or name.split(".")[0] in specs:
                inputs[name] = value

        for inp in n.get("inputs", []):
            name = inp["name"]
            if inp.get("link") is None:
                continue
            src = resolve_link(inp["link"])
            if src is not None:
                inputs[name] = src
            elif name in inputs and not _is_widget_spec(specs.get(name)):
                del inputs[name]

        absent = [k for k in (info["input"].get("required") or {}) if k not in inputs
                  and not any(i.startswith(k + ".") for i in inputs)]
        if absent and info.get("output_node"):
            warnings.append(f"Dropped UI-only output node #{n['id']} {n['type']} "
                            f"(missing {', '.join(absent)})")
            continue
        graph[str(n["id"])] = {
            "class_type": n["type"],
            "inputs": inputs,
            "_meta": {"title": n.get("title") or info.get("display_name") or n["type"]},
        }
    graph = prune_unreachable(graph, object_info)
    if all(n["class_type"].startswith("Preview") for n in graph.values()
           if object_info[n["class_type"]].get("output_node")):
        warnings.append("Only Preview* output nodes: results are temporary files. Add a "
                        "SaveImage/SaveVideo/SaveAudio node to keep them.")
    return graph, warnings


def prune_unreachable(graph: dict, object_info: dict) -> dict:
    """Keep only nodes that feed an output node (what ComfyUI would actually execute)."""
    outputs = [nid for nid, n in graph.items() if object_info[n["class_type"]].get("output_node")]
    if not outputs:
        raise WorkflowError("Workflow has no output node (e.g. SaveImage)")
    keep: set[str] = set()
    stack = list(outputs)
    while stack:
        nid = stack.pop()
        if nid in keep or nid not in graph:
            continue
        keep.add(nid)
        for v in graph[nid]["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[1], int):
                stack.append(str(v[0]))
    return {nid: n for nid, n in graph.items() if nid in keep}
