"""
Generate the example workflows from the live node definitions.

Hand-written workflow JSON drifts out of date the moment a widget is added.
This builds both flavours instead:

* ``examples/remerged_video_workflow.json`` - the canvas format you drag in.
* ``examples/remerged_video_api.json``       - the /prompt payload.
"""

import importlib.util
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def find_package_dir():
    """Works whether the tests sit inside the package or beside it."""
    for candidate in (
        os.path.join(HERE, "comfyui-remerged"),
        os.path.join(os.path.dirname(HERE), "comfyui-remerged"),
        os.path.join(os.path.dirname(os.path.dirname(HERE)), "comfyui-remerged"),
    ):
        if os.path.isfile(os.path.join(candidate, "__init__.py")):
            return candidate
    raise SystemExit("Could not find the comfyui-remerged package.")


PKG_DIR = find_package_dir()

# Minimal ComfyUI stubs so the package imports outside of ComfyUI.
_SANDBOX = os.path.join(HERE, "_gen_sandbox")
_USER = os.path.join(_SANDBOX, "user")
_OUT = os.path.join(_SANDBOX, "output")
os.makedirs(_USER, exist_ok=True)
os.makedirs(_OUT, exist_ok=True)

folder_paths = types.ModuleType("folder_paths")
folder_paths.get_user_directory = lambda: _USER
folder_paths.get_output_directory = lambda: _OUT
sys.modules["folder_paths"] = folder_paths

comfy = types.ModuleType("comfy")
comfy_utils = types.ModuleType("comfy.utils")
comfy_utils.ProgressBar = lambda total: None
comfy_mm = types.ModuleType("comfy.model_management")
comfy_mm.throw_exception_if_processing_interrupted = lambda: None
comfy.model_management = comfy_mm
comfy.utils = comfy_utils
sys.modules["comfy"] = comfy
sys.modules["comfy.utils"] = comfy_utils
sys.modules["comfy.model_management"] = comfy_mm

_spec = importlib.util.spec_from_file_location(
    "comfyui_remerged",
    os.path.join(PKG_DIR, "__init__.py"),
    submodule_search_locations=[PKG_DIR],
)
pkg = importlib.util.module_from_spec(_spec)
sys.modules["comfyui_remerged"] = pkg
_spec.loader.exec_module(pkg)

NODES = pkg.NODE_CLASS_MAPPINGS


def widget_values(cls, overrides=None):
    """The widget values for a node, in the order ComfyUI expects them."""
    overrides = overrides or {}
    spec = cls.INPUT_TYPES()
    values = []
    for name, definition in spec["required"].items():
        kind = definition[0]
        options = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
        if name in overrides:
            value = overrides[name]
        elif kind == "STRING":
            value = options.get("default", "")
        elif kind in ("INT", "FLOAT"):
            value = options.get("default", 0)
        elif kind == "BOOLEAN":
            value = options.get("default", False)
        else:
            value = options.get("default")
        values.append(value)
        if name == "seed":
            # ComfyUI's control-after-generate widget sits right after the seed.
            values.append("randomize")
    return values


def outputs_for(cls):
    result = []
    for index, (kind, name) in enumerate(zip(cls.RETURN_TYPES, cls.RETURN_NAMES)):
        result.append({"name": name, "type": kind, "links": None, "slot_index": index})
    return result


def sockets_for(cls):
    spec = cls.INPUT_TYPES()
    result = []
    for name, definition in spec.get("optional", {}).items():
        kind = definition[0]
        result.append({"name": name, "type": kind, "link": None, "shape": 7})
    return result


# --------------------------------------------------------------------------- #
# canvas workflow
# --------------------------------------------------------------------------- #
gen_cls = NODES["RemergedVideoGenerate"]
quote_cls = NODES["RemergedQuote"]
acct_cls = NODES["RemergedAccount"]

gen_id, quote_id, acct_id = 1, 2, 3

gen_node = {
    "id": gen_id,
    "type": "RemergedVideoGenerate",
    "pos": [80, 140],
    "size": [430, 760],
    "flags": {},
    "order": 0,
    "mode": 0,
    "inputs": sockets_for(gen_cls),
    "outputs": outputs_for(gen_cls),
    "properties": {"Node name for S&R": "RemergedVideoGenerate"},
    "widgets_values": widget_values(
        gen_cls,
        {
            "prompt": "A hummingbird hovering at a red flower, slow motion",
            "model": "seedance-2.5",
            "duration_s": 5,
            "aspect": "16:9",
            "mode": "default",
            "audio": True,
            "use_seed": False,
            "variations": 1,
        },
    ),
}

quote_node = {
    "id": quote_id,
    "type": "RemergedQuote",
    "pos": [80, -120],
    "size": [430, 190],
    "flags": {},
    "order": 1,
    "mode": 0,
    "inputs": sockets_for(quote_cls),
    "outputs": outputs_for(quote_cls),
    "properties": {"Node name for S&R": "RemergedQuote"},
    "widgets_values": widget_values(
        quote_cls,
        {
            "prompt": "A hummingbird hovering at a red flower, slow motion",
            "model": "seedance-2.5",
            "duration_s": 5,
            "aspect": "16:9",
            "mode": "default",
            "audio": True,
        },
    ),
}

acct_node = {
    "id": acct_id,
    "type": "RemergedAccount",
    "pos": [580, -120],
    "size": [320, 110],
    "flags": {},
    "order": 2,
    "mode": 0,
    "inputs": sockets_for(acct_cls),
    "outputs": outputs_for(acct_cls),
    "properties": {"Node name for S&R": "RemergedAccount"},
    "widgets_values": widget_values(acct_cls),
}

workflow = {
    "id": "1f2a9a3c-6f2f-4f7d-9a3e-remerged000001",
    "revision": 0,
    "last_node_id": acct_id,
    "last_link_id": 0,
    "nodes": [quote_node, acct_node, gen_node],
    "links": [],
    "groups": [],
    "config": {},
    "extra": {},
    "version": 0.4,
}

# --------------------------------------------------------------------------- #
# api payload
# --------------------------------------------------------------------------- #
api_prompt = {
    "1": {
        "class_type": "RemergedVideoGenerate",
        "inputs": {
            "prompt": "A hummingbird hovering at a red flower, slow motion",
            "model": "seedance-2.5",
            "duration_s": 5,
            "aspect": "16:9",
            "mode": "default",
            "audio": True,
            "seed": 0,
            "use_seed": False,
            "variations": 1,
            "camera_fixed": False,
            "negative_prompt": "",
            "prompt_expansion": True,
            "draft": False,
            "image_urls": "",
            "uploaded_inputs": "",
            "extra_inputs": "",
            "webhook_url": "",
            "poll_interval": 10,
            "timeout_minutes": 30,
            "max_credits": 0,
            "provider": "remerged",
            "api_key": "",
        },
    },
    "2": {
        "class_type": "RemergedQuote",
        "inputs": {
            "prompt": "A hummingbird hovering at a red flower, slow motion",
            "model": "seedance-2.5",
            "duration_s": 5,
            "aspect": "16:9",
            "mode": "default",
            "audio": True,
            "provider": "remerged",
            "api_key": "",
        },
    },
    "3": {
        "class_type": "RemergedAccount",
        "inputs": {"provider": "remerged", "api_key": ""},
    },
}

# --------------------------------------------------------------------------- #
# validate the api payload against the node definitions
# --------------------------------------------------------------------------- #
errors = []
for node_id, node in api_prompt.items():
    cls = NODES[node["class_type"]]
    spec = cls.INPUT_TYPES()
    required = spec["required"]
    optional = spec.get("optional", {})
    for key, value in node["inputs"].items():
        if key in required:
            kind, options = required[key][0], required[key][1]
            if isinstance(kind, list) and value not in kind:
                errors.append(f"node {node_id}: '{key}' value {value!r} not in {kind}")
            if kind == "INT" and not isinstance(value, int):
                errors.append(f"node {node_id}: '{key}' should be an INT")
            if kind == "BOOLEAN" and not isinstance(value, bool):
                errors.append(f"node {node_id}: '{key}' should be a BOOLEAN")
            if kind == "STRING" and not isinstance(value, str):
                errors.append(f"node {node_id}: '{key}' should be a STRING")
        elif key in optional:
            pass
        else:
            errors.append(f"node {node_id}: unknown input '{key}'")
    missing = [key for key in required if key not in node["inputs"]]
    if missing:
        errors.append(f"node {node_id}: missing required inputs {missing}")

if errors:
    print("VALIDATION FAILED")
    for item in errors:
        print("  -", item)
    sys.exit(1)

out_dir = os.path.join(PKG_DIR, "examples")
os.makedirs(out_dir, exist_ok=True)

with open(os.path.join(out_dir, "remerged_video_workflow.json"), "w", encoding="utf-8") as handle:
    json.dump(workflow, handle, indent=2)
    handle.write("\n")

with open(os.path.join(out_dir, "remerged_video_api.json"), "w", encoding="utf-8") as handle:
    json.dump({"prompt": api_prompt}, handle, indent=2)
    handle.write("\n")

print("examples written and validated:")
print("  examples/remerged_video_workflow.json")
print("  examples/remerged_video_api.json")
print("widget counts:", {n: len(v["widgets_values"]) for n, v in
                         (("generate", gen_node), ("quote", quote_node), ("account", acct_node))})
