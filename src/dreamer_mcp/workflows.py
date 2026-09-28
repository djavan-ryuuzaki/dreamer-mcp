"""Workflow discovery, the workflows.yaml manifest, and input overrides.

Workflows must be in ComfyUI *API format* (in ComfyUI: Workflow > Export (API)). They come from:
  * local files in WORKFLOWS_DIR (a ConfigMap in Kubernetes), and
  * (optionally) workflows saved in ComfyUI's user directory, exposed as "comfyui:<path>".

The optional manifest (WORKFLOWS_DIR/workflows.yaml) gives workflows friendly parameter names
and chooses the preset used by comfyui_generate_image / _upscale / _video / _audio.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import mimetypes
import random
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import imaging
from .client import ComfyClient, ComfyUIError
from .media import mime_type

COMFYUI_PREFIX = "comfyui:"
MANIFEST_NAMES = ("workflows.yaml", "workflows.yml")
PRESET_KINDS = ("image", "asset", "upscale", "video", "audio")
SEED_INPUTS = {"seed", "noise_seed"}
MODEL_EXTS = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft", ".onnx")
MAX_SEED = 2**50


class WorkflowError(Exception):
    """Invalid workflow, manifest, or parameter."""


@dataclass
class ParamSpec:
    targets: list[str]
    """Each target is "<node id or title>.<input name>"."""
    type: str = "auto"  # auto | string | int | float | bool | seed | choice | file | file_list
    default: Any = None
    description: str = ""
    prune_missing: bool = False
    """When no value is given (or, for file_list, for each unused slot), delete the target node
    and everything that depends on it. This is how optional LoadImage slots are expressed."""
    choices: list[str] | None = None
    """For type "choice": allowed values. When omitted they are read from the target's combo
    options in /object_info."""
    remove_when_false: list[str] = field(default_factory=list)
    """For type "bool": nodes (id or title) removed when the value is false, e.g. an optional
    SaveImage. Nodes that only fed a removed output are skipped by ComfyUI."""
    remove_when_true: list[str] = field(default_factory=list)
    """For type "bool": nodes removed when the value is true (the alternative branch)."""
    append_when: dict[str, str] = field(default_factory=dict)
    """For strings: {"<bool param>": "text"} appended when that param is true, e.g. a fixed
    instruction added to the prompt."""
    multiple_of: int | None = None
    """For ints: round to the nearest multiple (e.g. 16 for image sizes)."""
    flatten_alpha: str | None = None
    """For file/file_list: composite transparent images onto this color (e.g. "#ffffff")
    before ComfyUI's LoadImage drops the alpha channel."""

    @classmethod
    def parse(cls, name: str, raw: Any) -> ParamSpec:
        if isinstance(raw, str):
            return cls(targets=[raw])
        if not isinstance(raw, dict):
            raise WorkflowError(f"param '{name}': expected a 'target' (e.g. '6.text')")
        # Without a target a param only feeds rules (append_when, size, remove_when_*...).
        targets = _as_list(raw.get("target"))
        return cls(
            targets=[str(t) for t in targets],
            type=raw.get("type", "auto"),
            default=raw.get("default"),
            description=raw.get("description", ""),
            prune_missing=bool(raw.get("prune_missing", False)),
            choices=[str(c) for c in raw["choices"]] if raw.get("choices") else None,
            remove_when_false=_as_list(raw.get("remove_when_false")),
            remove_when_true=_as_list(raw.get("remove_when_true")),
            append_when={str(k): str(v) for k, v in (raw.get("append_when") or {}).items()},
            multiple_of=int(raw["multiple_of"]) if raw.get("multiple_of") else None,
            flatten_alpha=raw.get("flatten_alpha"),
        )


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


@dataclass
class WorkflowEntry:
    name: str
    file: str
    """Local file name relative to WORKFLOWS_DIR, or "comfyui:<path in user workflows>"."""
    fallback: str | None = None
    """Local file used when `file` ("comfyui:...") is not saved in ComfyUI."""
    description: str = ""
    params: dict[str, ParamSpec] = field(default_factory=dict)
    when_missing: dict[str, dict[str, Any]] = field(default_factory=dict)
    """param name -> {"<node>.<input>": value} applied when that param is not provided."""
    when_set: dict[str, dict[str, Any]] = field(default_factory=dict)
    """param name -> {"<node>.<input>": value} applied when that param is provided."""
    outputs: dict[str, str] = field(default_factory=dict)
    """label -> output node (id or title); the label is returned with each output file."""
    size: dict[str, Any] = field(default_factory=dict)
    """How width/height are derived when the caller doesn't give them. Keys (all optional):
    width/height (param names, default "width"/"height"), aspect_ratio/megapixels (param names
    turned into pixels), from_image (file param: when editing, use the size of its first image),
    max_side (cap for from_image, default 2048), default_megapixels (default 1.0)."""
    trim_alpha: dict[str, Any] = field(default_factory=dict)
    """{"outputs": [labels], "padding": 16, "param": "<bool param>"}: also return a copy of
    each transparent output cropped to its content."""

    @property
    def source(self) -> str:
        return "comfyui" if self.file.startswith(COMFYUI_PREFIX) else "local"


@dataclass
class Manifest:
    presets: dict[str, str | None] = field(default_factory=dict)
    workflows: dict[str, WorkflowEntry] = field(default_factory=dict)


def load_manifest(workflows_dir: Path) -> Manifest:
    for fname in MANIFEST_NAMES:
        path = workflows_dir / fname
        if path.is_file():
            break
    else:
        return Manifest()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise WorkflowError(f"Invalid {path.name}: {e}") from e

    workflows = {}
    for name, spec in (raw.get("workflows") or {}).items():
        spec = spec or {}
        params = {p: ParamSpec.parse(p, v) for p, v in (spec.get("params") or {}).items()}
        workflows[name] = WorkflowEntry(
            name=name,
            file=spec.get("file", f"{name}.json"),
            fallback=spec.get("fallback"),
            description=spec.get("description", ""),
            params=params,
            when_missing=dict(spec.get("when_missing") or {}),
            when_set=dict(spec.get("when_set") or {}),
            outputs={str(k): str(v) for k, v in (spec.get("outputs") or {}).items()},
            size=dict(spec.get("size") or {}),
            trim_alpha=dict(spec.get("trim_alpha") or {}),
        )
    return Manifest(presets=dict(raw.get("presets") or {}), workflows=workflows)


def is_api_format(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and len(data) > 0
        and all(isinstance(v, dict) and "class_type" in v for v in data.values())
    )


def normalize_graph(data: Any, name: str) -> dict:
    if isinstance(data, dict) and "prompt" in data and is_api_format(data["prompt"]):
        data = data["prompt"]
    if is_api_format(data):
        return data
    if isinstance(data, dict) and "nodes" in data and "links" in data:
        raise WorkflowError(
            f"Workflow '{name}' is in UI format. In ComfyUI open it and use "
            "'Workflow > Export (API)', then save that file instead."
        )
    raise WorkflowError(f"Workflow '{name}' is not a valid ComfyUI API-format workflow.")


class WorkflowRegistry:
    def __init__(self, client: ComfyClient, workflows_dir: Path, include_comfyui: bool = True):
        self.client = client
        self.dir = workflows_dir
        self.include_comfyui = include_comfyui

    def manifest(self) -> Manifest:
        # Re-read every time so ConfigMap updates apply without restarting the pod.
        return load_manifest(self.dir)

    async def entries(self) -> dict[str, WorkflowEntry]:
        manifest = self.manifest()
        entries = dict(manifest.workflows)
        referenced = {e.file for e in entries.values()} | {
            e.fallback for e in entries.values() if e.fallback}
        if self.dir.is_dir():
            for path in sorted(self.dir.rglob("*.json")):
                rel = path.relative_to(self.dir).as_posix()
                if rel not in referenced:
                    name = rel.removesuffix(".json")
                    entries.setdefault(name, WorkflowEntry(name=name, file=rel))
        if self.include_comfyui:
            for rel in await self.client.list_user_workflows():
                if not rel.endswith(".json"):
                    continue
                file = COMFYUI_PREFIX + rel
                if file not in referenced:
                    entries.setdefault(file, WorkflowEntry(name=file, file=file))
        return entries

    async def get(self, name: str) -> WorkflowEntry:
        entries = await self.entries()
        if name in entries:
            return entries[name]
        if name.startswith(COMFYUI_PREFIX):
            return WorkflowEntry(name=name, file=name)
        raise WorkflowError(f"Unknown workflow '{name}'. Available: {', '.join(sorted(entries)) or 'none'}")

    async def load_raw(self, entry: WorkflowEntry) -> Any:
        if entry.source != "comfyui":
            return self._load_local(entry.file)
        relpath = entry.file.removeprefix(COMFYUI_PREFIX)
        try:
            return await self.client.get_user_workflow(relpath)
        except ComfyUIError as e:
            # Not saved in ComfyUI: use the copy shipped with the MCP, if the manifest has one.
            if entry.fallback and (self.dir / entry.fallback).is_file():
                return self._load_local(entry.fallback)
            raise WorkflowError(f"Cannot load '{entry.file}' from ComfyUI: {e}") from e

    def _load_local(self, file: str) -> Any:
        path = (self.dir / file).resolve()
        if not path.is_relative_to(self.dir.resolve()) or not path.is_file():
            raise WorkflowError(f"Workflow file not found: {file}")
        return json.loads(path.read_text(encoding="utf-8"))

    async def load_graph(self, entry: WorkflowEntry) -> dict:
        return normalize_graph(await self.load_raw(entry), entry.name)

    async def describe_all(self) -> list[dict]:
        entries = await self.entries()
        presets = {v: k for k, v in self.manifest().presets.items() if v}
        sem = asyncio.Semaphore(8)

        async def describe(e: WorkflowEntry) -> dict:
            async with sem:
                try:
                    runnable, note = is_api_format(_unwrap(await self.load_raw(e))), None
                except (WorkflowError, ValueError) as ex:
                    runnable, note = False, str(ex)
            item: dict[str, Any] = {"name": e.name, "source": e.source, "runnable": runnable}
            if e.description:
                item["description"] = e.description
            if e.params:
                item["params"] = sorted(e.params)
            if e.name in presets:
                item["preset"] = presets[e.name]
            if not runnable:
                item["note"] = note or ("UI-format: run it once in ComfyUI, then comfyui_history + "
                                 "comfyui_save_workflow (or re-export with 'Export (API)')")
            return item

        return list(await asyncio.gather(*(describe(e) for e in entries.values())))


def _unwrap(data: Any) -> Any:
    if isinstance(data, dict) and "prompt" in data and is_api_format(data["prompt"]):
        return data["prompt"]
    return data


# --- graph manipulation ----------------------------------------------------------------------


def node_title(node: dict) -> str:
    return (node.get("_meta") or {}).get("title") or node.get("class_type", "")


def resolve_node(graph: dict, ref: str) -> str:
    if ref in graph:
        return ref
    ref_l = ref.lower()
    matches = [nid for nid, n in graph.items() if node_title(n).lower() == ref_l]
    if not matches:
        matches = [nid for nid, n in graph.items() if n.get("class_type", "").lower() == ref_l]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise WorkflowError(f"No node with id/title/class '{ref}'")
    raise WorkflowError(f"'{ref}' is ambiguous (nodes {', '.join(matches)}); use the node id")


def set_input(graph: dict, target: str, value: Any) -> None:
    if "." not in target:
        raise WorkflowError(f"Invalid target '{target}', expected '<node>.<input>' (e.g. '6.text')")
    # Titles and input names may both contain dots ("461.format.bit_depth"): prefer the split
    # whose node actually has that input, else fall back to the last dot.
    splits = [(target[:i], target[i + 1:]) for i, ch in enumerate(target) if ch == "."]
    for ref, input_name in splits:
        try:
            node_id = resolve_node(graph, ref)
        except WorkflowError:
            continue
        if input_name in (graph[node_id].get("inputs") or {}):
            graph[node_id]["inputs"][input_name] = value
            return
    ref, input_name = splits[-1]
    node_id = resolve_node(graph, ref)
    inputs = graph[node_id].setdefault("inputs", {})
    if input_name not in inputs:
        available = ", ".join(sorted(inputs)) or "none"
        raise WorkflowError(f"Node {node_id} has no input '{input_name}' (available: {available})")
    inputs[input_name] = value


def apply_overrides(graph: dict, overrides: dict[str, Any]) -> dict:
    graph = copy.deepcopy(graph)
    for target, value in overrides.items():
        set_input(graph, target, value)
    return graph


def randomize_seeds(graph: dict, skip: set[str] = frozenset()) -> None:
    for nid, node in graph.items():
        for name, value in (node.get("inputs") or {}).items():
            if name in SEED_INPUTS and isinstance(value, int) and f"{nid}.{name}" not in skip:
                node["inputs"][name] = random.randint(0, MAX_SEED)


def editable_inputs(graph: dict, max_len: int = 200) -> list[dict]:
    """Nodes with their literal (non-linked) inputs, so callers know what can be overridden."""
    nodes = []
    for nid, node in sorted(graph.items(), key=lambda kv: _node_sort_key(kv[0])):
        literal = {}
        for k, v in (node.get("inputs") or {}).items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[1], int):
                continue  # link to another node's output
            if isinstance(v, str) and len(v) > max_len:
                v = v[:max_len] + "..."
            literal[k] = v
        nodes.append({"id": nid, "class_type": node.get("class_type"), "title": node_title(node),
                      "inputs": literal})
    return nodes


def _node_sort_key(nid: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[:.]", nid))


# --- parameters ------------------------------------------------------------------------------


def coerce(value: Any, type_: str) -> Any:
    try:
        if type_ == "int":
            return int(value)
        if type_ == "float":
            return float(value)
        if type_ == "bool":
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
        if type_ == "string":
            return str(value)
    except (TypeError, ValueError) as e:
        raise WorkflowError(f"Cannot convert {value!r} to {type_}") from e
    return value


def combo_options(spec: Any) -> list[str] | None:
    """Options of a combo input spec from /object_info (old list form or new "COMBO" form)."""
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return [str(o) for o in spec[0]]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        return [str(o) for o in spec[1].get("options") or []]
    return None


def _split_target(graph: dict, target: str) -> tuple[str, str]:
    """(node id, input name), accepting dots in titles and input names."""
    for i, ch in enumerate(target):
        if ch == ".":
            try:
                return resolve_node(graph, target[:i]), target[i + 1:]
            except WorkflowError:
                continue
    raise WorkflowError(f"No node found for target '{target}'")


def file_basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


async def resolve_model_paths(client: ComfyClient, graph: dict) -> dict[str, str]:
    """Point model inputs at the file ComfyUI actually has when the workflow names one that
    is not in the combo but exactly one installed file has the same name in a subfolder
    (e.g. "model.safetensors" -> "QWEN2/model.safetensors"). Returns {old: new}."""
    wanted = [(nid, key, value) for nid, node in graph.items()
              for key, value in (node.get("inputs") or {}).items()
              if isinstance(value, str) and value.lower().endswith(MODEL_EXTS)]
    classes = sorted({graph[nid]["class_type"] for nid, _, _ in wanted})
    infos = await asyncio.gather(*(client.object_info(c) for c in classes),
                                 return_exceptions=True)
    specs = {c: i.get(c) or {} for c, i in zip(classes, infos, strict=True) if isinstance(i, dict)}
    changed: dict[str, str] = {}
    for nid, key, value in wanted:
        inputs = (specs.get(graph[nid]["class_type"]) or {}).get("input") or {}
        options = combo_options((inputs.get("required") or {}).get(key)
                                or (inputs.get("optional") or {}).get(key))
        if not options or value in options:
            continue
        matches = [o for o in options if file_basename(o) == file_basename(value)]
        if len(matches) == 1:
            graph[nid]["inputs"][key] = changed[value] = matches[0]
    return changed


async def param_choices(client: ComfyClient, graph: dict, spec: ParamSpec) -> list[str] | None:
    if spec.choices or spec.type != "choice":
        return spec.choices
    node_id, input_name = _split_target(graph, spec.targets[0])
    class_type = graph[node_id]["class_type"]
    info = (await client.object_info(class_type)).get(class_type) or {}
    inputs = {**(info.get("input", {}).get("required") or {}),
              **(info.get("input", {}).get("optional") or {})}
    return combo_options(inputs.get(input_name))


def match_choice(value: Any, choices: list[str]) -> str:
    """Exact match, then case-insensitive, then the label before " (" ("16:9" ->
    "16:9 (Widescreen)"), then a unique substring."""
    text = str(value).strip()
    if text in choices:
        return text
    low = text.lower()
    for test in (
        lambda c: c.lower() == low,
        lambda c: c.split(" (")[0].strip().lower() == low,
        lambda c: low in c.lower(),
    ):
        found = [c for c in choices if test(c)]
        if len(found) == 1:
            return found[0]
    raise WorkflowError(f"Invalid value {text!r}. Options: {', '.join(choices)}")


@dataclass
class InputFile:
    name: str
    """Value for the Load* node (a file in ComfyUI's input dir, or "<path> [output]")."""
    size: tuple[int, int] | None = None


def parse_file_ref(value: str) -> tuple[str, str, str]:
    """ "sub/name.png [output]" -> ("name.png", "sub", "output"); default type is input."""
    m = re.match(r"^(.*?)\s*\[(input|output|temp)\]$", value)
    path, type_ = (m.group(1), m.group(2)) if m else (value, "input")
    sub, _, name = path.rpartition("/")
    return name, sub, type_


async def _decode_source(client: ComfyClient, value: str) -> tuple[bytes | None, str]:
    """Bytes for a URL, data URI or raw base64 value; None when it is a ComfyUI filename."""
    ext = ".png"
    if value.startswith(("http://", "https://")):
        return await client.fetch_url(value), Path(value.split("?")[0]).suffix or ext
    if value.startswith("data:"):
        header, _, b64 = value.partition(",")
        return base64.b64decode(b64), mimetypes.guess_extension(header[5:].split(";")[0]) or ext
    if len(value) > 256:
        try:
            return base64.b64decode(value, validate=True), ext
        except binascii.Error as e:
            raise WorkflowError("File value looks like base64 but could not be decoded") from e
    return None, ext


async def prepare_input_file(
    client: ComfyClient, value: str, flatten: str | None = None, want_size: bool = False
) -> InputFile:
    """Upload a URL/data URI/base64 value (or reuse a ComfyUI filename), optionally flattening
    transparency and reading the image size."""
    data, ext = await _decode_source(client, value)
    fresh = data is not None
    if data is None:
        is_image = mime_type(parse_file_ref(value)[0]).startswith("image/")
        if not (flatten or want_size) or not is_image:
            return InputFile(value)
        try:
            data = (await client.view(*parse_file_ref(value))).content
        except ComfyUIError:
            return InputFile(value)  # let ComfyUI report a missing file
    img = imaging.open_image(data)
    size = img.size if img is not None else None
    if img is not None and flatten and imaging.has_alpha(img):
        data, ext, fresh = imaging.flatten(img, flatten), ".png", True
    if not fresh:
        return InputFile(value, size)
    result = await client.upload(data, f"mcp_{uuid.uuid4().hex[:12]}{ext}")
    sub = result.get("subfolder")
    return InputFile(f"{sub}/{result['name']}" if sub else result["name"], size)


async def upload_input_file(client: ComfyClient, value: str) -> str:
    """Accepts a URL, a data URI, raw base64, or the name of a file already in ComfyUI's input
    dir. Returns the filename to use in a Load* node."""
    return (await prepare_input_file(client, value)).name


async def build_graph(
    registry: WorkflowRegistry,
    entry: WorkflowEntry,
    params: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
    randomize: bool = False,
    graph: dict | None = None,
) -> tuple[dict, dict[str, Any], list[str]]:
    """Returns (graph, applied values, ignored param names). `graph` overrides the entry's file."""
    graph = copy.deepcopy(graph) if graph is not None else await registry.load_graph(entry)
    params = {k: v for k, v in (params or {}).items() if v is not None}
    applied: dict[str, Any] = {}
    ignored = [k for k in params if k not in entry.params]
    touched: set[str] = set()
    missing: list[str] = []
    prune: list[str] = []
    remove: set[str] = set()

    def flag(name: str) -> bool:
        spec = entry.params.get(name)
        value = params.get(name, spec.default if spec else None)
        return value is not None and coerce(value, "bool")

    # Files first: uploading them may also give the size used by the size rule.
    size = entry.size
    size_names = (size.get("width", "width"), size.get("height", "height"))
    derive_size = bool(size) and not any(n in params for n in size_names)
    size_from = size.get("from_image") if derive_size else None
    files: dict[str, list[InputFile]] = {}
    for name, spec in entry.params.items():
        if spec.type not in ("file", "file_list"):
            continue
        value = params.get(name, spec.default)
        values = [] if value is None else value if isinstance(value, list) else [value]
        if spec.type == "file_list" and len(values) > len(spec.targets):
            raise WorkflowError(f"'{name}' accepts at most {len(spec.targets)} files")
        files[name] = [
            await prepare_input_file(registry.client, str(v), spec.flatten_alpha,
                                     want_size=(name == size_from and i == 0))
            for i, v in enumerate(values[:1] if spec.type == "file" else values)
        ]
    if derive_size:
        first = (files.get(size_from) or [None])[0] if size_from else None
        if (dims := _derive_size(size, params, first.size if first else None)) is not None:
            params[size_names[0]], params[size_names[1]] = dims

    for name, spec in entry.params.items():
        value = params.get(name, spec.default)
        if value is not None and spec.append_when:
            extra = [text.strip() for flag_name, text in spec.append_when.items() if flag(flag_name)]
            base = str(value).rstrip()
            if extra and base and base[-1] not in ".!?":
                base += "."
            value = " ".join([base, *extra])
        if spec.type == "file_list":
            uploaded = [f.name for f in files[name]]
            for target, v in zip(spec.targets, uploaded, strict=False):
                set_input(graph, target, v)
                touched.add(target)
            if spec.prune_missing:
                prune.extend(spec.targets[len(uploaded):])
            if uploaded:
                applied[name] = uploaded
            else:
                missing.append(name)
            continue
        if spec.type == "seed" and (value is None or value in ("random", -1)):
            value = random.randint(0, MAX_SEED)
        if value is None:
            missing.append(name)
            if spec.prune_missing:
                prune.extend(spec.targets)
            continue
        if spec.type == "file":
            value = files[name][0].name
        elif spec.type == "choice":
            choices = await param_choices(registry.client, graph, spec)
            value = match_choice(value, choices) if choices else str(value)
        else:
            value = coerce(value, spec.type)
            if spec.multiple_of and isinstance(value, int) and not isinstance(value, bool):
                value = imaging.round_to_multiple(value, spec.multiple_of)
        if spec.remove_when_false or spec.remove_when_true:
            refs = spec.remove_when_true if coerce(value, "bool") else spec.remove_when_false
            remove.update(resolve_node(graph, ref) for ref in refs)
        for target in spec.targets:
            set_input(graph, target, value)
            touched.add(target)
        applied[name] = value

    # Conditional overrides never replace a value the caller set explicitly.
    explicit = set(touched)
    rules = [entry.when_missing.get(n) for n in missing] + [entry.when_set.get(n) for n in params]
    for rule in filter(None, rules):
        for target, value in rule.items():
            if target not in explicit:
                set_input(graph, target, value)
                touched.add(target)

    for target, value in (overrides or {}).items():
        set_input(graph, target, value)
        touched.add(target)
        applied[target] = value

    for label, ref in entry.outputs.items():
        graph[resolve_node(graph, ref)].setdefault("_meta", {})["mcp_output"] = label
    if prune or remove:
        prune_nodes(graph, remove | {resolve_node(graph, t.rsplit(".", 1)[0]) for t in prune})
    if remove and entry.outputs and not any(
        (n.get("_meta") or {}).get("mcp_output") for n in graph.values()
    ):
        raise WorkflowError("Every output of this workflow was disabled; enable at least one.")
    if randomize:
        randomize_seeds(graph, skip=touched)
    await resolve_model_paths(registry.client, graph)
    return graph, applied, ignored


def _derive_size(rule: dict, params: dict, image_size: tuple[int, int] | None
                 ) -> tuple[int, int] | None:
    """aspect_ratio/megapixels -> pixels (ratio defaults to the input image's, area to
    default_megapixels); else the input image size capped at max_side; else None (defaults)."""
    ratio_value = params.get(rule.get("aspect_ratio") or "", None)
    mp_value = params.get(rule.get("megapixels") or "", None)
    if ratio_value is not None or mp_value is not None:
        try:
            if ratio_value is not None:
                ratio = imaging.parse_aspect_ratio(ratio_value)
            else:
                ratio = image_size[0] / image_size[1] if image_size else 1.0
        except ValueError as e:
            raise WorkflowError(str(e)) from e
        megapixels = coerce(mp_value, "float") if mp_value is not None else float(
            rule.get("default_megapixels", 1.0))
        if not 0 < megapixels <= 16:
            raise WorkflowError("megapixels must be between 0 and 16")
        return imaging.size_for(ratio, megapixels)
    if image_size:
        return imaging.fit_size(image_size, int(rule.get("max_side", 2048)))
    return None


def _is_link(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[1], int)


def prune_nodes(graph: dict, node_ids: set[str]) -> None:
    """Delete nodes and, transitively, their dependents. Dynamic/autogrow inputs (dotted names
    like "images.image_3") are optional, so only that input is dropped, not the whole node."""
    removed = set(node_ids)
    changed = True
    while changed:
        changed = False
        for nid, node in graph.items():
            if nid in removed:
                continue
            inputs = node.get("inputs") or {}
            for key, value in list(inputs.items()):
                if _is_link(value) and str(value[0]) in removed:
                    if "." in key:
                        del inputs[key]
                    else:
                        removed.add(nid)
                        changed = True
                        break
    for nid in removed:
        graph.pop(nid, None)


# --- outputs ---------------------------------------------------------------------------------


LinkFn = Callable[[str, str, str], dict[str, str]]


def _output_label(graph: dict, node_id: str) -> str | None:
    meta = (graph.get(node_id) or {}).get("_meta") or {}
    if meta.get("mcp_output"):
        return meta["mcp_output"]
    title = meta.get("title") or ""
    return title if title.startswith("[$") else None


def extract_outputs(entry: dict, links: LinkFn, include_temp: bool = False) -> list[dict]:
    files = []
    prompt = entry.get("prompt")
    graph = prompt[2] if isinstance(prompt, list) and len(prompt) > 2 else None
    graph = graph if isinstance(graph, dict) else {}
    for node_id, node_out in (entry.get("outputs") or {}).items():
        label = _output_label(graph, node_id)
        for kind, items in node_out.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or "filename" not in item:
                    continue
                type_ = item.get("type", "output")
                if type_ == "temp" and not include_temp:
                    continue
                subfolder = item.get("subfolder", "")
                files.append({
                    "node": node_id,
                    **({"label": label} if label else {}),
                    "kind": kind,
                    "filename": item["filename"],
                    "subfolder": subfolder,
                    "type": type_,
                    "mime_type": mime_type(item["filename"]),
                    **links(item["filename"], subfolder, type_),
                })
    return files


def extract_texts(entry: dict, max_len: int = 500) -> list[dict]:
    """Text outputs (PreviewAny, LLM nodes...), truncated."""
    texts = []
    for node_id, node_out in (entry.get("outputs") or {}).items():
        for item in node_out.get("text") or []:
            if isinstance(item, str) and item.strip():
                text = item if len(item) <= max_len else item[:max_len] + f"... [{len(item)} chars]"
                texts.append({"node": node_id, "text": text})
    return texts


def extract_error(entry: dict) -> dict | None:
    for msg in (entry.get("status") or {}).get("messages") or []:
        if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error":
            d = msg[1]
            return {k: d.get(k) for k in ("node_id", "node_type", "exception_type", "exception_message")}
    return None
