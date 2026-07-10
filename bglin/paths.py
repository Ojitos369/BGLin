"""XDG paths used by bglin."""

import os
from pathlib import Path

APP = "bglin"


def _xdg(env: str, fallback: str) -> Path:
    return Path(os.environ.get(env, os.path.expanduser(fallback)))


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config") / APP
CACHE_DIR = _xdg("XDG_CACHE_HOME", "~/.cache") / APP
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state") / APP
THUMB_DIR = CACHE_DIR / "thumbs"

CONFIG_FILE = CONFIG_DIR / "config.json"
CATALOG_FILE = CONFIG_DIR / "catalog.json"
STATE_FILE = STATE_DIR / "state.json"

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/{APP}-{os.getuid()}"))
SOCKET_PATH = RUNTIME_DIR / f"{APP}.sock"

DEFAULT_MEDIA_DIR = Path(os.path.expanduser("~/Pictures")) / APP


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, STATE_DIR, THUMB_DIR, DEFAULT_MEDIA_DIR):
        d.mkdir(parents=True, exist_ok=True)
