"""
Configuration, credential lookup and the model catalogue.

The API key can come from four places, in this order:

1. the ``api_key`` widget on a node,
2. the provider's own environment variable (``REMERGED_API_KEY`` for Remerged),
3. ``<ComfyUI>/user/remerged/config.json``,
4. ``<ComfyUI>/user/remerged_api_key.txt``.

The model catalogue (``GET /models``) is cached per provider as JSON in the
ComfyUI user directory so the node menus stay useful even when the API is
unreachable.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from . import profile as profile_mod

#: Environment variables that may hold the key.
ENV_KEY_NAMES = ("REMERGED_API_KEY", "REMERGED_TOKEN")

#: Environment variables that may override the API host (proxies, self hosting).
ENV_BASE_URL_NAMES = ("REMERGED_BASE_URL", "REMERGED_API_BASE_URL")

ASPECTS: List[str] = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"]

#: ``"default"`` means "let the model pick its own lowest quality".
MODE_DEFAULT = "default"

#: Cache of the live catalogue, per provider.
_CATALOGUE: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------- #
# locations
# ---------------------------------------------------------------------- #
def _comfy_user_dir() -> str:
    try:
        import folder_paths  # type: ignore

        base = folder_paths.get_user_directory()
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".comfyui", "user")
    return base


def user_dir() -> str:
    """``<ComfyUI>/user/remerged`` - created on demand."""
    path = os.path.join(_comfy_user_dir(), "remerged")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def catalogue_path(provider: str = "remerged") -> str:
    return os.path.join(user_dir(), f"{profile_mod.safe_name(provider)}-catalogue.json")


def config_path() -> str:
    return os.path.join(user_dir(), "config.json")


# ---------------------------------------------------------------------- #
# credentials
# ---------------------------------------------------------------------- #
def _read_saved_key() -> str:
    path = os.path.join(_comfy_user_dir(), "remerged_api_key.txt")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return ""


def get_api_key(override: str = "", provider: str = "remerged") -> str:
    """Resolve the API key from the widget, the environment or a saved file.

    A provider may carry its own key (saved with the profile) and its own
    environment variable; both take precedence over the global fallbacks.
    """
    if override and override.strip():
        return override.strip()

    prof = profile_mod.load_profile(provider) or {}

    env_name = prof.get("api_key_env")
    if env_name:
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()

    saved = prof.get("api_key")
    if isinstance(saved, str) and saved.strip():
        return saved.strip()

    for name in ENV_KEY_NAMES:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()

    try:
        with open(config_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            value = data.get("api_key")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        pass

    return _read_saved_key()


def save_api_key(api_key: str) -> str:
    """Store the key so it does not have to be typed into every graph."""
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("Refusing to save an empty API key.")
    path = config_path()
    data: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        pass
    data["api_key"] = api_key
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def get_base_url(default: str = "", provider: str = "remerged") -> str:
    """The API host.

    Precedence:

    1. the node's own ``base_url`` widget (handled by the caller),
    2. ``<PROVIDER>_BASE_URL`` in the environment,
    3. ``base_urls[<provider>]`` in ``config.json``,
    4. the provider profile's own base URL,
    5. for the built-in provider only, ``REMERGED_BASE_URL`` or the global
       ``base_url`` in ``config.json``.

    The global overrides deliberately do not reach custom providers: a
    Remerged proxy setting must not redirect somebody else's API.
    """
    from .api import DEFAULT_BASE_URL

    name = profile_mod.safe_name(provider)
    is_builtin = name == "remerged"

    scoped_env = f"{name.upper().replace('-', '_')}_BASE_URL"
    value = os.environ.get(scoped_env)
    if value and value.strip():
        return value.strip().rstrip("/")

    data: Dict[str, Any] = {}
    try:
        with open(config_path(), "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        pass

    scoped = (data.get("base_urls") or {}).get(name) if isinstance(data, dict) else None
    if isinstance(scoped, str) and scoped.strip():
        return scoped.strip().rstrip("/")

    if is_builtin:
        for env_name in ENV_BASE_URL_NAMES:
            value = os.environ.get(env_name)
            if value and value.strip():
                return value.strip().rstrip("/")
        value = data.get("base_url") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")

    prof = profile_mod.load_profile(provider) or {}
    saved = prof.get("base_url")
    if isinstance(saved, str) and saved.strip():
        return saved.strip().rstrip("/")

    return (default or DEFAULT_BASE_URL).rstrip("/")


# ---------------------------------------------------------------------- #
# catalogue
# ---------------------------------------------------------------------- #
def load_catalogue(provider: str = "remerged") -> Optional[Dict[str, Any]]:
    """Read the cached catalogue for a provider from disk."""
    key = profile_mod.safe_name(provider)
    if key in _CATALOGUE:
        return _CATALOGUE[key]
    try:
        with open(catalogue_path(key), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            _CATALOGUE[key] = data
            return data
    except Exception:
        pass
    return None


def save_catalogue(payload: Any, provider: str = "remerged") -> Dict[str, Any]:
    """Normalise and persist a provider's catalogue, then return it."""
    key = profile_mod.safe_name(provider)
    normalised = profile_mod.normalise_catalogue(payload)
    _CATALOGUE[key] = normalised
    try:
        with open(catalogue_path(key), "w", encoding="utf-8") as handle:
            json.dump(normalised, handle, indent=2)
    except Exception:
        pass
    return normalised


def refresh_catalogue(client, provider: str = "remerged") -> Dict[str, Any]:
    """Fetch the live catalogue and cache it."""
    return save_catalogue(client.get_models(), provider)


def model_ids(provider: str = "remerged") -> List[str]:
    """Model ids for the dropdown: the provider's guide, then the live catalogue."""
    prof = profile_mod.load_profile(provider) or {}
    guide = prof.get("models") or {}
    if guide:
        return list(guide.keys())

    catalogue = load_catalogue(provider) or {}
    if catalogue:
        return list(catalogue.keys())

    return list(profile_mod.MODEL_IDS)


def model_info(model_id: str, provider: str = "remerged") -> Dict[str, Any]:
    """Everything known about one model: the provider's guide plus the catalogue."""
    prof = profile_mod.load_profile(provider) or {}
    info = dict((prof.get("models") or {}).get(model_id, {}))
    info.setdefault("label", model_id)
    info.setdefault("min_duration", 4)
    info.setdefault("max_duration", 30)
    info.setdefault("renditions", ["480p", "720p", "1080p"])
    info.setdefault("upscales", ["1080p", "4k"])
    info.setdefault("seedance", False)
    info.setdefault("single_image", False)
    info.setdefault("on_free", False)

    catalogue = load_catalogue(provider) or {}
    live = catalogue.get(model_id)
    if live:
        modes = live.get("modes")
        if isinstance(modes, list) and modes:
            info["live_modes"] = [str(mode) for mode in modes]
    return info


def modes_for_model(model_id: str, provider: str = "remerged") -> List[str]:
    """Every mode a model accepts.

    The provider's guide is authoritative and the live catalogue only ever
    *adds* to it: refusing a mode the API would have accepted is worse than
    letting the API decide.
    """
    info = model_info(model_id, provider)
    renditions = list(info.get("renditions") or [])
    upscales = list(info.get("upscales") or [])

    modes: List[str] = []
    for rendition in renditions:
        if rendition not in modes:
            modes.append(rendition)
    for rendition in renditions:
        for upscale in upscales:
            if rendition == upscale:
                continue
            combined = f"{rendition}-{upscale}"
            if combined not in modes:
                modes.append(combined)
    for mode in info.get("live_modes") or []:
        if mode not in modes:
            modes.append(mode)
    return modes


def all_modes(provider: str = "remerged") -> List[str]:
    """Every mode any model accepts, for the node dropdown."""
    modes = [MODE_DEFAULT]
    for model_id in model_ids(provider):
        for mode in modes_for_model(model_id, provider):
            if mode not in modes:
                modes.append(mode)
    if len(modes) == 1:
        modes = [
            MODE_DEFAULT,
            "480p",
            "720p",
            "1080p",
            "480p-1080p",
            "480p-4k",
            "720p-1080p",
            "720p-4k",
            "1080p-4k",
        ]
    return modes


# ---------------------------------------------------------------------- #
# validation helpers
# ---------------------------------------------------------------------- #
def slugify(text: str, max_length: int = 48) -> str:
    """A filesystem friendly fragment of a prompt."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return (cleaned[:max_length].strip("-")) or "remerged"


def normalise_duration(model_id: str, duration: int, provider: str = "remerged") -> int:
    """Clamp to the model's range and snap Wan 2.6 to 5/10/15."""
    info = model_info(model_id, provider)
    steps = info.get("duration_steps")
    if steps:
        for step in steps:
            if duration <= step:
                return int(step)
        return int(steps[-1])
    duration = max(int(info.get("min_duration", 4)), min(int(info.get("max_duration", 30)), int(duration)))
    return duration
