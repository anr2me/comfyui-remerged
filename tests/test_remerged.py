"""
Offline test suite for the custom nodes.

Runs the real node code against a local mock of two different services:

* a Remerged-shaped one, and
* a generic OpenAPI 3.1 service with completely different field names,
  endpoints and status values.

No key, no network, no credits.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# ComfyUI / torch stubs
# --------------------------------------------------------------------------- #
_SANDBOX = tempfile.mkdtemp(prefix="comfy-remerged-test-")
_USER_DIR = os.path.join(_SANDBOX, "user")
_OUT_DIR = os.path.join(_SANDBOX, "output")
os.makedirs(_USER_DIR, exist_ok=True)
os.makedirs(_OUT_DIR, exist_ok=True)

folder_paths = types.ModuleType("folder_paths")
folder_paths.get_user_directory = lambda: _USER_DIR
folder_paths.get_output_directory = lambda: _OUT_DIR
sys.modules["folder_paths"] = folder_paths


class _ProgressBar:
    def __init__(self, total):
        self.total = total
        self.value = 0

    def update_absolute(self, value, total=None, preview=None):
        self.value = value


comfy = types.ModuleType("comfy")
comfy_utils = types.ModuleType("comfy.utils")
comfy_utils.ProgressBar = _ProgressBar
comfy_mm = types.ModuleType("comfy.model_management")
comfy_mm.throw_exception_if_processing_interrupted = lambda: None
comfy_server = types.ModuleType("comfy.server")


class _PromptServer:
    instance = None


comfy_server.PromptServer = _PromptServer
comfy.model_management = comfy_mm
comfy.utils = comfy_utils
comfy.server = comfy_server
sys.modules["comfy"] = comfy
sys.modules["comfy.utils"] = comfy_utils
sys.modules["comfy.model_management"] = comfy_mm
sys.modules["comfy.server"] = comfy_server


class _VideoFromFile:
    def __init__(self, file, **kwargs):
        self.file = file

    def __repr__(self):
        return f"VideoFromFile({self.file!r})"


comfy_api = types.ModuleType("comfy_api")
comfy_api_input_impl = types.ModuleType("comfy_api.input_impl")
comfy_api_input_impl.VideoFromFile = _VideoFromFile
sys.modules["comfy_api"] = comfy_api
sys.modules["comfy_api.input_impl"] = comfy_api_input_impl

# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
STATE = {
    "polls": 0,
    "jobs": {},
    "uploads": {},
    "job_body": None,
    "quote_body": None,
    "force_status": None,
    "force_error": None,
    "gen_jobs": {},
    "gen_job_body": None,
}

FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512

#: A deliberately different service: different paths, different field names,
#: a different envelope, different status words.
GENERIC_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Generic Video API", "version": "1"},
    "servers": [{"url": f"http://127.0.0.1:0/genapi"}],
    "security": [{"apiKey": []}],
    "components": {
        "securitySchemes": {"apiKey": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "RenderRequest": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "model_name": {"type": "string"},
                    "duration_seconds": {"type": "integer"},
                    "aspect_ratio": {"type": "string"},
                    "with_audio": {"type": "boolean"},
                    "random_seed": {"type": "integer"},
                    "batch_count": {"type": "integer"},
                    "callback": {"type": "string"},
                },
            },
            "RenderJob": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string"},
                    "state": {
                        "type": "string",
                        "enum": ["queued", "in_progress", "succeeded", "failed"],
                    },
                    "output_url": {"type": "string"},
                    "error_message": {"type": "string"},
                    "cost_credits": {"type": "integer"},
                },
            },
        },
    },
    "paths": {
        "/v1/renders": {
            "post": {
                "operationId": "createRender",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/RenderRequest"}}
                    }
                },
                "responses": {
                    "201": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "request_id": {"type": "string"},
                                        "request_ids": {"type": "array", "items": {"type": "string"}},
                                        "cost_credits": {"type": "integer"},
                                    },
                                }
                            }
                        }
                    }
                },
            },
            "get": {
                "operationId": "listRenders",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "jobs": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/RenderJob"},
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
            },
        },
        "/v1/renders/{renderId}": {
            "get": {
                "operationId": "getRender",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "data": {"$ref": "#/components/schemas/RenderJob"}
                                    },
                                }
                            }
                        }
                    }
                },
            }
        },
        "/v1/estimate": {
            "post": {
                "operationId": "estimateRender",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/RenderRequest"}}
                    }
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "cost": {"type": "integer"},
                                        "usd_cost": {"type": "number"},
                                    },
                                }
                            }
                        }
                    }
                },
            }
        },
        "/v1/catalog": {
            "get": {
                "operationId": "listCatalog",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "id": {"type": "string"},
                                                    "modes": {"type": "array", "items": {"type": "string"}},
                                                },
                                            },
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
            }
        },
        "/v1/files": {
            "post": {
                "operationId": "createFile",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "mime_type": {"type": "string"},
                                    "size_bytes": {"type": "integer"},
                                },
                            }
                        }
                    }
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "file_key": {"type": "string"},
                                        "upload_target": {"type": "string"},
                                    },
                                }
                            }
                        }
                    }
                },
            }
        },
        "/v1/wallet": {
            "get": {
                "operationId": "getWallet",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "balance_credits": {"type": "integer"},
                                        "tier": {"type": "string"},
                                    },
                                }
                            }
                        }
                    }
                },
            }
        },
    },
}


# --------------------------------------------------------------------------- #
# mock server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload=None, raw=None, content_type="application/json"):
        if raw is None:
            raw = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        raw_path = self.path
        path = raw_path.split("?")[0]

        # ---- generic service ----
        if path == "/genapi/openapi.json":
            spec = json.loads(json.dumps(GENERIC_SPEC))
            spec["servers"][0]["url"] = f"http://127.0.0.1:{PORT}/genapi"
            return self._send(200, spec)
        if path == "/genapi/v1/catalog":
            return self._send(200, {"items": [{"id": "model-x", "modes": ["fast", "hd"]}]})
        if path == "/genapi/v1/wallet":
            return self._send(200, {"balance_credits": 99, "tier": "gold"})
        if path == "/genapi/v1/renders":
            return self._send(200, {"jobs": list(STATE["gen_jobs"].values())})
        if path.startswith("/genapi/v1/renders/"):
            job_id = path.rsplit("/", 1)[-1]
            job = STATE["gen_jobs"].get(job_id)
            if not job:
                return self._send(404, {"error": "not_found", "message": "No such render."})
            job["polls"] = job.get("polls", 0) + 1
            if job["polls"] >= 2:
                job["state"] = "succeeded"
                job["output_url"] = f"http://127.0.0.1:{PORT}/video/out.mp4"
            else:
                job["state"] = "in_progress"
            return self._send(200, {"data": job})

        # ---- remerged-shaped service ----
        if path == "/api/v1/models":
            return self._send(
                200,
                {
                    "models": [
                        {"id": "seedance-2.5", "modes": ["480p", "720p", "1080p", "480p-1080p", "480p-4k"]},
                        {"id": "wan-2.6", "modes": ["720p", "1080p"]},
                    ]
                },
            )
        if path == "/api/v1/account":
            return self._send(200, {"credits": 250, "plan": "pro", "topup_url": "https://remerged.ai/topup"})
        if path.startswith("/api/v1/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = STATE["jobs"].get(job_id)
            if not job:
                return self._send(404, {"error": "not_found", "message": "No such job."})
            job["polls"] = job.get("polls", 0) + 1
            STATE["polls"] += 1
            if STATE["force_status"]:
                job["status"] = STATE["force_status"]
            elif job["polls"] >= 2:
                job["status"] = "ready"
                job["video_url"] = f"http://127.0.0.1:{PORT}/video/out.mp4"
                job["poster_url"] = f"http://127.0.0.1:{PORT}/poster/out.jpg"
            else:
                job["status"] = "generating"
            return self._send(200, {"job": job})
        if path.startswith("/api/v1/jobs"):
            limit = 20
            if "limit=" in raw_path:
                try:
                    limit = int(raw_path.split("limit=")[1].split("&")[0])
                except Exception:
                    pass
            return self._send(200, {"jobs": list(STATE["jobs"].values())[:limit]})
        if path == "/video/out.mp4":
            return self._send(200, raw=FAKE_MP4, content_type="video/mp4")
        if path == "/poster/out.jpg":
            return self._send(200, raw=b"\xff\xd8\xff\xe0JFIF", content_type="image/jpeg")
        return self._send(404, {"error": "not_found"})

    def do_PUT(self):
        data = self._body()
        STATE["uploads"][self.path] = data
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = self.path
        raw = self._body()
        payload = json.loads(raw) if raw else {}

        if STATE["force_error"]:
            code, body = STATE["force_error"]
            return self._send(code, body)

        # ---- generic service ----
        if path == "/genapi/v1/renders":
            STATE["gen_job_body"] = payload
            variations = int(payload.get("batch_count") or 1)
            ids = []
            for _ in range(variations):
                job_id = f"req_{len(STATE['gen_jobs']) + 1}"
                STATE["gen_jobs"][job_id] = {
                    "identifier": job_id,
                    "state": "queued",
                    "polls": 0,
                    "output_url": None,
                    "error_message": None,
                    "cost_credits": 42,
                }
                ids.append(job_id)
            return self._send(201, {"request_id": ids[0], "request_ids": ids, "cost_credits": 42})
        if path == "/genapi/v1/estimate":
            return self._send(200, {"cost": 42, "usd_cost": 0.42})
        if path == "/genapi/v1/files":
            return self._send(
                200,
                {
                    "file_key": "uploads/9/9",
                    "upload_target": f"http://127.0.0.1:{PORT}/upload/gen",
                },
            )

        # ---- remerged-shaped service ----
        if path == "/api/v1/quote":
            STATE["quote_body"] = payload
            return self._send(200, {"credits": 18, "usd": 0.18, "per_video_credits": 18})
        if path == "/api/v1/jobs":
            STATE["job_body"] = payload
            variations = int(payload.get("variations") or 1)
            ids = []
            for _ in range(variations):
                job_id = f"job_{len(STATE['jobs']) + 1}"
                STATE["jobs"][job_id] = {
                    "id": job_id,
                    "status": "generating",
                    "polls": 0,
                    "kind": "generate",
                    "prompt": payload.get("prompt"),
                    "model": payload.get("model"),
                    "duration_s": payload.get("durationS"),
                    "aspect": payload.get("aspect"),
                    "quote_credits": 18,
                    "video_url": None,
                    "poster_url": None,
                    "error": None,
                    "draft": bool(payload.get("draft")),
                    "finish_by": "2026-11-01T00:00:00Z" if payload.get("draft") else None,
                    "created_at": int(time.time() * 1000),
                }
                ids.append(job_id)
            return self._send(201, {"id": ids[0], "ids": ids, "credits": 18 * variations})
        if path == "/api/v1/uploads":
            key = f"uploads/{payload['bytes']}/{abs(hash(payload['content_type'])) % 10**8}"
            return self._send(
                200,
                {
                    "key": key,
                    "url": f"http://127.0.0.1:{PORT}/upload/{key}",
                    "method": "PUT",
                    "max_bytes": 50 * 1024 * 1024,
                },
            )
        if path.endswith("/finish"):
            job_id = path.split("/jobs/")[1].split("/")[0]
            new_id = f"{job_id}_finished"
            STATE["jobs"][new_id] = dict(STATE["jobs"][job_id], id=new_id, status="generating", polls=0)
            return self._send(201, {"id": new_id, "ids": [new_id], "credits": 54})
        return self._send(404, {"error": "not_found"})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

os.environ["REMERGED_API_KEY"] = "rm_live_testkey"
os.environ["REMERGED_BASE_URL"] = f"http://127.0.0.1:{PORT}/api/v1"

# --------------------------------------------------------------------------- #
# load the package
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))


def find_package_dir():
    """Works whether the tests sit inside the package or beside it."""
    candidates = [
        os.path.join(HERE, "comfyui-remerged"),
        os.path.join(os.path.dirname(HERE), "comfyui-remerged"),
        os.path.join(os.path.dirname(os.path.dirname(HERE)), "comfyui-remerged"),
    ]
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "__init__.py")):
            return candidate
    raise SystemExit("Could not find the comfyui-remerged package.")


PKG_DIR = find_package_dir()

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "comfyui_remerged",
    os.path.join(PKG_DIR, "__init__.py"),
    submodule_search_locations=[PKG_DIR],
)
pkg = importlib.util.module_from_spec(_spec)
sys.modules["comfyui_remerged"] = pkg
_spec.loader.exec_module(pkg)

from comfyui_remerged.remerged_nodes import api as rapi  # noqa: E402
from comfyui_remerged.remerged_nodes import config as rconfig  # noqa: E402
from comfyui_remerged.remerged_nodes import profile as rprofile  # noqa: E402

NODES = pkg.NODE_CLASS_MAPPINGS
NAMES = pkg.NODE_DISPLAY_NAME_MAPPINGS

import numpy as np  # noqa: E402

PASSED = []
FAILED = []


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  FAIL  {label} :: {detail}")


def fake_image(height=16, width=24):
    """A stand-in for a ComfyUI IMAGE tensor: (B, H, W, C) floats in 0..1."""
    arr = np.zeros((1, height, width, 3), dtype="float32")
    arr[..., 0] = np.linspace(0, 1, width)[None, :]
    arr[..., 1] = 0.5
    arr[..., 2] = 0.25
    return arr


def base_kwargs(**overrides):
    kwargs = dict(
        prompt="A hummingbird hovering at a red flower, slow motion",
        model="seedance-2.5",
        duration_s=5,
        aspect="16:9",
        mode="default",
        audio=True,
        seed=12345,
        use_seed=False,
        variations=1,
        camera_fixed=False,
        negative_prompt="",
        prompt_expansion=True,
        draft=False,
        image_urls="",
        uploaded_inputs="",
        extra_inputs="",
        webhook_url="",
        poll_interval=1,
        timeout_minutes=5,
        max_credits=0,
        provider="remerged",
        api_key="",
    )
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------- #
print("\n=== 1. package surface ===")
check("9 node classes registered", len(NODES) == 9, f"got {len(NODES)}")
check("display names match", len(NAMES) == len(NODES))
check("main node present", "RemergedVideoGenerate" in NODES, ",".join(sorted(NODES)))
check("provider node present", "RemergedProvider" in NODES, ",".join(sorted(NODES)))

# --------------------------------------------------------------------------- #
print("\n=== 2. INPUT_TYPES validity ===")
for name, cls in NODES.items():
    spec = cls.INPUT_TYPES()
    ok = isinstance(spec, dict) and "required" in spec
    check(f"{name}.INPUT_TYPES shape", ok, str(type(spec)))
    returns = getattr(cls, "RETURN_TYPES", None)
    check(
        f"{name} RETURN_TYPES/RETURN_NAMES length",
        isinstance(returns, tuple) and len(returns) == len(cls.RETURN_NAMES),
        f"{returns} vs {cls.RETURN_NAMES}",
    )
    check(f"{name} has FUNCTION", hasattr(cls, "FUNCTION"))
    check(f"{name} has CATEGORY", hasattr(cls, "CATEGORY"))

gen_spec = NODES["RemergedVideoGenerate"].INPUT_TYPES()
check("prompt is multiline", gen_spec["required"]["prompt"][1].get("multiline") is True)
check("seed widget named 'seed'", "seed" in gen_spec["required"])
check(
    "seed has control_after_generate",
    gen_spec["required"]["seed"][1].get("control_after_generate") is True,
)
check(
    "optional inputs include base_url and images",
    set(gen_spec["optional"]) == {"base_url", "start_image", "end_image", "reference_images"},
    str(set(gen_spec["optional"])),
)
check("provider widget present", "provider" in gen_spec["required"])
check("extra_inputs escape hatch present", "extra_inputs" in gen_spec["required"])
check("model list has 7 entries", len(gen_spec["required"]["model"][0]) == 7, str(gen_spec["required"]["model"][0]))
check("duration bounds", gen_spec["required"]["duration_s"][1]["min"] == 4)
check("variations max 4", gen_spec["required"]["variations"][1]["max"] == 4)

for name, cls in NODES.items():
    spec = cls.INPUT_TYPES()
    if name == "RemergedProvider":
        continue
    check(f"{name} has a provider widget", "provider" in spec["required"], str(list(spec["required"])))

# --------------------------------------------------------------------------- #
print("\n=== 3. client error handling ===")
prof = rprofile.load_profile("remerged")
client = rapi.ApiClient(prof, "rm_live_testkey", base_url=f"http://127.0.0.1:{PORT}/api/v1")

try:
    client._request("GET", "/definitely-not-here")
    check("404 raises", False, "no exception")
except rapi.RemergedError as exc:
    check("404 raises RemergedError", True)
    check("404 keeps status", exc.status == 404, str(exc.status))
    check("404 keeps code", exc.code == "not_found", str(exc.code))

STATE["force_error"] = (
    402,
    {"error": "insufficient_credits", "message": "Not enough credits.", "topup_url": "https://remerged.ai/topup"},
)
try:
    client.create_job({"prompt": "x"})
    check("402 raises", False, "no exception")
except rapi.RemergedError as exc:
    check("402 raises RemergedError", True)
    check("402 code captured", exc.code == "insufficient_credits", str(exc.code))
    check("402 topup_url captured", exc.topup_url == "https://remerged.ai/topup", str(exc.topup_url))
STATE["force_error"] = None

# --------------------------------------------------------------------------- #
print("\n=== 4. upload round trip ===")
key = client.upload_bytes(b"PNGDATA", "image/png")
check("upload returns a valid key", key.startswith("uploads/"), key)
check("bytes actually PUT", any(v == b"PNGDATA" for v in STATE["uploads"].values()))

# --------------------------------------------------------------------------- #
print("\n=== 5. quote ===")
quote_node = NODES["RemergedQuote"]()
credits, usd, per_video, raw = quote_node.quote(
    "a hummingbird", "seedance-2.5", 5, "16:9", "default", True, "remerged", ""
)
check("quote returns 18 credits", credits == 18, str(credits))
check("quote returns usd", usd == 0.18, str(usd))
check("quote body omits 'mode' when default", "mode" not in STATE["quote_body"], str(STATE["quote_body"]))

# --------------------------------------------------------------------------- #
print("\n=== 6. generate, no inputs ===")
STATE["polls"] = 0
gen = NODES["RemergedVideoGenerate"]()
video, video_path, job_ids, charged, status, info = gen.generate(**base_kwargs())
check("video is a native VIDEO", isinstance(video, _VideoFromFile), repr(video))
check("video file written", os.path.isfile(video_path), video_path)
check("file has the mock bytes", open(video_path, "rb").read() == FAKE_MP4)
check("job id returned", json.loads(job_ids) == ["job_1"], job_ids)
check("credits reported", charged == 18, str(charged))
check("status ready", status == "ready", status)
body = STATE["job_body"]
check("prompt sent", body["prompt"].startswith("A hummingbird"))
check("durationS sent", body["durationS"] == 5)
check("aspect sent", body["aspect"] == "16:9")
check("model sent", body["model"] == "seedance-2.5")
check("no seed when use_seed off", "seed" not in body, str(body))
check("no mode when default", "mode" not in body, str(body))
check("audio sent", body["audio"] is True)
check("info json parses", "jobs" in json.loads(info))

# --------------------------------------------------------------------------- #
print("\n=== 7. generate with a starting image ===")
STATE["polls"] = 0
gen.generate(**base_kwargs(start_image=fake_image()))
body = STATE["job_body"]
check(
    "start image uploaded and referenced",
    "images" in body and body["images"][0]["role"] == "first_frame",
    str(body.get("images")),
)
check("image key shape ok", body["images"][0]["key"].startswith("uploads/"), body["images"][0]["key"])

print("\n=== 7b. reference images ===")
STATE["polls"] = 0
gen.generate(**base_kwargs(reference_images=fake_image()))
body = STATE["job_body"]
check("reference role used", body["images"][0]["role"] == "reference", str(body.get("images")))

# --------------------------------------------------------------------------- #
print("\n=== 8. validation rules ===")
cases = [
    ("seed + variations rejected", base_kwargs(use_seed=True, variations=2), "cannot be used together"),
    ("draft only on seedance 2.5", base_kwargs(model="wan-2.6", draft=True), "Seedance 2.5"),
    (
        "frames and references conflict",
        base_kwargs(start_image=fake_image(), reference_images=fake_image()),
        "not both",
    ),
    (
        "single image models reject 2",
        base_kwargs(model="ltx-2.5", start_image=fake_image(), end_image=fake_image()),
        "one starting image",
    ),
    ("bad mode rejected", base_kwargs(model="seedance-2.5", mode="360p"), "not a mode"),
    ("unknown model rejected", base_kwargs(model="not-a-model"), "Unknown model"),
    ("empty prompt rejected", base_kwargs(prompt="  "), "prompt is empty"),
    ("21:9 rejected on wan", base_kwargs(model="wan-2.6", aspect="21:9"), "21:9"),
]
for label, kwargs, needle in cases:
    try:
        gen.generate(**kwargs)
        check(label, False, "no error raised")
    except Exception as exc:
        check(label, needle.lower() in str(exc).lower(), f"{type(exc).__name__}: {exc}")

print("\n=== 8b. wan duration snapping ===")
STATE["polls"] = 0
gen.generate(**base_kwargs(model="wan-2.6", duration_s=7))
check("wan 7s snapped to 10", STATE["job_body"]["durationS"] == 10, str(STATE["job_body"]["durationS"]))

print("\n=== 8c. seed is sent when asked ===")
STATE["polls"] = 0
gen.generate(**base_kwargs(use_seed=True, seed=999))
check("seed sent", STATE["job_body"].get("seed") == 999, str(STATE["job_body"].get("seed")))

print("\n=== 8d. credit budget ===")
try:
    gen.generate(**base_kwargs(max_credits=5))
    check("budget enforced", False, "no error")
except Exception as exc:
    check("budget enforced", "over your limit" in str(exc).lower(), str(exc))

print("\n=== 8e. extra_inputs escape hatch ===")
STATE["polls"] = 0
gen.generate(**base_kwargs(extra_inputs='{"custom_flag": true, "model": "override-model"}'))
check("extra field merged", STATE["job_body"].get("custom_flag") is True, str(STATE["job_body"]))
check("extra field overrides", STATE["job_body"].get("model") == "override-model", str(STATE["job_body"]))

# --------------------------------------------------------------------------- #
print("\n=== 9. variations ===")
STATE["polls"] = 0
video, video_path, job_ids, charged, status, info = gen.generate(**base_kwargs(variations=2))
check("variations body field", STATE["job_body"].get("variations") == 2, str(STATE["job_body"].get("variations")))
check("multiple job ids", len(json.loads(job_ids)) == 2, job_ids)

# --------------------------------------------------------------------------- #
print("\n=== 10. job status node ===")
status_node = NODES["RemergedJobStatus"]()
video, video_path, st, url, cr, err, raw = status_node.status("job_1", False, 1, 5, "remerged", "")
check("status returned", st == "ready", st)
check("video url returned", url.endswith("/video/out.mp4"), url)

print("\n=== 10b. job status waits ===")
STATE["polls"] = 0
STATE["jobs"]["job_1"]["polls"] = 0
STATE["jobs"]["job_1"]["status"] = "generating"
status_node.status("job_1", True, 1, 5, "remerged", "")
check("wait polled until ready", STATE["polls"] >= 2, str(STATE["polls"]))
check("wait ended on ready", STATE["jobs"]["job_1"]["status"] == "ready", STATE["jobs"]["job_1"]["status"])

# --------------------------------------------------------------------------- #
print("\n=== 11. finish draft ===")
STATE["jobs"]["draft_1"] = {"id": "draft_1", "status": "ready", "draft": True, "finish_by": "2026-11-01"}
finish_node = NODES["RemergedFinishDraft"]()
video, video_path, job_ids, st, cr, raw = finish_node.finish("draft_1", "1080p", "", 1, 5, "remerged", "")
check("finish produced a job id", bool(json.loads(job_ids)), job_ids)
check("finish status ready", st == "ready", st)

# --------------------------------------------------------------------------- #
print("\n=== 12. account node ===")
acct = NODES["RemergedAccount"]()
credits, plan, topup, in_flight, raw = acct.account("remerged", "")
check("account credits", credits == 250, str(credits))
check("account plan", plan == "pro", plan)
check("account topup url", "topup" in topup, topup)

# --------------------------------------------------------------------------- #
print("\n=== 13. models node ===")
models_node = NODES["RemergedModels"]()
ids, modes, details, raw = models_node.models(True, "remerged", "")
check("catalogue refreshed", "seedance-2.5" in ids, ids)
check("details include modes", "modes" in details, details[:120])
check("catalogue cached to disk", os.path.isfile(rconfig.catalogue_path("remerged")), rconfig.catalogue_path("remerged"))

print("\n=== 14. list jobs node ===")
list_node = NODES["RemergedListJobs"]()
job_ids, jobs, count = list_node.list_jobs(20, "remerged", "")
check("list jobs returns ids", count >= 1, str(count))

# --------------------------------------------------------------------------- #
print("\n=== 15. upload node ===")
up = NODES["RemergedUpload"]()
key, ctype, nbytes, raw = up.upload("", "image/png", "remerged", "", fake_image())
check("upload node returns key", key.startswith("uploads/"), key)
check("upload node content type", ctype == "image/png", ctype)
check("upload node byte count", nbytes > 0, str(nbytes))

# --------------------------------------------------------------------------- #
print("\n=== 16. config helpers ===")
check("slugify", rconfig.slugify("A hummingbird!") == "a-hummingbird", rconfig.slugify("A hummingbird!"))
check("all_modes contains default", rconfig.MODE_DEFAULT in rconfig.all_modes(), str(rconfig.all_modes()))
check("modes_for_model seedance", "1080p" in rconfig.modes_for_model("seedance-2.5"), str(rconfig.modes_for_model("seedance-2.5")))
check("normalise_duration clamps", rconfig.normalise_duration("seedance-2.5", 99) == 30)
check("normalise_duration min", rconfig.normalise_duration("seedance-2.5", 1) == 4)
check("api key from env", rconfig.get_api_key("", "remerged") == "rm_live_testkey")
check("api key override wins", rconfig.get_api_key("rm_live_other", "remerged") == "rm_live_other")

# --------------------------------------------------------------------------- #
print("\n=== 17. no API key -> clear error ===")
saved = os.environ.pop("REMERGED_API_KEY")
try:
    gen.generate(**base_kwargs())
    check("missing key raises", False, "no error")
except Exception as exc:
    check("missing key raises", "No API key" in str(exc), str(exc))
os.environ["REMERGED_API_KEY"] = saved

# =========================================================================== #
# GENERIC PROVIDER
# =========================================================================== #
print("\n=== 18. OpenAPI introspection ===")
spec = json.loads(json.dumps(GENERIC_SPEC))
spec["servers"][0]["url"] = f"http://127.0.0.1:{PORT}/genapi"

derived = rprofile.introspect(spec, name="genapi", label="Generic Video API")
endpoints = derived["endpoints"]
check("create endpoint found", endpoints.get("create", {}).get("path") == "/v1/renders", str(endpoints.get("create")))
check("status endpoint found", endpoints.get("status", {}).get("path") == "/v1/renders/{renderId}", str(endpoints.get("status")))
check("list endpoint found", endpoints.get("list", {}).get("path") == "/v1/renders", str(endpoints.get("list")))
check("models endpoint found", endpoints.get("models", {}).get("path") == "/v1/catalog", str(endpoints.get("models")))
check("quote endpoint found", endpoints.get("quote", {}).get("path") == "/v1/estimate", str(endpoints.get("quote")))
check("upload endpoint found", endpoints.get("upload", {}).get("path") == "/v1/files", str(endpoints.get("upload")))
check("account endpoint found", endpoints.get("account", {}).get("path") == "/v1/wallet", str(endpoints.get("account")))

fields = derived["job_fields"]
check("prompt mapped to 'text'", fields.get("prompt") == "text", str(fields.get("prompt")))
check("model mapped to 'model_name'", fields.get("model") == "model_name", str(fields.get("model")))
check("duration mapped to 'duration_seconds'", fields.get("duration") == "duration_seconds", str(fields.get("duration")))
check("aspect mapped to 'aspect_ratio'", fields.get("aspect") == "aspect_ratio", str(fields.get("aspect")))
check("audio mapped to 'with_audio'", fields.get("audio") == "with_audio", str(fields.get("audio")))
check("seed mapped to 'random_seed'", fields.get("seed") == "random_seed", str(fields.get("seed")))
check("variations mapped to 'batch_count'", fields.get("variations") == "batch_count", str(fields.get("variations")))
check("webhook mapped to 'callback'", fields.get("webhook_url") == "callback", str(fields.get("webhook_url")))

out_fields = derived["job_fields_out"]
check("job id mapped to 'identifier'", out_fields.get("id") == "identifier", str(out_fields.get("id")))
check("job status mapped to 'state'", out_fields.get("status") == "state", str(out_fields.get("status")))
check("job video mapped to 'output_url'", out_fields.get("video") == "output_url", str(out_fields.get("video")))
check("job error mapped to 'error_message'", out_fields.get("error") == "error_message", str(out_fields.get("error")))
check("status envelope is 'data'", derived["responses"]["status"]["envelope"] == "data", str(derived["responses"]))

status_values = derived["status_values"]
check("'succeeded' means ready", status_values.get("ready") == ["succeeded"], str(status_values.get("ready")))
check("'in_progress' means generating", status_values.get("generating") == ["in_progress"], str(status_values.get("generating")))
check("'failed' means failed", status_values.get("failed") == ["failed"], str(status_values.get("failed")))

check("auth detected as bearer", derived["auth"]["type"] == "bearer", str(derived["auth"]))
check("base url from servers[]", derived["base_url"].endswith("/genapi"), derived["base_url"])
check("unmapped fields warned about", any("mode" in w for w in derived["warnings"]), str(derived["warnings"]))

# --------------------------------------------------------------------------- #
print("\n=== 18b. introspecting the real Remerged spec reproduces the built-in ===")
# A condensed copy of https://remerged.ai/openapi.json
REMERGED_SPEC = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://remerged.ai/api/v1"}],
    "security": [{"apiKey": []}],
    "components": {
        "securitySchemes": {"apiKey": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "JobBody": {
                "type": "object",
                "required": ["prompt", "durationS", "aspect"],
                "properties": {
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "durationS": {"type": "integer"},
                    "aspect": {"type": "string"},
                    "audio": {"type": "boolean"},
                    "mode": {"type": "string"},
                    "images": {"type": "array"},
                    "seed": {"type": "integer"},
                    "cameraFixed": {"type": "boolean"},
                    "negativePrompt": {"type": "string"},
                    "promptExpansion": {"type": "boolean"},
                    "draft": {"type": "boolean"},
                    "variations": {"type": "integer"},
                    "upscaleOnly": {"type": "boolean"},
                    "sourceDurationS": {"type": "number"},
                    "image_urls": {"type": "array"},
                    "webhook_url": {"type": "string"},
                },
            },
            "Job": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": ["queued", "generating", "upscaling", "ready", "failed"]},
                    "kind": {"type": "string"},
                    "prompt": {"type": "string"},
                    "model": {"type": "string"},
                    "mode": {"type": "string"},
                    "duration_s": {"type": "number"},
                    "aspect": {"type": "string"},
                    "quote_credits": {"type": "integer"},
                    "video_url": {"type": "string"},
                    "poster_url": {"type": "string"},
                    "error": {"type": "string"},
                    "failure": {"type": "object"},
                    "draft": {"type": "boolean"},
                    "finish_by": {"type": "string"},
                    "created_at": {"type": "integer"},
                },
            },
        },
    },
    "paths": {
        "/models": {"get": {"operationId": "listModels", "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}}}},
        "/quote": {
            "post": {
                "operationId": "quote",
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/JobBody"}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {"credits": {"type": "integer"}, "usd": {"type": "number"}, "per_video_credits": {"type": "integer"}}}}}}},
            }
        },
        "/jobs": {
            "post": {
                "operationId": "createJob",
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/JobBody"}}}},
                "responses": {"201": {"content": {"application/json": {"schema": {"type": "object", "properties": {"id": {"type": "string"}, "ids": {"type": "array"}, "credits": {"type": "integer"}}}}}}},
            },
            "get": {"operationId": "listJobs", "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {"jobs": {"type": "array", "items": {"$ref": "#/components/schemas/Job"}}}}}}}}},
        },
        "/jobs/{id}": {
            "get": {"operationId": "getJob", "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {"job": {"$ref": "#/components/schemas/Job"}}}}}}}}
        },
        "/jobs/{id}/finish": {
            "post": {"operationId": "finishDraft", "responses": {"201": {"content": {"application/json": {"schema": {"type": "object"}}}}}}
        },
        "/uploads": {
            "post": {
                "operationId": "createUpload",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"content_type": {"type": "string"}, "bytes": {"type": "integer"}}}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {"key": {"type": "string"}, "url": {"type": "string"}, "method": {"type": "string"}, "max_bytes": {"type": "integer"}}}}}}},
            }
        },
        "/account": {"get": {"operationId": "getAccount", "responses": {"200": {"content": {"application/json": {"schema": {"type": "object"}}}}}}},
    },
}

rederived = rprofile.introspect(REMERGED_SPEC, name="remerged")
builtin = rprofile.BUILTIN_REMERGED
check("rederived endpoints match the built-in", rederived["endpoints"] == builtin["endpoints"], f"{rederived['endpoints']} != {builtin['endpoints']}")
check("rederived request fields match", rederived["job_fields"] == builtin["job_fields"], str(rederived["job_fields"]))
check("rederived response fields match", rederived["job_fields_out"] == builtin["job_fields_out"], str(rederived["job_fields_out"]))
check("rederived envelope matches", rederived["responses"]["status"] == builtin["responses"]["status"], str(rederived["responses"].get("status")))
check("rederived auth matches", rederived["auth"] == builtin["auth"], str(rederived["auth"]))
check("rederived base url matches", rederived["base_url"] == builtin["base_url"], rederived["base_url"])
check("no warnings on the real spec", not rederived["warnings"], str(rederived["warnings"]))
check(
    "status values derived from the enum",
    rederived["status_values"].get("ready") == ["ready"],
    str(rederived["status_values"]),
)

# --------------------------------------------------------------------------- #
print("\n=== 19. provider setup node ===")
provider_node = NODES["RemergedProvider"]()
name, ep_json, warnings, summary = provider_node.setup(
    "genapi",
    f"http://127.0.0.1:{PORT}/genapi",
    f"http://127.0.0.1:{PORT}/genapi/openapi.json",
    True,
    True,
    label="Generic Video API",
    api_key="rm_live_gen",
    test_connection=True,
)
check("provider saved under the right name", name == "genapi", name)
check("provider file written", os.path.isfile(rprofile.profile_path("genapi")), rprofile.profile_path("genapi"))
check("provider appears in the list", "genapi" in rprofile.profile_names(), str(rprofile.profile_names()))
check("connection test reported", "models endpoint answered" in warnings, warnings)
saved_profile = rprofile.load_profile("genapi")
check("saved profile has the key", saved_profile.get("api_key") == "rm_live_gen", str(saved_profile.get("api_key")))
check("saved profile keeps endpoints", saved_profile["endpoints"]["create"]["path"] == "/v1/renders")

print("\n=== 20. generic provider end to end ===")
gen2 = NODES["RemergedVideoGenerate"]()
STATE["gen_jobs"] = {}
video, video_path, job_ids, charged, status, info = gen2.generate(
    **base_kwargs(provider="genapi", prompt="a paper boat", model="model-x", mode="fast", duration_s=8)
)
gbody = STATE["gen_job_body"]
check("prompt sent as 'text'", gbody.get("text") == "a paper boat", str(gbody))
check("model sent as 'model_name'", gbody.get("model_name") == "model-x", str(gbody))
check("duration sent as 'duration_seconds'", gbody.get("duration_seconds") == 8, str(gbody))
check("aspect sent as 'aspect_ratio'", gbody.get("aspect_ratio") == "16:9", str(gbody))
check("audio sent as 'with_audio'", gbody.get("with_audio") is True, str(gbody))
check("mode was not mapped so not sent", "mode" not in gbody, str(gbody))
check("no Remerged field names leaked", "prompt" not in gbody and "durationS" not in gbody, str(gbody))
check("job id extracted from request_id", json.loads(job_ids) == ["req_1"], job_ids)
check("credits extracted from cost_credits", charged == 42, str(charged))
check("status normalised to ready", status == "ready", status)
check("video downloaded", os.path.isfile(video_path), video_path)
check("video bytes correct", open(video_path, "rb").read() == FAKE_MP4)

print("\n=== 21. generic provider: seed and variations ===")
STATE["gen_jobs"] = {}
gen2.generate(**base_kwargs(provider="genapi", use_seed=True, seed=77))
check("seed sent as 'random_seed'", STATE["gen_job_body"].get("random_seed") == 77, str(STATE["gen_job_body"]))

STATE["gen_jobs"] = {}
gen2.generate(**base_kwargs(provider="genapi", variations=3))
check("variations sent as 'batch_count'", STATE["gen_job_body"].get("batch_count") == 3, str(STATE["gen_job_body"]))

print("\n=== 22. generic provider: quote / account / models / list ===")
credits, usd, per_video, raw = quote_node.quote(
    "a paper boat", "model-x", 8, "16:9", "default", True, "genapi", ""
)
check("generic quote credits", credits == 42, str(credits))
check("generic quote usd", usd == 0.42, str(usd))

credits, plan, topup, in_flight, raw = acct.account("genapi", "")
check("generic account credits", credits == 99, str(credits))
check("generic account plan", plan == "gold", plan)

ids, modes, details, raw = models_node.models(True, "genapi", "")
check("generic models listed", "model-x" in ids, ids)

job_ids, jobs, count = list_node.list_jobs(20, "genapi", "")
check("generic list jobs", count >= 1, str(count))

print("\n=== 23. generic provider: unknown model is not blocked ===")
STATE["gen_jobs"] = {}
try:
    gen2.generate(**base_kwargs(provider="genapi", model="some-unlisted-model"))
    check("generic provider accepts an unlisted model", True)
except Exception as exc:
    check("generic provider accepts an unlisted model", False, str(exc))

# --------------------------------------------------------------------------- #
print("\n=== 24. base_url override on the node ===")
STATE["gen_jobs"] = {}
gen2.generate(**base_kwargs(provider="genapi", base_url=f"http://127.0.0.1:{PORT}/genapi"))
check("base_url override works", STATE["gen_job_body"].get("text") is not None)

# --------------------------------------------------------------------------- #
server.shutdown()
shutil.rmtree(_SANDBOX, ignore_errors=True)

print("\n" + "=" * 62)
print(f"PASSED {len(PASSED)}   FAILED {len(FAILED)}")
if FAILED:
    print("\nFailures:")
    for item in FAILED:
        print("  -", item)
    sys.exit(1)
print("all good")
