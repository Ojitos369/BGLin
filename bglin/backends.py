"""Wallpaper-setting backends for the major Linux desktops.

Equivalent role to LumaLoop's WallpaperService: this is the layer that
actually paints the desktop. Auto-detects the running environment.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import paths

ORIGINAL_FILE = paths.STATE_DIR / "original.json"

# bglin mode -> (cinnamon/gnome picture-options, feh flag, xfce image-style)
_MODE_MAP = {
    "zoom": ("zoom", "--bg-fill", 5),
    "scaled": ("scaled", "--bg-max", 4),
    "stretched": ("stretched", "--bg-scale", 3),
    "centered": ("centered", "--bg-center", 1),
    "wallpaper": ("wallpaper", "--bg-tile", 2),
    "spanned": ("spanned", "--bg-fill", 5),
}


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, timeout=15)


def _gsettings_get(schema: str, key: str) -> str:
    return subprocess.run(
        ["gsettings", "get", schema, key],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip().strip("'")


class Backend:
    name = "base"

    def set_wallpaper(self, image: Path, mode: str) -> None:
        raise NotImplementedError

    def get_current(self) -> dict | None:
        """Snapshot of the current wallpaper settings, or None if unsupported."""
        return None

    def apply_snapshot(self, snapshot: dict) -> None:
        pass

    @staticmethod
    def available() -> bool:
        return False


class CinnamonBackend(Backend):
    name = "cinnamon"
    SCHEMA = "org.cinnamon.desktop.background"

    def set_wallpaper(self, image: Path, mode: str) -> None:
        opt = _MODE_MAP.get(mode, _MODE_MAP["zoom"])[0]
        _run(["gsettings", "set", self.SCHEMA, "picture-uri", image.as_uri()])
        _run(["gsettings", "set", self.SCHEMA, "picture-options", opt])

    def get_current(self) -> dict | None:
        return {
            "picture-uri": _gsettings_get(self.SCHEMA, "picture-uri"),
            "picture-options": _gsettings_get(self.SCHEMA, "picture-options"),
        }

    def apply_snapshot(self, snapshot: dict) -> None:
        for key, value in snapshot.items():
            _run(["gsettings", "set", self.SCHEMA, key, value])

    @staticmethod
    def available() -> bool:
        return "cinnamon" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() \
            and shutil.which("gsettings") is not None


class GnomeBackend(Backend):
    name = "gnome"
    SCHEMA = "org.gnome.desktop.background"

    def set_wallpaper(self, image: Path, mode: str) -> None:
        opt = _MODE_MAP.get(mode, _MODE_MAP["zoom"])[0]
        uri = image.as_uri()
        for key in ("picture-uri", "picture-uri-dark"):
            _run(["gsettings", "set", self.SCHEMA, key, uri])
        _run(["gsettings", "set", self.SCHEMA, "picture-options", opt])

    def get_current(self) -> dict | None:
        return {key: _gsettings_get(self.SCHEMA, key)
                for key in ("picture-uri", "picture-uri-dark", "picture-options")}

    def apply_snapshot(self, snapshot: dict) -> None:
        for key, value in snapshot.items():
            _run(["gsettings", "set", self.SCHEMA, key, value])

    @staticmethod
    def available() -> bool:
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        return ("gnome" in de or "unity" in de or "budgie" in de) \
            and shutil.which("gsettings") is not None


class MateBackend(Backend):
    name = "mate"
    SCHEMA = "org.mate.background"

    def set_wallpaper(self, image: Path, mode: str) -> None:
        opt = _MODE_MAP.get(mode, _MODE_MAP["zoom"])[0]
        _run(["gsettings", "set", self.SCHEMA, "picture-filename", str(image)])
        _run(["gsettings", "set", self.SCHEMA, "picture-options", opt])

    def get_current(self) -> dict | None:
        return {key: _gsettings_get(self.SCHEMA, key)
                for key in ("picture-filename", "picture-options")}

    def apply_snapshot(self, snapshot: dict) -> None:
        for key, value in snapshot.items():
            _run(["gsettings", "set", self.SCHEMA, key, value])

    @staticmethod
    def available() -> bool:
        return "mate" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() \
            and shutil.which("gsettings") is not None


class XfceBackend(Backend):
    name = "xfce"

    def set_wallpaper(self, image: Path, mode: str) -> None:
        style = _MODE_MAP.get(mode, _MODE_MAP["zoom"])[2]
        props = subprocess.run(
            ["xfconf-query", "-c", "xfce4-desktop", "-l"],
            capture_output=True, text=True, timeout=15,
        ).stdout.splitlines()
        for prop in props:
            if prop.endswith("/last-image") or prop.endswith("/image-path"):
                _run(["xfconf-query", "-c", "xfce4-desktop", "-p", prop,
                      "-s", str(image)])
            elif prop.endswith("/image-style"):
                _run(["xfconf-query", "-c", "xfce4-desktop", "-p", prop,
                      "-s", str(style)])

    @staticmethod
    def available() -> bool:
        return "xfce" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() \
            and shutil.which("xfconf-query") is not None


class KdeBackend(Backend):
    name = "kde"

    def set_wallpaper(self, image: Path, mode: str) -> None:
        _run(["plasma-apply-wallpaperimage", str(image)])

    @staticmethod
    def available() -> bool:
        return "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() \
            and shutil.which("plasma-apply-wallpaperimage") is not None


class SwwwBackend(Backend):
    """Wayland compositors (Hyprland, Sway, ...) via swww."""
    name = "swww"

    def set_wallpaper(self, image: Path, mode: str) -> None:
        resize = {"zoom": "crop", "scaled": "fit", "stretched": "stretch"}.get(mode, "crop")
        _run(["swww", "img", str(image), "--resize", resize,
              "--transition-type", "fade"])

    @staticmethod
    def available() -> bool:
        return os.environ.get("XDG_SESSION_TYPE") == "wayland" \
            and shutil.which("swww") is not None


class FehBackend(Backend):
    """Fallback for plain X11 window managers."""
    name = "feh"

    def set_wallpaper(self, image: Path, mode: str) -> None:
        flag = _MODE_MAP.get(mode, _MODE_MAP["zoom"])[1]
        _run(["feh", flag, str(image)])

    @staticmethod
    def available() -> bool:
        return shutil.which("feh") is not None


_BACKENDS = [CinnamonBackend, GnomeBackend, MateBackend, XfceBackend,
             KdeBackend, SwwwBackend, FehBackend]


def save_original(backend: Backend) -> None:
    """Snapshot the pre-bglin wallpaper once (no-op if already saved)."""
    if ORIGINAL_FILE.exists():
        return
    snapshot = backend.get_current()
    if snapshot:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        ORIGINAL_FILE.write_text(
            json.dumps({"backend": backend.name, "snapshot": snapshot}))


def restore_original(backend: Backend) -> bool:
    """Put back the wallpaper that was set before bglin took over."""
    if not ORIGINAL_FILE.exists():
        return False
    try:
        data = json.loads(ORIGINAL_FILE.read_text())
        if data.get("backend") == backend.name and data.get("snapshot"):
            backend.apply_snapshot(data["snapshot"])
        ORIGINAL_FILE.unlink()
        return True
    except (json.JSONDecodeError, OSError):
        return False


def get_backend(preferred: str = "auto") -> Backend:
    if preferred != "auto":
        for cls in _BACKENDS:
            if cls.name == preferred:
                return cls()
        raise ValueError(f"Unknown backend: {preferred}")
    for cls in _BACKENDS:
        if cls.available():
            return cls()
    raise RuntimeError(
        "No wallpaper backend detected. Install feh or run inside a "
        "supported desktop (Cinnamon, GNOME, MATE, XFCE, KDE, swww)."
    )
