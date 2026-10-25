#!/usr/bin/env python3
"""
Save a Remerged API key so you do not have to paste it into every graph.

    python3 scripts/set_api_key.py rm_live_your_key_here

The key is written to ``<ComfyUI>/user/remerged/config.json``.

Run it with ``--user-dir`` if ComfyUI lives somewhere unusual::

    python3 scripts/set_api_key.py rm_live_... --user-dir /opt/ComfyUI/user
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)


def find_user_dir(explicit=""):
    if explicit:
        return explicit

    # Running inside ComfyUI: ask it where the user folder is.
    try:
        sys.path.insert(0, PKG_DIR)
        import folder_paths  # type: ignore

        return folder_paths.get_user_directory()
    except Exception:
        pass

    for guess in (
        os.path.join(os.getcwd(), "user"),
        os.path.join(os.path.dirname(os.path.abspath(os.getcwd())), "user"),
        os.path.join(os.path.expanduser("~"), ".comfyui", "user"),
    ):
        if os.path.isdir(guess):
            return guess

    return os.path.join(os.path.expanduser("~"), ".comfyui", "user")


def main():
    parser = argparse.ArgumentParser(description="Save a Remerged API key.")
    parser.add_argument("api_key", help="Your key, starting with rm_live_")
    parser.add_argument("--user-dir", default="", help="ComfyUI's user folder.")
    parser.add_argument("--base-url", default="", help="Override the API host.")
    args = parser.parse_args()

    key = args.api_key.strip()
    if not key:
        parser.error("The key is empty.")
    if not key.startswith("rm_"):
        print("Warning: keys normally start with 'rm_'.", file=sys.stderr)

    user_dir = find_user_dir(args.user_dir)
    target_dir = os.path.join(user_dir, "remerged")
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, "config.json")

    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    data["api_key"] = key
    if args.base_url:
        data["base_url"] = args.base_url.strip().rstrip("/")

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)

    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

    print(f"Saved to {path}")
    print("Restart ComfyUI to pick it up.")


if __name__ == "__main__":
    main()
