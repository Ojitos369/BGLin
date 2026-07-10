"""Thumbnail generation with disk cache.

Images via Pillow; videos via ffmpegthumbnailer (fallback: ffmpeg).
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

from PIL import Image

from . import paths
from .catalog import is_video

THUMB_SIZE = (320, 320)


def thumbnail_for(media_path: str) -> Path | None:
    """Return path to a cached thumbnail, generating it if needed."""
    src = Path(media_path)
    if not src.exists():
        return None
    key = hashlib.md5(
        f"{src}|{src.stat().st_mtime_ns}|{THUMB_SIZE}".encode()
    ).hexdigest()
    dest = paths.THUMB_DIR / f"{key}.png"
    if dest.exists():
        return dest
    paths.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    if is_video(media_path):
        return _video_thumb(src, dest)
    try:
        with Image.open(src) as img:
            img.thumbnail(THUMB_SIZE)
            img.convert("RGB").save(dest, "PNG")
        return dest
    except OSError:
        return None


def _video_thumb(src: Path, dest: Path) -> Path | None:
    """Extract a representative frame, like LumaLoop's VideoThumbnailLoader."""
    try:
        if shutil.which("ffmpegthumbnailer"):
            subprocess.run(
                ["ffmpegthumbnailer", "-i", str(src), "-o", str(dest),
                 "-s", str(THUMB_SIZE[0]), "-q", "8"],
                check=True, capture_output=True, timeout=30)
        elif shutil.which("ffmpeg"):
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "3", "-i", str(src),
                 "-frames:v", "1", "-vf", f"scale={THUMB_SIZE[0]}:-1",
                 str(dest)],
                check=True, capture_output=True, timeout=30)
        else:
            return None
        return dest if dest.exists() else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
