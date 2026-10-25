"""
The custom nodes for ComfyUI.

By default these talk to [Remerged](https://remerged.ai), which renders AI
video.  But nothing here is Remerged specific: a *provider profile* says where
the endpoints are and what the request and response fields are called, so the
same nodes can drive any service with a compatible OpenAPI 3.1 description.

The Remerged profile covers (https://remerged.ai/developers):

    GET  /models              what every model takes, and your price
    POST /quote               what a job would cost, without making it
    POST /jobs                make a video
    GET  /jobs/{id}           status, and a video URL once it is ready
    GET  /jobs                your recent jobs
    POST /jobs/{id}/finish    finish a Seedance 2.5 draft at 1080p
    POST /uploads             a presigned PUT for an input file
    GET  /account             balance, plan and limits
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import config, profile as profile_mod, utils
from .api import ApiClient, RemergedError, wait_for_job

CATEGORY = "Remerged"

#: The native ``VIDEO`` type only exists on newer ComfyUI builds.
_HAS_VIDEO = utils.video_type_available()
VIDEO_TYPE = "VIDEO" if _HAS_VIDEO else "STRING"

#: Roles the API understands for an uploaded input.
_UPLOAD_ROLES = [
    "reference",
    "first_frame",
    "last_frame",
    "reference_video",
    "reference_audio",
    "upscale_source",
    "character",
    "costume",
]

_KEY_PATTERN = re.compile(
    r"^(uploads/[^/]+/[^/]+|character:[0-9a-f-]{36}|preset:asset-\d{14}-[a-z0-9]{5})$"
)


# ---------------------------------------------------------------------- #
# shared helpers
# ---------------------------------------------------------------------- #
def _provider_widget() -> Tuple[List[str], Dict[str, Any]]:
    names = profile_mod.profile_names()
    if "remerged" not in names:
        names = ["remerged"] + names
    return names, {"default": "remerged"}


def _resolve_client(
    api_key: str,
    provider: str = "remerged",
    base_url: str = "",
) -> Tuple[ApiClient, Dict[str, Any]]:
    """Build a client for a provider, with the profile it was loaded from."""
    prof = profile_mod.load_profile(provider)
    if prof is None:
        raise ValueError(
            f"No provider called '{provider}'. Known providers: "
            f"{', '.join(profile_mod.profile_names())}."
        )

    key = config.get_api_key(api_key, provider)
    if not key:
        env_hint = prof.get("api_key_env") or "the provider's env var"
        raise ValueError(
            f"No API key for '{provider}'. Put one in the node's 'api_key' field, "
            f"set {env_hint}, or save it to "
            f"{os.path.join(config.user_dir(), 'config.json')}."
        )

    host = (base_url or "").strip() or config.get_base_url(provider=provider)
    return ApiClient(prof, key, base_url=host), prof


def _model_guide(prof: Dict[str, Any]) -> Dict[str, Any]:
    """The provider's per-model guide, empty for a generic service."""
    return prof.get("models") or {}


def _known_model_info(prof: Dict[str, Any], model: str) -> Dict[str, Any]:
    guide = _model_guide(prof)
    if not guide:
        return {}
    return dict(guide.get(model) or {})


def _core_canonical(
    prompt: str,
    model: str,
    duration: int,
    aspect: str,
    mode: str,
    audio: bool,
) -> Dict[str, Any]:
    """The part of the job body every service understands."""
    canonical: Dict[str, Any] = {
        "prompt": prompt,
        "model": model,
        "duration": int(duration),
        "aspect": aspect,
        "audio": bool(audio),
    }
    if mode and mode != config.MODE_DEFAULT:
        canonical["mode"] = mode
    return canonical


def _slug_from_prompt(prompt: str) -> str:
    return config.slugify(prompt or "video")


def _extension_from_url(url: str, fallback: str = ".mp4") -> str:
    try:
        path = url.split("?")[0].split("#")[0]
        ext = os.path.splitext(path)[1].lower()
    except Exception:
        ext = ""
    if ext and len(ext) <= 6 and ext[1:].isalnum():
        return ext
    return fallback


def _merge_extra_inputs(text: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a JSON object's fields into the request body, overriding."""
    text = (text or "").strip()
    if not text:
        return body
    try:
        extra = json.loads(text)
    except Exception as exc:
        raise ValueError(f"'extra_inputs' must be a JSON object: {exc}") from exc
    if not isinstance(extra, dict):
        raise ValueError("'extra_inputs' must be a JSON object.")
    body.update(extra)
    return body


def _parse_uploaded_inputs(text: str) -> List[Dict[str, Any]]:
    """Read the advanced 'uploaded_inputs' widget."""
    entries = utils.parse_json_list(text)
    cleaned: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each entry of 'uploaded_inputs' must be a JSON object.")
        key = entry.get("key")
        if not isinstance(key, str) or not _KEY_PATTERN.match(key):
            raise ValueError(
                f"'{key}' is not a valid upload key. Expected something like "
                "'uploads/<folder>/<file>'."
            )
        item: Dict[str, Any] = {"key": key}
        role = entry.get("role")
        if role:
            if role not in _UPLOAD_ROLES:
                raise ValueError(
                    f"'{role}' is not a known role. Valid roles: {', '.join(_UPLOAD_ROLES)}."
                )
            item["role"] = role
        for optional in ("seconds", "tail"):
            if optional in entry:
                item[optional] = entry[optional]
        cleaned.append(item)
    return cleaned


def _upload_image(client: ApiClient, image: Any, reporter: utils.ProgressReporter) -> str:
    reporter.status("Uploading a starting image...")
    payload = utils.image_to_png_bytes(image)
    return client.upload_bytes(payload, "image/png")


def _download_job_assets(
    client: ApiClient,
    job: Dict[str, Any],
    prompt_slug: str,
    reporter: utils.ProgressReporter,
) -> Dict[str, Optional[str]]:
    """Download the finished video (and poster) of a ready job."""
    job_id = str(client.job_field(job, "id") or "job")
    video_url = client.job_field(job, "video")
    poster_url = client.job_field(job, "poster")

    video_path: Optional[str] = None
    poster_path: Optional[str] = None
    out_dir = utils.remerged_output_directory()

    if video_url:
        reporter.status(f"Downloading {job_id}...")
        target = utils.unique_path(
            out_dir, f"{prompt_slug}-{job_id[:8]}", _extension_from_url(video_url)
        )
        client.download(video_url, target)
        video_path = target

    if poster_url:
        try:
            target = utils.unique_path(
                out_dir, f"{prompt_slug}-{job_id[:8]}-poster", _extension_from_url(poster_url, ".jpg")
            )
            client.download(poster_url, target)
            poster_path = target
        except Exception:
            poster_path = None

    return {"video_path": video_path, "poster_path": poster_path}


def _video_output(paths: List[str]):
    """Build the native VIDEO value, or a plain path when it is unavailable."""
    if not paths:
        return "" if not _HAS_VIDEO else None
    if not _HAS_VIDEO:
        return paths[0] if len(paths) == 1 else json.dumps(paths)
    videos = [utils.make_video(path) for path in paths]
    return videos[0] if len(videos) == 1 else videos


def _known_models(provider: str) -> List[str]:
    """Models offered by a provider, from its guide or its cached catalogue."""
    return config.model_ids(provider)


def _all_models() -> List[str]:
    """Every model any known provider offers, for the dropdown."""
    models: List[str] = []
    for profile in profile_mod.list_profiles():
        for model in config.model_ids(profile.get("name", "remerged")):
            if model not in models:
                models.append(model)
    return models or list(profile_mod.MODEL_IDS)


def _all_aspects() -> List[str]:
    """Every aspect any known provider offers."""
    aspects: List[str] = []
    for profile in profile_mod.list_profiles():
        guide = profile.get("models") or {}
        for info in guide.values():
            for aspect in info.get("aspects") or []:
                if aspect not in aspects:
                    aspects.append(aspect)
    for aspect in config.ASPECTS:
        if aspect not in aspects:
            aspects.append(aspect)
    return aspects


def _all_modes() -> List[str]:
    """Every mode any known provider offers."""
    modes: List[str] = [config.MODE_DEFAULT]
    for profile in profile_mod.list_profiles():
        name = profile.get("name", "remerged")
        for model in config.model_ids(name):
            for mode in config.modes_for_model(model, name):
                if mode not in modes:
                    modes.append(mode)
    return modes


# ---------------------------------------------------------------------- #
# 1. the main node
# ---------------------------------------------------------------------- #
class RemergedVideoGenerate:
    """Make a video and wait for it."""

    @classmethod
    def INPUT_TYPES(cls):
        providers, provider_options = _provider_widget()
        return {
            "required": {
                "prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "tooltip": "Go to the model exactly as written. Refer to pictures as Image 1, Image 2…",
                    },
                ),
                "model": (_all_models(), {"default": "seedance-2.5"}),
                "duration_s": (
                    "INT",
                    {
                        "default": 5,
                        "min": 4,
                        "max": 30,
                        "step": 1,
                        "tooltip": "4-30s. Wan 2.6 renders 5, 10 or 15s only.",
                    },
                ),
                "aspect": (_all_aspects(), {"default": "16:9"}),
                "mode": (
                    _all_modes(),
                    {
                        "default": config.MODE_DEFAULT,
                        "tooltip": "Render quality, optionally with an upscale: '480p', '720p', '1080p', "
                        "'480p-1080p', '480p-4k', '720p-4k'… 'default' leaves it to the model.",
                    },
                ),
                "audio": ("BOOLEAN", {"default": True, "tooltip": "Render sound as well as picture."}),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 2147483647,
                        "control_after_generate": True,
                        "tooltip": "0 to 2147483647. Only sent when 'use_seed' is on.",
                    },
                ),
                "use_seed": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Off: the model picks the seed (needed for variations). On: the seed above is sent.",
                    },
                ),
                "variations": (
                    "INT", {"default": 1, "min": 1, "max": 4, "tooltip": "Up to four at once. Cannot be combined with a seed."},
                ),
                "camera_fixed": (
                    "BOOLEAN", {"default": False, "tooltip": "Seedance models only. Keeps the camera still."},
                ),
                "negative_prompt": (
                    "STRING", {"multiline": True, "default": "", "tooltip": "What to keep out. Wan 2.6 only, up to 500 characters."},
                ),
                "prompt_expansion": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Wan 2.6 only. Off renders your prompt exactly as written.",
                    },
                ),
                "draft": (
                    "BOOLEAN", {"default": False, "tooltip": "Seedance 2.5 only. A 480p draft you can finish at 1080p."},
                ),
                "image_urls": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "tooltip": "Public HTTPS images, one URL per line. These are references.",
                    },
                ),
                "uploaded_inputs": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "tooltip": 'Advanced. JSON list, e.g. [{"key": "uploads/a/b", "role": "reference_video", "seconds": 5}]',
                    },
                ),
                "extra_inputs": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "tooltip": "Advanced. JSON object merged into the request body, last. "
                        "Use it for fields this provider has that the widgets do not cover.",
                    },
                ),
                "webhook_url": (
                    "STRING", {"default": "", "tooltip": "Optional. The job body is POSTed here when it is ready or fails."},
                ),
                "poll_interval": ("INT", {"default": 10, "min": 2, "max": 120, "tooltip": "Seconds between status checks."}),
                "timeout_minutes": ("INT", {"default": 30, "min": 1, "max": 180}),
                "max_credits": (
                    "INT", {"default": 0, "min": 0, "max": 100000, "tooltip": "0 = no limit. Refuse the job if it would cost more."},
                ),
                "provider": (providers, provider_options),
                "api_key": (
                    "STRING", {"default": "", "tooltip": "Leave empty to use the provider's env var or your saved key."},
                ),
            },
            "optional": {
                "base_url": (
                    "STRING",
                    {"default": "", "tooltip": "Optional. Override the provider's base URL for this run."},
                ),
                "start_image": ("IMAGE", {"tooltip": "The image the video starts from."}),
                "end_image": ("IMAGE", {"tooltip": "The image the video ends on. Seedance only."}),
                "reference_images": ("IMAGE", {"tooltip": "Style or subject references."}),
            },
        }

    RETURN_TYPES = (VIDEO_TYPE, "STRING", "STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("video", "video_path", "job_ids", "credits", "status", "info")
    FUNCTION = "generate"
    CATEGORY = CATEGORY
    DESCRIPTION = "Make a video and wait until it is ready."

    def generate(
        self,
        prompt: str,
        model: str,
        duration_s: int,
        aspect: str,
        mode: str,
        audio: bool,
        seed: int,
        use_seed: bool,
        variations: int,
        camera_fixed: bool,
        negative_prompt: str,
        prompt_expansion: bool,
        draft: bool,
        image_urls: str,
        uploaded_inputs: str,
        extra_inputs: str,
        webhook_url: str,
        poll_interval: int,
        timeout_minutes: int,
        max_credits: int,
        provider: str,
        api_key: str,
        base_url: str = "",
        start_image: Any = None,
        end_image: Any = None,
        reference_images: Any = None,
    ):
        reporter = utils.ProgressReporter(total=100, label="Remerged")
        client, prof = _resolve_client(api_key, provider, base_url)

        if not (prompt or "").strip():
            raise ValueError("The prompt is empty.")

        guide = _model_guide(prof)
        info = _known_model_info(prof, model)

        if guide and model not in guide:
            raise ValueError(
                f"Unknown model '{model}' for {prof.get('label') or provider}. "
                f"Available: {', '.join(sorted(guide))}."
            )

        # -------------------------------------------------- duration
        requested_duration = int(duration_s)
        if info:
            duration = config.normalise_duration(model, requested_duration, provider)
            if duration != requested_duration:
                reporter.status(
                    f"{model} renders {info.get('min_duration')}-{info.get('max_duration')}s; "
                    f"using {duration}s."
                )
        else:
            duration = requested_duration

        # -------------------------------------------------- mode
        if info:
            allowed = config.modes_for_model(model, provider)
            if mode and mode != config.MODE_DEFAULT and allowed and mode not in allowed:
                raise ValueError(
                    f"'{mode}' is not a mode {model} accepts. Valid modes: {', '.join(allowed)}."
                )

        if variations > 1 and use_seed:
            raise ValueError(
                "A seed and variations cannot be used together - one seed makes one video. "
                "Turn 'use_seed' off, or set variations to 1."
            )

        if draft and info and model != "seedance-2.5":
            raise ValueError("A draft can only be made with Seedance 2.5.")

        # -------------------------------------------------- images
        images: List[Dict[str, Any]] = []
        frames = 0
        others = 0

        if start_image is not None:
            images.append({"key": _upload_image(client, start_image, reporter), "role": "first_frame"})
            frames += 1
        if end_image is not None:
            images.append({"key": _upload_image(client, end_image, reporter), "role": "last_frame"})
            frames += 1
        if reference_images is not None:
            try:
                count = int(reference_images.shape[0])
            except Exception:
                count = 1
            for index in range(count):
                key = client.upload_bytes(
                    utils.image_to_png_bytes(reference_images[index]), "image/png"
                )
                images.append({"key": key, "role": "reference"})
                others += 1

        extra_inputs_entries = _parse_uploaded_inputs(uploaded_inputs)
        images.extend(extra_inputs_entries)
        for entry in extra_inputs_entries:
            if entry.get("role") in ("first_frame", "last_frame"):
                frames += 1
            else:
                others += 1

        urls = [line.strip() for line in (image_urls or "").splitlines() if line.strip()]
        others += len(urls)

        # -------------------------------------------------- input rules
        if info:
            if info.get("seedance"):
                if frames and others:
                    raise ValueError(
                        "A Seedance video starts from frames or from references, not both. "
                        "Remove the reference images, or the first/last frame."
                    )
            elif info.get("single_image"):
                if frames + others > 1:
                    raise ValueError(
                        f"{model} takes a prompt and one starting image only. "
                        "You gave it more than one input."
                    )

            if info.get("wan") and aspect == "21:9" and others == 0:
                raise ValueError("Wan 2.6 renders no 21:9 from words. Use another aspect.")

        # -------------------------------------------------- body
        canonical = _core_canonical(prompt, model, duration, aspect, mode, audio)
        if images:
            canonical["images"] = images
        if urls:
            canonical["image_urls"] = urls
        if use_seed:
            canonical["seed"] = int(seed)
        if variations > 1:
            canonical["variations"] = int(variations)
        if camera_fixed and (not info or info.get("seedance")):
            canonical["camera_fixed"] = True
        if (negative_prompt or "").strip():
            if not info or info.get("wan"):
                canonical["negative_prompt"] = negative_prompt.strip()[:500]
            else:
                reporter.status("negative_prompt only applies to Wan 2.6; ignoring it here.")
        if not info or info.get("wan"):
            canonical["prompt_expansion"] = bool(prompt_expansion)
        if draft:
            canonical["draft"] = True
        if (webhook_url or "").strip():
            canonical["webhook_url"] = webhook_url.strip()[:2000]

        body = client.build_body(canonical)
        body = _merge_extra_inputs(extra_inputs, body)

        if not body.get("prompt") and not body.get("text"):
            raise ValueError(
                f"The '{provider}' profile has no field for the prompt, so there would be "
                "nothing to send. Fix the profile's 'job_fields'."
            )

        # -------------------------------------------------- price check
        quoted = None
        try:
            quoted = client.quote(body)
        except Exception:
            # A key without the read scope cannot quote; that is not fatal.
            quoted = None
        if quoted and max_credits > 0:
            credits = int(
                client._extract(
                    quoted, "quote", "credits", profile_mod.QUOTE_SYNONYMS["credits"], 0
                )
                or 0
            )
            if credits > max_credits:
                raise ValueError(
                    f"This job costs {credits} credits (${credits / 100:.2f}), over your limit of "
                    f"{max_credits} credits. Nothing was charged."
                )

        # -------------------------------------------------- create
        reporter.status("Creating the job...")
        reporter.update(5)
        created = client.create_job(body)

        job_ids = list(created.get("ids") or [])
        if not job_ids and created.get("id"):
            job_ids = [str(created["id"])]
        if not job_ids:
            raise RemergedError(
                "The API accepted the job but did not return an id.",
                payload=created.get("raw") if isinstance(created.get("raw"), dict) else {},
            )

        credits = int(created.get("credits") or 0)

        # -------------------------------------------------- poll
        timeout = max(60.0, float(timeout_minutes) * 60.0)
        jobs: List[Dict[str, Any]] = []
        paths: List[str] = []
        poster_paths: List[str] = []

        for position, job_id in enumerate(job_ids):
            def on_poll(job: Dict[str, Any], status: str, position=position) -> None:
                reporter.status(f"Job {job_id}: {status} ({reporter.elapsed()}s)")
                reporter.update(5 + int(90 * (position + 0.5) / len(job_ids)))

            job = wait_for_job(
                client,
                job_id,
                interval=poll_interval,
                timeout=timeout,
                on_poll=on_poll,
                should_abort=utils.check_interrupted,
            )
            jobs.append(job)
            assets = _download_job_assets(client, job, _slug_from_prompt(prompt), reporter)
            if assets.get("video_path"):
                paths.append(assets["video_path"])
            if assets.get("poster_path"):
                poster_paths.append(assets["poster_path"])

        reporter.update(100)
        reporter.status("Done.")

        if not jobs:
            raise RemergedError("The job finished but no video came back.")

        final = jobs[-1]
        status_text = client.job_status(final) or "ready"
        if final.get("draft"):
            finish_by = final.get("finish_by")
            if finish_by:
                reporter.status(f"Draft ready. Finish it at 1080p before {finish_by}.")

        info_payload = {
            "provider": prof.get("name"),
            "jobs": jobs,
            "credits": credits,
            "quote": quoted,
            "video_paths": paths,
            "poster_paths": poster_paths,
        }

        return (
            _video_output(paths),
            json.dumps(paths) if len(paths) != 1 else (paths[0] if paths else ""),
            json.dumps(job_ids),
            credits,
            status_text,
            json.dumps(info_payload, indent=2, default=str),
        )


# ---------------------------------------------------------------------- #
# 2. quote
# ---------------------------------------------------------------------- #
class RemergedQuote:
    """Price a job without making it."""

    @classmethod
    def INPUT_TYPES(cls):
        providers, provider_options = _provider_widget()
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "A hummingbird at a red flower"}),
                "model": (_all_models(), {"default": "seedance-2.5"}),
                "duration_s": ("INT", {"default": 5, "min": 4, "max": 30, "step": 1}),
                "aspect": (_all_aspects(), {"default": "16:9"}),
                "mode": (_all_modes(), {"default": config.MODE_DEFAULT}),
                "audio": ("BOOLEAN", {"default": True}),
                "provider": (providers, provider_options),
                "api_key": ("STRING", {"default": ""}),
            },
            "optional": {
                "base_url": ("STRING", {"default": "", "tooltip": "Optional. Override the provider's base URL."}),
            },
        }

    RETURN_TYPES = ("INT", "FLOAT", "INT", "STRING")
    RETURN_NAMES = ("credits", "usd", "per_video_credits", "raw")
    FUNCTION = "quote"
    CATEGORY = CATEGORY
    DESCRIPTION = "Work out what a job would cost, without making it."

    def quote(self, prompt, model, duration_s, aspect, mode, audio, provider, api_key, base_url=""):
        client, prof = _resolve_client(api_key, provider, base_url)

        info = _known_model_info(prof, model)
        duration = config.normalise_duration(model, int(duration_s), provider) if info else int(duration_s)
        canonical = _core_canonical(prompt, model, duration, aspect, mode, audio)
        body = client.build_body(canonical)

        result = client.quote(body) or {}
        credits = int(
            client._extract(result, "quote", "credits", profile_mod.QUOTE_SYNONYMS["credits"], 0) or 0
        )
        usd = float(
            client._extract(result, "quote", "usd", profile_mod.QUOTE_SYNONYMS["usd"], 0.0) or 0.0
        )
        per_video = int(
            client._extract(
                result, "quote", "per_video_credits", profile_mod.QUOTE_SYNONYMS["per_video_credits"], 0
            )
            or 0
        )
        return (credits, usd, per_video, json.dumps(result, indent=2, default=str))


# ---------------------------------------------------------------------- #
# 3. upload
# ---------------------------------------------------------------------- #
class RemergedUpload:
    """Upload a local file and get a key to use in a job."""

    @classmethod
    def INPUT_TYPES(cls):
        providers, provider_options = _provider_widget()
        return {
            "required": {
                "file_path": (
                    "STRING",
                    {"default": "", "tooltip": "Path to a local image, video or audio file."},
                ),
                "content_type": (utils.ALLOWED_UPLOAD_TYPES, {"default": "image/png"}),
                "provider": (providers, provider_options),
                "api_key": ("STRING", {"default": ""}),
            },
            "optional": {
                "image": ("IMAGE", {"tooltip": "Alternative to file_path. Encoded as PNG."}),
                "base_url": ("STRING", {"default": "", "tooltip": "Optional. Override the provider's base URL."}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "INT", "STRING")
    RETURN_NAMES = ("key", "content_type", "bytes", "raw")
    FUNCTION = "upload"
    CATEGORY = CATEGORY
    DESCRIPTION = "Upload an input file and return the key for a job."

    def upload(self, file_path, content_type, provider, api_key, image=None, base_url=""):
        client, _prof = _resolve_client(api_key, provider, base_url)

        if image is not None:
            payload = utils.image_to_png_bytes(image)
            resolved_type = "image/png"
        else:
            path = (file_path or "").strip()
            if not path:
                raise ValueError("Give a file_path or connect an image.")
            if not os.path.isfile(path):
                raise ValueError(f"No such file: {path}")
            payload, guessed = utils.read_file(path)
            resolved_type = content_type or guessed
            if guessed in utils.ALLOWED_UPLOAD_TYPES and content_type == "image/png":
                # A default content type on a real file should not override detection.
                resolved_type = guessed

        if resolved_type not in utils.ALLOWED_UPLOAD_TYPES:
            raise ValueError(
                f"'{resolved_type}' is not an accepted content type. "
                f"Allowed: {', '.join(utils.ALLOWED_UPLOAD_TYPES)}."
            )

        key, url = client.upload_ticket(resolved_type, len(payload))
        client.put_upload(url, payload, resolved_type)

        return (
            str(key),
            resolved_type,
            len(payload),
            json.dumps({"key": key, "url": url}, indent=2, default=str),
        )


# ---------------------------------------------------------------------- #
# 4. job status
# ---------------------------------------------------------------------- #
class RemergedJobStatus:
    """Look up a job you made earlier."""

    @classmethod
    def INPUT_TYPES(cls):
        providers, provider_options = _provider_widget()
        return {
            "required": {
                "job_id": ("STRING", {"default": ""}),
                "wait": ("BOOLEAN", {"default": True, "tooltip": "Poll until the job is ready."}),
                "poll_interval": ("INT", {"default": 10, "min": 2, "max": 120}),
                "timeout_minutes": ("INT", {"default": 30, "min": 1, "max": 180}),
                "provider": (providers, provider_options),
                "api_key": ("STRING", {"default": ""}),
            },
            "optional": {
                "base_url": ("STRING", {"default": "", "tooltip": "Optional. Override the provider's base URL."}),
            },
        }

    RETURN_TYPES = (VIDEO_TYPE, "STRING", "STRING", "STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("video", "video_path", "status", "video_url", "credits", "error", "raw")
    FUNCTION = "status"
    CATEGORY = CATEGORY
    DESCRIPTION = "Check a job, and fetch a fresh video URL for it."

    def status(self, job_id, wait, poll_interval, timeout_minutes, provider, api_key, base_url=""):
        reporter = utils.ProgressReporter(total=100, label="Remerged")
        client, _prof = _resolve_client(api_key, provider, base_url)
        job_id = (job_id or "").strip()
        if not job_id:
            raise ValueError("No job id given.")

        if wait:
            job = wait_for_job(
                client,
                job_id,
                interval=poll_interval,
                timeout=max(60.0, float(timeout_minutes) * 60.0),
                on_poll=lambda payload, status: reporter.status(f"Job {job_id}: {status}"),
                should_abort=utils.check_interrupted,
            )
        else:
            job = client.get_job(job_id)

        paths: List[str] = []
        if client.job_status(job) == "ready":
            video_url = client.job_field(job, "video")
            if video_url:
                assets = _download_job_assets(client, job, "remerged", reporter)
                if assets.get("video_path"):
                    paths.append(assets["video_path"])

        reporter.update(100)

        return (
            _video_output(paths),
            paths[0] if paths else "",
            client.job_status(job) or "unknown",
            str(client.job_field(job, "video") or ""),
            int(client.job_field(job, "credits") or 0),
            str(client.job_field(job, "error") or ""),
            json.dumps(job, indent=2, default=str),
        )


# ---------------------------------------------------------------------- #
# 5. finish a draft
# ---------------------------------------------------------------------- #
class RemergedFinishDraft:
    """Finish a draft at a higher resolution."""

    @classmethod
    def INPUT_TYPES(cls):
        providers, provider_options = _provider_widget()
        return {
            "required": {
                "job_id": ("STRING", {"default": "", "tooltip": "The id of the draft job."}),
                "mode": (["1080p", "720p", "480p"], {"default": "1080p"}),
                "webhook_url": ("STRING", {"default": ""}),
                "poll_interval": ("INT", {"default": 10, "min": 2, "max": 120}),
                "timeout_minutes": ("INT", {"default": 30, "min": 1, "max": 180}),
                "provider": (providers, provider_options),
                "api_key": ("STRING", {"default": ""}),
            },
            "optional": {
                "base_url": ("STRING", {"default": "", "tooltip": "Optional. Override the provider's base URL."}),
            },
        }

    RETURN_TYPES = (VIDEO_TYPE, "STRING", "STRING", "STRING", "INT", "STRING")
    RETURN_NAMES = ("video", "video_path", "job_id", "status", "credits", "raw")
    FUNCTION = "finish"
    CATEGORY = CATEGORY
    DESCRIPTION = "Finish a draft at 1080p - the same take, within seven days."

    def finish(self, job_id, mode, webhook_url, poll_interval, timeout_minutes, provider, api_key, base_url=""):
        reporter = utils.ProgressReporter(total=100, label="Remerged")
        client, _prof = _resolve_client(api_key, provider, base_url)
        job_id = (job_id or "").strip()
        if not job_id:
            raise ValueError("No draft job id given.")

        reporter.status("Finishing the draft...")
        created = client.finish_job(job_id, mode=mode or "1080p", webhook_url=(webhook_url or "").strip() or None)

        raw_ids = created.get("ids") if isinstance(created, dict) else None
        if not isinstance(raw_ids, list):
            raw_ids = []
        if not raw_ids and isinstance(created, dict) and created.get("id"):
            raw_ids = [str(created["id"])]
        new_ids = [str(item) for item in raw_ids]
        if not new_ids:
            raise RemergedError("The API did not return a job id for the finished draft.", payload={})

        jobs: List[Dict[str, Any]] = []
        paths: List[str] = []
        for new_id in new_ids:
            job = wait_for_job(
                client,
                new_id,
                interval=poll_interval,
                timeout=max(60.0, float(timeout_minutes) * 60.0),
                on_poll=lambda payload, status: reporter.status(f"Finishing {new_id}: {status}"),
                should_abort=utils.check_interrupted,
            )
            jobs.append(job)
            assets = _download_job_assets(client, job, f"finished-{job_id[:8]}", reporter)
            if assets.get("video_path"):
                paths.append(assets["video_path"])

        reporter.update(100)
        final = jobs[-1] if jobs else {}

        return (
            _video_output(paths),
            json.dumps(paths) if len(paths) != 1 else (paths[0] if paths else ""),
            json.dumps(new_ids),
            client.job_status(final) or "ready",
            int(client.job_field(final, "credits") or 0),
            json.dumps({"jobs": jobs, "raw": created}, indent=2, default=str),
        )


# ---------------------------------------------------------------------- #
# 6. account
# ---------------------------------------------------------------------- #
class RemergedAccount:
    """Balance, plan and limits."""

    @classmethod
    def INPUT_TYPES(cls):
        providers, provider_options = _provider_widget()
        return {
            "required": {
                "provider": (providers, provider_options),
                "api_key": ("STRING", {"default": ""}),
            },
            "optional": {
                "base_url": ("STRING", {"default": "", "tooltip": "Optional. Override the provider's base URL."}),
            },
        }

    RETURN_TYPES = ("INT", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("credits", "plan", "topup_url", "jobs_in_flight", "raw")
    FUNCTION = "account"
    CATEGORY = CATEGORY
    DESCRIPTION = "Show the balance, plan and limits."

    def account(self, provider, api_key, base_url=""):
        client, _prof = _resolve_client(api_key, provider, base_url)
        data = client.get_account() or {}

        def pick(*names, default=None):
            for name in names:
                if isinstance(data, dict) and name in data and data[name] is not None:
                    return data[name]
            found = profile_mod.search_key(data, list(names)) if isinstance(data, dict) else None
            return default if found is None else found

        credits = pick("credits", "balance_credits", "remaining_credits", default=0)
        try:
            credits = int(float(credits))
        except Exception:
            credits = 0

        plan = pick("plan", "plan_name", "tier", default="")
        topup_url = pick("topup_url", "add_credits_url", "topup", default="")
        in_flight = pick("jobs_in_flight", "in_flight", "active_jobs", default=0)
        try:
            in_flight = int(in_flight)
        except Exception:
            in_flight = 0

        return (
            credits,
            str(plan),
            str(topup_url),
            str(in_flight),
            json.dumps(data, indent=2, default=str),
        )


# ---------------------------------------------------------------------- #
# 7. models
# ---------------------------------------------------------------------- #
class RemergedModels:
    """The model catalogue."""

    @classmethod
    def INPUT_TYPES(cls):
        providers, provider_options = _provider_widget()
        return {
            "required": {
                "refresh": ("BOOLEAN", {"default": False, "tooltip": "Fetch the catalogue from the API."}),
                "provider": (providers, provider_options),
                "api_key": ("STRING", {"default": ""}),
            },
            "optional": {
                "base_url": ("STRING", {"default": "", "tooltip": "Optional. Override the provider's base URL."}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("model_ids", "modes", "details", "raw")
    FUNCTION = "models"
    CATEGORY = CATEGORY
    DESCRIPTION = "List the models, their modes and their limits."

    def models(self, refresh, provider, api_key, base_url=""):
        raw: Any = None
        if refresh:
            client, _prof = _resolve_client(api_key, provider, base_url)
            raw = client.get_models()
            config.save_catalogue(raw, provider)
        else:
            raw = config.load_catalogue(provider) or {}

        details: Dict[str, Any] = {}
        for model_id in config.model_ids(provider):
            info = config.model_info(model_id, provider)
            details[model_id] = {
                "label": info.get("label"),
                "durations": [info.get("min_duration"), info.get("max_duration")],
                "modes": config.modes_for_model(model_id, provider),
                "on_free": info.get("on_free"),
                "notes": info.get("notes"),
            }

        raw_text = json.dumps(raw, indent=2, default=str)

        return (
            json.dumps(config.model_ids(provider)),
            json.dumps(config.all_modes(provider)),
            json.dumps(details, indent=2, default=str),
            raw_text,
        )


# ---------------------------------------------------------------------- #
# 8. recent jobs
# ---------------------------------------------------------------------- #
class RemergedListJobs:
    """Your recent jobs."""

    @classmethod
    def INPUT_TYPES(cls):
        providers, provider_options = _provider_widget()
        return {
            "required": {
                "limit": ("INT", {"default": 20, "min": 1, "max": 100}),
                "provider": (providers, provider_options),
                "api_key": ("STRING", {"default": ""}),
            },
            "optional": {
                "base_url": ("STRING", {"default": "", "tooltip": "Optional. Override the provider's base URL."}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "INT")
    RETURN_NAMES = ("job_ids", "jobs", "count")
    FUNCTION = "list_jobs"
    CATEGORY = CATEGORY
    DESCRIPTION = "List your recent jobs, newest first."

    def list_jobs(self, limit, provider, api_key, base_url=""):
        client, _prof = _resolve_client(api_key, provider, base_url)
        payload = client.list_jobs(limit) or {}
        jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
        if not jobs and isinstance(payload, list):
            jobs = payload
        job_ids = [
            str(client.job_field(job, "id"))
            for job in jobs
            if isinstance(job, dict) and client.job_field(job, "id")
        ]
        return (
            json.dumps(job_ids),
            json.dumps(jobs, indent=2, default=str),
            len(job_ids),
        )


# ---------------------------------------------------------------------- #
# 9. provider setup
# ---------------------------------------------------------------------- #
class RemergedProvider:
    """Point the nodes at another OpenAPI service."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "provider_name": (
                    "STRING",
                    {"default": "my-api", "tooltip": "The name to save this provider under."},
                ),
                "base_url": (
                    "STRING",
                    {"default": "", "tooltip": "The service's base URL, e.g. https://api.example.com/v1"},
                ),
                "openapi_url": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "The OpenAPI document. Leave empty and a few likely URLs are tried.",
                    },
                ),
                "save": ("BOOLEAN", {"default": True, "tooltip": "Save the profile so the other nodes can use it."}),
                "overwrite": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "label": ("STRING", {"default": "", "tooltip": "A friendly name for the dropdown."}),
                "api_key": ("STRING", {"default": "", "tooltip": "Saved with the profile (chmod 600)."}),
                "test_connection": (
                    "BOOLEAN",
                    {"default": True, "tooltip": "Call the models endpoint afterwards to check it works."},
                ),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("provider_name", "endpoints", "warnings", "summary")
    FUNCTION = "setup"
    CATEGORY = CATEGORY
    DESCRIPTION = "Read an OpenAPI 3.1 document and build a provider profile from it."

    def setup(self, provider_name, base_url, openapi_url, save, overwrite, label="", api_key="", test_connection=True):
        name = profile_mod.safe_name(provider_name)
        existing = profile_mod.load_profile(name)
        if existing and not overwrite and existing.get("builtin"):
            raise ValueError(f"'{name}' is a built-in provider. Choose another name.")
        if existing and not overwrite:
            # Keep the saved profile's own key unless a new one is given.
            api_key = api_key or existing.get("api_key", "")

        spec, source = _fetch_openapi(openapi_url, base_url)

        if spec is None:
            raise ValueError(
                f"Could not read an OpenAPI document. Tried: {source}. "
                "Give 'openapi_url' explicitly."
            )

        profile = profile_mod.introspect(
            spec,
            base_url=base_url,
            openapi_url=openapi_url or source,
            name=name,
            label=label or provider_name,
        )
        if api_key:
            profile["api_key"] = api_key.strip()

        warnings = profile.get("warnings") or []

        if test_connection:
            try:
                client = ApiClient(profile, api_key, base_url=profile.get("base_url"))
                client.get_models()
                warnings = [w for w in warnings if "Could not find a 'models'" not in w]
                warnings.append("Connected: the models endpoint answered.")
            except Exception as exc:
                warnings.append(f"Connection test failed: {exc}")

        path = ""
        if save:
            path = profile_mod.save_profile(profile)

        endpoints = profile.get("endpoints") or {}
        summary = {
            "name": profile.get("name"),
            "label": profile.get("label"),
            "base_url": profile.get("base_url"),
            "openapi_url": profile.get("openapi_url"),
            "auth": profile.get("auth"),
            "endpoints": endpoints,
            "job_fields": profile.get("job_fields"),
            "responses": profile.get("responses"),
            "status_values": profile.get("status_values"),
            "saved_to": path,
        }

        return (
            profile.get("name", name),
            json.dumps(endpoints, indent=2),
            "\n".join(warnings) if warnings else "none",
            json.dumps(summary, indent=2, default=str),
        )


def _fetch_openapi(openapi_url: str, base_url: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read an OpenAPI document, guessing the URL when it is not given."""
    candidates: List[str] = []
    if openapi_url:
        candidates.append(openapi_url)
    host = (base_url or "").rstrip("/")
    if host:
        candidates.extend(
            [
                f"{host}/openapi.json",
                f"{host}/../openapi.json",
                f"{host}/api/openapi.json",
                f"{host}/docs/openapi.json",
            ]
        )

    tried: List[str] = []
    for candidate in candidates:
        tried.append(candidate)
        try:
            import urllib.request

            request = urllib.request.Request(candidate, method="GET")
            request.add_header("User-Agent", "comfyui-remerged/1.0")
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and ("openapi" in payload or "paths" in payload):
                return payload, candidate
        except Exception:
            continue

    return None, ", ".join(tried) if tried else "nothing"


NODE_CLASS_MAPPINGS = {
    "RemergedVideoGenerate": RemergedVideoGenerate,
    "RemergedQuote": RemergedQuote,
    "RemergedUpload": RemergedUpload,
    "RemergedJobStatus": RemergedJobStatus,
    "RemergedFinishDraft": RemergedFinishDraft,
    "RemergedAccount": RemergedAccount,
    "RemergedModels": RemergedModels,
    "RemergedListJobs": RemergedListJobs,
    "RemergedProvider": RemergedProvider,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RemergedVideoGenerate": "Remerged Video",
    "RemergedQuote": "Remerged Quote",
    "RemergedUpload": "Remerged Upload",
    "RemergedJobStatus": "Remerged Job Status",
    "RemergedFinishDraft": "Remerged Finish Draft",
    "RemergedAccount": "Remerged Account",
    "RemergedModels": "Remerged Models",
    "RemergedListJobs": "Remerged Recent Jobs",
    "RemergedProvider": "Remerged API Provider",
}
