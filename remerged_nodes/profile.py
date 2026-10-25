"""
Provider profiles: how to talk to an OpenAPI 3.1 video service.

A *profile* is a small JSON document that says how one service maps onto the
canonical vocabulary the nodes speak:

    request   prompt, model, duration, aspect, mode, audio, seed, variations,
              camera_fixed, negative_prompt, prompt_expansion, draft, images,
              image_urls, webhook_url
    job       id, status, video, poster, error, credits
    create    id, ids, credits
    quote     credits, usd, per_video_credits
    upload    key, url

Remerged ships as a built-in profile.  Any other OpenAPI 3.1 service can be
described the same way, either by letting :func:`introspect` read its
``openapi.json`` or by writing the profile by hand.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

#: Canonical request fields and the names a service might use for them.
FIELD_SYNONYMS: Dict[str, List[str]] = {
    "prompt": ["prompt", "text", "description", "input_text", "prompt_text", "input"],
    "model": ["model", "model_id", "model_name", "model_slug", "engine"],
    "duration": ["duration", "duration_s", "durations", "duration_seconds", "length", "length_s", "seconds", "num_seconds"],
    "aspect": ["aspect", "aspect_ratio", "ratio", "orientation"],
    "mode": ["mode", "quality", "resolution", "rendition", "render_mode", "quality_mode"],
    "audio": ["audio", "sound", "with_audio", "generate_audio", "audio_enabled"],
    "seed": ["seed", "random_seed"],
    "variations": ["variations", "num_variations", "n", "count", "num_outputs", "batch_size", "batch_count", "num_samples", "samples"],
    "camera_fixed": ["camera_fixed", "camerafixed", "lock_camera", "static_camera", "fixed_camera"],
    "negative_prompt": ["negative_prompt", "negativeprompt", "negative", "avoid"],
    "prompt_expansion": ["prompt_expansion", "promptexpansion", "expand_prompt", "rewrite_prompt"],
    "draft": ["draft", "preview", "low_res", "test_render"],
    "images": ["images", "image", "input_images", "image_inputs", "inputs", "input_image", "image_input"],
    "image_urls": ["image_urls", "imageurls", "urls", "image_url", "remote_images"],
    "webhook_url": ["webhook_url", "webhookurl", "webhook", "callback_url", "callbackurl", "callback"],
}

#: Canonical job fields and the names a service might use for them.
JOB_SYNONYMS: Dict[str, List[str]] = {
    "id": ["id", "job_id", "jobid", "uuid", "identifier", "request_id", "task_id", "generation_id"],
    "status": ["status", "state", "job_status", "processing_status", "job_state"],
    "video": ["video_url", "videourl", "clip_url", "render_url", "output_url", "result_url", "download_url", "video", "url"],
    "poster": ["poster_url", "posterurl", "thumbnail_url", "thumbnailurl", "preview_url", "still_url", "image_url", "imageurl"],
    "error": ["error", "error_message", "errormessage", "failure", "failure_reason", "message"],
    "credits": ["quote_credits", "credits", "credit_cost", "cost", "price", "amount", "cost_credits"],
    "mode": ["mode", "quality", "resolution"],
    "duration": ["duration_s", "duration", "duration_seconds", "length"],
    "aspect": ["aspect", "aspect_ratio", "ratio"],
    "prompt": ["prompt", "text"],
    "model": ["model", "model_id"],
}

#: Canonical create-response fields.
CREATE_SYNONYMS: Dict[str, List[str]] = {
    "id": ["id", "job_id", "jobid", "uuid", "request_id", "task_id", "generation_id"],
    "ids": ["ids", "job_ids", "jobids", "request_ids", "generation_ids"],
    "credits": ["credits", "quote_credits", "cost", "price", "amount", "cost_credits"],
}

#: Canonical quote-response fields.
QUOTE_SYNONYMS: Dict[str, List[str]] = {
    "credits": ["credits", "cost", "amount", "total"],
    "usd": ["usd", "price", "cost_usd", "usd_cost", "amount_usd", "total_usd", "dollars"],
    "per_video_credits": ["per_video_credits", "pervideocredits", "unit_credits", "credits_per_video", "each"],
}

#: Canonical upload-response fields.
UPLOAD_SYNONYMS: Dict[str, List[str]] = {
    "key": ["key", "upload_key", "id", "upload_id", "file_id", "path", "name"],
    "url": ["url", "upload_url", "uploadurl", "href", "signed_url", "presigned_url", "put_url", "upload_url_href"],
}

#: Canonical upload-request fields.
UPLOAD_REQUEST_SYNONYMS: Dict[str, List[str]] = {
    "content_type": ["content_type", "contenttype", "mime_type", "mimetype", "type"],
    "bytes": ["bytes", "size", "size_bytes", "byte_size", "content_length", "length"],
}

#: Job status buckets. Order matters for substring matching.
STATUS_SYNONYMS: Dict[str, List[str]] = {
    "queued": ["queued", "pending", "waiting", "accepted", "submitted", "created", "scheduled"],
    "generating": ["generating", "running", "processing", "in_progress", "inprogress", "started", "working", "rendering"],
    "upscaling": ["upscaling", "upscale", "scaling", "postprocess", "post_processing", "enhancing"],
    "failed": ["failed", "failure", "error", "errored", "cancelled", "canceled", "rejected", "aborted"],
    "ready": ["ready", "complete", "completed", "succeeded", "success", "done", "finished", "available", "succeed"],
}

#: Every model the Remerged API currently sells, straight from its OpenAPI enum.
MODEL_IDS: List[str] = [
    "seedance-2.5",
    "seedance-2.0",
    "seedance-2.0-fast",
    "seedance-2.0-mini",
    "minimax-h3",
    "ltx-2.5",
    "wan-2.6",
]

#: The Remerged model guide, from https://remerged.ai/help/creating/choosing-a-model
#: and https://remerged.ai/help/creating/quality-and-upscale
REMERGED_MODEL_GUIDE: Dict[str, Dict[str, Any]] = {
    "seedance-2.5": {
        "label": "Seedance 2.5",
        "min_duration": 4,
        "max_duration": 30,
        "renditions": ["480p", "720p", "1080p"],
        "upscales": ["1080p", "4k"],
        "seedance": True,
        "single_image": False,
        "on_free": False,
        "notes": "Newest and the default. Up to 30s, dialogue in 11 languages, first and last frames, reference video and audio, characters.",
    },
    "seedance-2.0": {
        "label": "Seedance 2.0",
        "min_duration": 4,
        "max_duration": 15,
        "renditions": ["480p", "720p", "1080p"],
        "upscales": ["1080p", "4k"],
        "seedance": True,
        "single_image": False,
        "on_free": False,
        "notes": "Highest quality of the 2.0 family. Short, plain prompts and numbered shots work best.",
    },
    "seedance-2.0-fast": {
        "label": "Seedance 2.0 Fast",
        "min_duration": 4,
        "max_duration": 15,
        "renditions": ["480p", "720p"],
        "upscales": ["1080p", "4k"],
        "seedance": True,
        "single_image": False,
        "on_free": False,
        "notes": "Same features as Seedance 2.0, cheaper and quicker.",
    },
    "seedance-2.0-mini": {
        "label": "Seedance 2.0 Mini",
        "min_duration": 4,
        "max_duration": 15,
        "renditions": ["480p", "720p"],
        "upscales": ["1080p", "4k"],
        "seedance": True,
        "single_image": False,
        "on_free": True,
        "free_max_duration": 4,
        "free_renditions": ["480p"],
        "notes": "Best value. On the Free plan this is the only Seedance available (480p, up to 4s).",
    },
    "minimax-h3": {
        "label": "MiniMax H3",
        "min_duration": 4,
        "max_duration": 10,
        "renditions": ["720p"],
        "upscales": ["1080p", "4k"],
        "seedance": False,
        "single_image": True,
        "on_free": True,
        "notes": "Runs on Remerged's own GPU. Takes a prompt and at most one starting image.",
    },
    "ltx-2.5": {
        "label": "LTX-2.5",
        "min_duration": 4,
        "max_duration": 10,
        "renditions": ["720p"],
        "upscales": ["1080p", "4k"],
        "seedance": False,
        "single_image": True,
        "on_free": True,
        "notes": "Runs on Remerged's own GPU. Takes a prompt and at most one starting image.",
    },
    "wan-2.6": {
        "label": "Wan 2.6",
        "min_duration": 5,
        "max_duration": 15,
        "duration_steps": [5, 10, 15],
        "renditions": ["720p", "1080p"],
        "upscales": ["1080p", "4k"],
        "seedance": False,
        "single_image": True,
        "wan": True,
        "on_free": False,
        "notes": "Renders 5, 10 or 15s only (a length in between is rounded up). Expands the prompt itself, and takes negativePrompt.",
    },
}

#: The Remerged profile, hand-checked against https://remerged.ai/openapi.json
BUILTIN_REMERGED: Dict[str, Any] = {
    "name": "remerged",
    "label": "Remerged",
    "builtin": True,
    "base_url": "https://remerged.ai/api/v1",
    "openapi_url": "https://remerged.ai/openapi.json",
    "auth": {"type": "bearer", "header": "Authorization", "prefix": "Bearer "},
    "api_key_env": "REMERGED_API_KEY",
    "models": REMERGED_MODEL_GUIDE,
    "endpoints": {
        "models": {"method": "GET", "path": "/models"},
        "quote": {"method": "POST", "path": "/quote"},
        "create": {"method": "POST", "path": "/jobs"},
        "status": {"method": "GET", "path": "/jobs/{id}"},
        "list": {"method": "GET", "path": "/jobs"},
        "finish": {"method": "POST", "path": "/jobs/{id}/finish"},
        "upload": {"method": "POST", "path": "/uploads"},
        "account": {"method": "GET", "path": "/account"},
    },
    "responses": {
        "create": {"id": "id", "ids": "ids", "credits": "credits"},
        "status": {"envelope": "job"},
        "quote": {"credits": "credits", "usd": "usd", "per_video_credits": "per_video_credits"},
        "upload": {"key": "key", "url": "url"},
    },
    "job_fields": {
        "prompt": "prompt",
        "model": "model",
        "duration": "durationS",
        "aspect": "aspect",
        "mode": "mode",
        "audio": "audio",
        "seed": "seed",
        "variations": "variations",
        "camera_fixed": "cameraFixed",
        "negative_prompt": "negativePrompt",
        "prompt_expansion": "promptExpansion",
        "draft": "draft",
        "images": "images",
        "image_urls": "image_urls",
        "webhook_url": "webhook_url",
    },
    "job_fields_out": {
        "id": "id",
        "status": "status",
        "video": "video_url",
        "poster": "poster_url",
        "error": "error",
        "credits": "quote_credits",
        "mode": "mode",
        "duration": "duration_s",
        "aspect": "aspect",
        "prompt": "prompt",
        "model": "model",
    },
    "upload_fields": {"content_type": "content_type", "bytes": "bytes"},
    "status_values": {
        "queued": ["queued"],
        "generating": ["generating", "upscaling"],
        "upscaling": ["upscaling"],
        "ready": ["ready"],
        "failed": ["failed"],
    },
}


# ---------------------------------------------------------------------- #
# small helpers
# ---------------------------------------------------------------------- #
def norm(name: str) -> str:
    """Normalise a field name so camelCase and snake_case compare equal."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def dig(payload: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path out of a nested object."""
    if not path:
        return payload
    current = payload
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def classify_status(value: Any) -> str:
    """Map a service's status string onto one of our buckets."""
    if value is None:
        return ""
    text = norm(value)
    if not text:
        return ""
    for canonical, names in STATUS_SYNONYMS.items():
        if text in names:
            return canonical
    for canonical in ("failed", "generating", "queued", "upscaling", "ready"):
        for name in STATUS_SYNONYMS[canonical]:
            if name in text:
                return canonical
    return text


def _scan_for(node: Any, target: str, level: int) -> Any:
    """Find an exact normalised key name in a nested object."""
    if not isinstance(node, dict):
        return None
    for key, value in node.items():
        if norm(key) == target:
            return value
    if level <= 0:
        return None
    for value in node.values():
        if isinstance(value, dict):
            found = _scan_for(value, target, level - 1)
            if found is not None:
                return found
    return None


def search_key(payload: Any, names: List[str], depth: int = 2) -> Any:
    """Find a value by any of several possible field names.

    The names are tried in order, so a more specific name always wins over a
    more general one: looking for ``video_url`` finds it before ``url`` would
    have matched a poster.
    """
    for name in names or []:
        found = _scan_for(payload, norm(name), depth)
        if found is not None:
            return found
    return None


def _normalise_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    model_id = None
    for key in ("id", "model", "slug", "model_id", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            model_id = value.strip()
            break
    if not model_id:
        return None

    modes = None
    for key in ("modes", "resolutions", "qualities", "renditions", "outputs"):
        value = entry.get(key)
        if isinstance(value, list) and value:
            modes = [str(item) for item in value]
            break

    durations = None
    for key in ("durations", "duration_range", "lengths"):
        value = entry.get(key)
        if isinstance(value, list) and value:
            durations = value
            break

    return {"id": model_id, "modes": modes, "durations": durations, "raw": entry}


def normalise_catalogue(payload: Any) -> Dict[str, Any]:
    """Turn whatever ``GET /models`` returns into ``{model_id: {...}}``.

    The shape of a models endpoint is not standardised, so this accepts a list
    of objects, an object with a ``models`` array, or an object keyed by id.
    """
    entries: List[Dict[str, Any]] = []

    if isinstance(payload, dict):
        found = False
        for key in ("models", "data", "items", "catalogue", "catalog", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                entries = [item for item in value if isinstance(item, dict)]
                found = True
                break
        if not found and payload and all(isinstance(v, dict) for v in payload.values()):
            entries = [
                {"id": key, **value} for key, value in payload.items() if isinstance(value, dict)
            ]
    elif isinstance(payload, list):
        entries = [item for item in payload if isinstance(item, dict)]

    normalised: Dict[str, Any] = {}
    for entry in entries:
        info = _normalise_entry(entry)
        if info:
            normalised[info["id"]] = info
    return normalised


# ---------------------------------------------------------------------- #
# profile helpers
# ---------------------------------------------------------------------- #
def provider_dir() -> str:
    """Where saved provider profiles live."""
    from . import config

    path = os.path.join(config.user_dir(), "providers")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def profile_path(name: str) -> str:
    return os.path.join(provider_dir(), f"{safe_name(name)}.json")


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip()).strip("-.")
    return cleaned.lower() or "provider"


def load_profile(name: str) -> Optional[Dict[str, Any]]:
    """Load a profile by name, falling back to the built-ins."""
    key = safe_name(name)
    if key == "remerged":
        return json.loads(json.dumps(BUILTIN_REMERGED))
    path = profile_path(key)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("name", key)
            data.setdefault("label", data.get("name"))
            return data
    except Exception:
        return None
    return None


def save_profile(profile: Dict[str, Any]) -> str:
    """Write a profile to disk and return its path."""
    name = safe_name(profile.get("name") or "provider")
    profile = dict(profile)
    profile["name"] = name
    profile["builtin"] = False
    path = profile_path(name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def list_profiles() -> List[Dict[str, Any]]:
    """Every known profile: the built-ins first, then the saved ones."""
    profiles: List[Dict[str, Any]] = [json.loads(json.dumps(BUILTIN_REMERGED))]
    seen = {"remerged"}
    try:
        entries = sorted(os.listdir(provider_dir()))
    except Exception:
        entries = []
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        name = entry[:-5]
        if name in seen:
            continue
        loaded = load_profile(name)
        if loaded:
            seen.add(name)
            profiles.append(loaded)
    return profiles


def profile_names() -> List[str]:
    return [profile.get("name", "provider") for profile in list_profiles()]


def profile_label(name: str) -> str:
    for profile in list_profiles():
        if profile.get("name") == safe_name(name):
            return str(profile.get("label") or profile.get("name"))
    return str(name)


# ---------------------------------------------------------------------- #
# OpenAPI introspection
# ---------------------------------------------------------------------- #
def _resolve(spec: Dict[str, Any], schema: Any) -> Dict[str, Any]:
    """Follow ``$ref`` until we reach a real schema object."""
    depth = 0
    while isinstance(schema, dict) and "$ref" in schema and depth < 12:
        name = str(schema["$ref"]).split("/")[-1]
        schema = (((spec.get("components") or {}).get("schemas") or {}).get(name) or {})
        depth += 1
    return schema if isinstance(schema, dict) else {}


def _json_schema(spec: Dict[str, Any], operation: Dict[str, Any], where: str) -> Dict[str, Any]:
    """The schema of an operation's request body or response."""
    container = operation.get("responses") if where == "response" else operation.get("requestBody")
    if not isinstance(container, dict):
        return {}
    if where == "response":
        entry = container.get("200") or container.get("201") or container.get("default") or {}
    else:
        entry = container
    if not isinstance(entry, dict):
        return {}
    content = entry.get("content") or {}
    for media_type in ("application/json", "text/json", "application/vnd.api+json"):
        if media_type in content:
            return _resolve(spec, (content[media_type] or {}).get("schema") or {})
    if content:
        first = next(iter(content.values()))
        return _resolve(spec, (first or {}).get("schema") or {})
    return {}


def _properties(spec: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return properties
    return {}


def _operations(spec: Dict[str, Any]):
    """Every operation in the spec as ``(path, method, operation)``."""
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if str(method).lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(operation, dict):
                continue
            yield path, str(method).lower(), operation


def _path_words(path: str) -> str:
    return norm(path)


def _has_placeholder(path: str) -> bool:
    return "{" in path and "}" in path


def _placeholder(path: str) -> str:
    match = re.search(r"\{([^}]+)\}", path)
    return match.group(1) if match else "id"


def _find_field_path(
    spec: Dict[str, Any],
    schema: Dict[str, Any],
    wanted: List[str],
    max_depth: int = 2,
) -> str:
    """Find the dotted path to a property named one of ``wanted``.

    Looks at the schema itself, then one and two levels down, so both
    ``{"id": ...}`` and ``{"data": {"id": ...}}`` are found.
    """
    wanted_norm = {norm(name) for name in wanted}
    properties = _properties(spec, schema)
    for key in properties:
        if norm(key) in wanted_norm:
            return key
    if max_depth <= 1:
        return ""
    for key, sub_schema in properties.items():
        resolved = _resolve(spec, sub_schema)
        sub_props = _properties(spec, resolved)
        for sub_key in sub_props:
            if norm(sub_key) in wanted_norm:
                return f"{key}.{sub_key}"
        if max_depth <= 2:
            for sub_key, deep_schema in sub_props.items():
                deep = _resolve(spec, deep_schema)
                for deep_key in _properties(spec, deep):
                    if norm(deep_key) in wanted_norm:
                        return f"{key}.{sub_key}.{deep_key}"
    return ""


def _match_mapping(properties: Dict[str, Any], synonyms: Dict[str, List[str]]) -> Dict[str, str]:
    """Map canonical field names onto the properties a service actually has."""
    lookup: Dict[str, str] = {}
    for canonical, names in synonyms.items():
        for name in names:
            target = norm(name)
            for key in properties:
                if norm(key) == target:
                    lookup[canonical] = key
                    break
            if canonical in lookup:
                break
    return lookup


def _introspect_auth(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Work out how to authenticate, from the security schemes."""
    schemes = ((spec.get("components") or {}).get("securitySchemes") or {})
    for scheme in schemes.values():
        if not isinstance(scheme, dict):
            continue
        kind = str(scheme.get("type") or "").lower()
        if kind == "http":
            scheme_name = str(scheme.get("scheme") or "").lower()
            if scheme_name == "bearer":
                return {"type": "bearer", "header": "Authorization", "prefix": "Bearer "}
            return {"type": "http", "header": "Authorization", "prefix": ""}
        if kind == "apikey":
            header = str(scheme.get("in") or "header").lower()
            name = str(scheme.get("name") or "X-API-Key")
            if header == "header":
                return {"type": "api_key", "header": name, "prefix": ""}
            return {"type": "query", "header": "", "prefix": "", "query_param": name}
        if kind == "oauth2":
            return {"type": "bearer", "header": "Authorization", "prefix": "Bearer "}
    return {"type": "bearer", "header": "Authorization", "prefix": "Bearer "}


def _score_endpoint(
    path: str,
    method: str,
    operation: Dict[str, Any],
    spec: Dict[str, Any],
    want_method: str,
    path_words: List[str],
    op_words: List[str],
) -> float:
    if method != want_method:
        return -1.0
    score = 0.0
    words = _path_words(path)
    for word in path_words:
        if norm(word) in words:
            score += 2.0
    operation_id = norm(operation.get("operationId"))
    for word in op_words:
        if norm(word) in operation_id:
            score += 3.0
    return score


def introspect(
    spec: Dict[str, Any],
    base_url: str = "",
    openapi_url: str = "",
    name: str = "",
    label: str = "",
) -> Dict[str, Any]:
    """Derive a profile from an OpenAPI 3.1 document.

    Anything that cannot be found is left out, and the caller is told what was
    missing so it can be filled in by hand.
    """
    spec = spec if isinstance(spec, dict) else {}
    warnings: List[str] = []

    profile: Dict[str, Any] = {
        "name": safe_name(name or "provider"),
        "label": label or name or "Provider",
        "builtin": False,
        "base_url": (base_url or "").rstrip("/"),
        "openapi_url": openapi_url,
        "auth": _introspect_auth(spec),
        "api_key_env": "",
        "endpoints": {},
        "responses": {},
        "job_fields": {},
        "job_fields_out": {},
        "upload_fields": {},
        "status_values": {},
        "warnings": warnings,
    }

    if not profile["base_url"]:
        servers = spec.get("servers") or []
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            profile["base_url"] = str(servers[0].get("url") or "").rstrip("/")
    if not profile["base_url"]:
        warnings.append("No base_url in the spec and none given; set one by hand.")

    operations = list(_operations(spec))

    # ---------------------------------------------------------- create
    create_best: Tuple[float, str, str, Dict[str, Any]] = (-1.0, "", "", {})
    for path, method, operation in operations:
        if method != "post":
            continue
        score = _score_endpoint(
            path,
            method,
            operation,
            spec,
            "post",
            ["job", "generate", "generation", "render", "video", "create", "task", "submit"],
            ["create", "generate", "submit", "render", "make", "start"],
        )
        body_schema = _json_schema(spec, operation, "request")
        body_props = _properties(spec, body_schema)
        if body_props:
            score += 3.0
        if any(norm(key) in {norm(n) for n in ("prompt", "text", "description")} for key in body_props):
            score += 3.0
        response_schema = _json_schema(spec, operation, "response")
        response_props = _properties(spec, response_schema)
        if response_props:
            score += 1.0
        if score > create_best[0]:
            create_best = (score, method, path, operation)

    create_path = ""
    create_operation: Dict[str, Any] = {}
    if create_best[0] > 0:
        _, create_path, create_operation = create_best[1], create_best[2], create_best[3]
        profile["endpoints"]["create"] = {"method": "POST", "path": create_path}
    else:
        warnings.append("Could not find a 'create job' endpoint - set endpoints.create by hand.")

    create_body_schema = _json_schema(spec, create_operation, "request") if create_operation else {}
    create_body_props = _properties(spec, create_body_schema)
    if create_body_props:
        mapped = _match_mapping(create_body_props, FIELD_SYNONYMS)
        profile["job_fields"] = mapped
        for canonical in FIELD_SYNONYMS:
            if canonical not in mapped:
                warnings.append(f"No request field for '{canonical}' - it will not be sent.")

    # ---------------------------------------------------------- status
    status_best: Tuple[float, str, Dict[str, Any]] = (-1.0, "", {})
    for path, method, operation in operations:
        if method != "get" or not _has_placeholder(path):
            continue
        score = 1.0
        if create_path and _path_words(path).startswith(_path_words(create_path)):
            score += 4.0
        if "job" in _path_words(path):
            score += 1.0
        response_schema = _json_schema(spec, operation, "response")
        if _find_field_path(spec, response_schema, JOB_SYNONYMS["status"]):
            score += 2.0
        if score > status_best[0]:
            status_best = (score, path, operation)

    if status_best[0] > 0:
        status_path = status_best[1]
        profile["endpoints"]["status"] = {"method": "GET", "path": status_path}
        status_schema = _json_schema(spec, status_best[2], "response")
        envelope = _find_field_path(spec, status_schema, JOB_SYNONYMS["status"])
        if "." in envelope:
            envelope = envelope.rsplit(".", 1)[0]
        profile["responses"]["status"] = {"envelope": envelope}

        job_props: Dict[str, Any] = {}
        if envelope:
            job_props = _properties(spec, _resolve(spec, _dig_schema(spec, status_schema, envelope)))
        else:
            job_props = _properties(spec, status_schema)
        if job_props:
            profile["job_fields_out"] = _match_mapping(job_props, JOB_SYNONYMS)
    else:
        warnings.append("Could not find a 'job status' endpoint - set endpoints.status by hand.")

    # ---------------------------------------------------------- the rest
    simple_targets = {
        "models": ("get", ["model", "catalogue", "catalog"], ["listmodels", "models", "getmodels"]),
        "list": ("get", ["job", "task", "history"], ["listjobs", "listtasks", "getjobs"]),
        "account": (
            "get",
            ["account", "balance", "user", "credit", "wallet", "billing", "subscription"],
            ["getaccount", "account", "balance", "wallet", "billing"],
        ),
        "quote": ("post", ["quote", "price", "estimate", "cost"], ["quote", "price", "estimate", "cost"]),
        "finish": ("post", ["finish", "finalize", "complete", "upscale"], ["finish", "finalize", "complete"]),
        "upload": ("post", ["upload", "file", "asset", "media"], ["createupload", "upload", "presign"]),
    }

    for target, (method, path_words, op_words) in simple_targets.items():
        best: Tuple[float, str, Dict[str, Any]] = (-1.0, "", {})
        for path, op_method, operation in operations:
            score = _score_endpoint(path, op_method, operation, spec, method, path_words, op_words)
            if target == "finish" and not _has_placeholder(path):
                score -= 3.0
            if target == "list" and create_path and path == create_path:
                score += 3.0
            if score > best[0]:
                best = (score, path, operation)
        # At least one word has to match, so a zero-scoring path is never taken.
        if best[0] >= 2.0 and best[1]:
            profile["endpoints"][target] = {"method": method.upper(), "path": best[1]}
        else:
            warnings.append(f"Could not find a '{target}' endpoint - set endpoints.{target} by hand.")

    # ---------------------------------------------------------- responses
    create_response_schema = _json_schema(spec, create_operation, "response") if create_operation else {}
    if create_response_schema:
        create_map: Dict[str, str] = {}
        for canonical, names in CREATE_SYNONYMS.items():
            path_found = _find_field_path(spec, create_response_schema, names)
            if path_found:
                create_map[canonical] = path_found
        if create_map:
            profile["responses"]["create"] = create_map

    quote_endpoint = profile["endpoints"].get("quote")
    if quote_endpoint:
        quote_operation = _operation_for(spec, quote_endpoint["path"], quote_endpoint["method"].lower())
        if quote_operation:
            quote_schema = _json_schema(spec, quote_operation, "response")
            quote_map: Dict[str, str] = {}
            for canonical, names in QUOTE_SYNONYMS.items():
                path_found = _find_field_path(spec, quote_schema, names)
                if path_found:
                    quote_map[canonical] = path_found
            if quote_map:
                profile["responses"]["quote"] = quote_map

    upload_endpoint = profile["endpoints"].get("upload")
    if upload_endpoint:
        upload_operation = _operation_for(spec, upload_endpoint["path"], upload_endpoint["method"].lower())
        if upload_operation:
            upload_schema = _json_schema(spec, upload_operation, "response")
            upload_map: Dict[str, str] = {}
            for canonical, names in UPLOAD_SYNONYMS.items():
                path_found = _find_field_path(spec, upload_schema, names)
                if path_found:
                    upload_map[canonical] = path_found
            if upload_map:
                profile["responses"]["upload"] = upload_map

            upload_request_schema = _json_schema(spec, upload_operation, "request")
            upload_props = _properties(spec, upload_request_schema)
            if upload_props:
                profile["upload_fields"] = _match_mapping(upload_props, UPLOAD_REQUEST_SYNONYMS)

    # ---------------------------------------------------------- status values
    profile["status_values"] = _status_values_from_spec(spec, profile)

    return profile


def _dig_schema(spec: Dict[str, Any], schema: Dict[str, Any], path: str) -> Dict[str, Any]:
    """Walk a dotted path through a schema's properties."""
    current = schema
    for part in str(path or "").split("."):
        if not part:
            continue
        properties = _properties(spec, current)
        if part not in properties:
            return {}
        current = _resolve(spec, properties[part])
    return current


def _operation_for(spec: Dict[str, Any], path: str, method: str) -> Dict[str, Any]:
    item = (spec.get("paths") or {}).get(path) or {}
    operation = item.get(method) or item.get(method.upper()) or {}
    return operation if isinstance(operation, dict) else {}


def _status_values_from_spec(spec: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, List[str]]:
    """Work out which of the service's status strings mean what to us."""
    endpoint = profile.get("endpoints", {}).get("status")
    if not endpoint:
        return {}

    operation = _operation_for(spec, endpoint["path"], str(endpoint["method"]).lower())
    schema = _json_schema(spec, operation, "response")
    envelope = ((profile.get("responses") or {}).get("status") or {}).get("envelope") or ""
    job_schema = _dig_schema(spec, schema, envelope) if envelope else schema

    properties = _properties(spec, job_schema)
    status_key = (profile.get("job_fields_out") or {}).get("status")
    if not status_key or status_key not in properties:
        return {}

    status_schema = _resolve(spec, properties[status_key])
    enum = status_schema.get("enum")
    if not isinstance(enum, list) or not enum:
        return {}

    buckets: Dict[str, List[str]] = {}
    for value in enum:
        canonical = classify_status(value)
        if canonical in STATUS_SYNONYMS:
            buckets.setdefault(canonical, []).append(str(value))
    return buckets


def profile_status_map(profile: Dict[str, Any]) -> Dict[str, str]:
    """A lookup from the service's own status strings to our buckets."""
    mapping: Dict[str, str] = {}
    for canonical, values in (profile.get("status_values") or {}).items():
        for value in values or []:
            mapping[str(value).lower()] = canonical
    return mapping


def canonical_status(profile: Dict[str, Any], value: Any) -> str:
    """Normalise a status using the profile first, then the generic rules."""
    mapping = profile_status_map(profile)
    if value is None:
        return ""
    text = str(value)
    if text.lower() in mapping:
        return mapping[text.lower()]
    return classify_status(text)
