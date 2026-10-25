"""
Helpers that bridge the Remerged API and the ComfyUI runtime.

Everything ComfyUI specific (progress bars, interruption, the output folder,
the native ``VIDEO`` type) is imported lazily and defensively, so this module
also imports cleanly outside of ComfyUI - which is what makes the test suite
possible.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from typing import Any, Optional, Tuple

#: Content types the Remerged upload endpoint accepts.
ALLOWED_UPLOAD_TYPES = [
    "image/jpeg",
    "image/png",
    "image/webp",
    "video/mp4",
    "video/quicktime",
    "audio/mpeg",
    "audio/wav",
]

_EXTENSION_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".qt": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


# ---------------------------------------------------------------------- #
# ComfyUI plumbing
# ---------------------------------------------------------------------- #
def output_directory() -> str:
    """ComfyUI's output folder, with a sane fallback outside of ComfyUI."""
    try:
        import folder_paths  # type: ignore

        return folder_paths.get_output_directory()
    except Exception:
        return os.path.join(os.getcwd(), "output")


def remerged_output_directory() -> str:
    """``<output>/Remerged`` - where finished videos are written."""
    path = os.path.join(output_directory(), "Remerged")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def check_interrupted() -> None:
    """Raise if the user pressed the interrupt button."""
    module = sys.modules.get("comfy.model_management")
    if module is None:
        try:
            import comfy.model_management as module  # type: ignore
        except Exception:
            return
    checker = getattr(module, "throw_exception_if_processing_interrupted", None)
    if callable(checker):
        checker()


def video_type_available() -> bool:
    """True when this ComfyUI build knows the native ``VIDEO`` type."""
    try:
        from comfy_api.input_impl import VideoFromFile  # type: ignore # noqa: F401

        return True
    except Exception:
        pass
    try:
        from comfy_api.latest._input_impl import VideoFromFile  # type: ignore # noqa: F401

        return True
    except Exception:
        return False


def make_video(path: str):
    """Wrap a saved file in ComfyUI's native video input type."""
    try:
        from comfy_api.input_impl import VideoFromFile  # type: ignore
    except Exception:
        from comfy_api.latest._input_impl import VideoFromFile  # type: ignore

    return VideoFromFile(path)


class ProgressReporter:
    """Progress bar plus a status line in the ComfyUI UI.

    Both halves are optional; the reporter silently degrades when the ComfyUI
    runtime is not present.
    """

    def __init__(self, total: int = 100, label: str = "Remerged") -> None:
        self.total = max(1, int(total))
        self.label = label
        self._bar = None
        self._last_message = ""
        self._started = time.time()

        try:
            from comfy.utils import ProgressBar  # type: ignore

            self._bar = ProgressBar(self.total)
        except Exception:
            self._bar = None

    def update(self, value: int, total: Optional[int] = None, preview: Any = None) -> None:
        if self._bar is None:
            return
        try:
            self._bar.update_absolute(int(value), total=total, preview=preview)
        except TypeError:
            try:
                self._bar.update_absolute(int(value))
            except Exception:
                pass
        except Exception:
            pass

    def status(self, message: str) -> None:
        """Push a human readable status line into the ComfyUI UI."""
        self._last_message = message
        try:
            from comfy.server import PromptServer  # type: ignore

            server = PromptServer.instance
            server.send_sync(
                "remerged.status",
                {"message": message, "elapsed": round(time.time() - self._started, 1)},
            )
        except Exception:
            pass

    def elapsed(self) -> float:
        return round(time.time() - self._started, 1)


# ---------------------------------------------------------------------- #
# image / file handling
# ---------------------------------------------------------------------- #
def content_type_for_path(path: str) -> str:
    ext = os.path.splitext(path or "")[1].lower()
    return _EXTENSION_TYPES.get(ext, "application/octet-stream")


def image_to_png_bytes(image: Any) -> bytes:
    """Encode a ComfyUI ``IMAGE`` tensor as PNG bytes.

    ComfyUI images are ``(B, H, W, C)`` float tensors in the 0..1 range.
    """
    import numpy as np
    from PIL import Image

    array = image[0]
    if hasattr(array, "detach"):
        array = array.detach()
    if hasattr(array, "cpu"):
        array = array.cpu()
    array = np.asarray(array)

    if array.ndim == 2:
        array = array[:, :, None]
    if array.shape[2] == 1:
        array = np.repeat(array, 3, axis=2)
    elif array.shape[2] > 4:
        array = array[:, :, :4]

    if array.dtype != np.uint8:
        array = np.clip(array.astype("float32"), 0.0, 1.0) * 255.0
        array = np.round(array).astype(np.uint8)

    mode = "RGBA" if array.shape[2] == 4 else "RGB"
    buffer = io.BytesIO()
    Image.fromarray(array, mode=mode).save(buffer, format="PNG")
    return buffer.getvalue()


def image_batch_to_png_bytes(images: Any) -> list:
    """Encode every frame of an ``IMAGE`` batch as its own PNG."""
    if images is None:
        return []
    try:
        count = int(images.shape[0])
    except Exception:
        count = 1
    return [image_to_png_bytes(images[index]) for index in range(count)]


def read_file(path: str) -> Tuple[bytes, str]:
    """Read a local file and guess its content type."""
    with open(path, "rb") as handle:
        data = handle.read()
    return data, content_type_for_path(path)


def unique_path(directory: str, stem: str, extension: str) -> str:
    """A path that will not collide with an existing file."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = os.path.join(directory, f"{stem}-{stamp}{extension}")
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}-{stamp}-{counter}{extension}")
        counter += 1
    return candidate


def parse_json_list(text: str) -> list:
    """Parse a JSON array from a widget, tolerating blank input."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError(f"Expected a JSON array, but it could not be read: {exc}") from exc
    if isinstance(data, dict):
        return [data]
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array.")
    return data
