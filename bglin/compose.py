"""Compose a spanned canvas with a different image per monitor.

gsettings backends can only show one file, so per-monitor images are
stitched into a single canvas covering the virtual screen and applied with
picture-options "spanned".
"""

from pathlib import Path

from PIL import Image, ImageOps

from . import paths
from .screens import Monitor, virtual_bounds

# Two alternating file names so the URI value always changes between
# applications (same-URI writes would not trigger a desktop refresh).
_SLOTS = ("span-a.png", "span-b.png")
_STATE = {"flip": 0}


def compose_span(assignments: dict[int, str | None],
                 monitors: list[Monitor]) -> Path:
    """assignments: monitor index -> image path (None = leave black,
    e.g. a monitor covered by a video window)."""
    x0, y0, width, height = virtual_bounds(monitors)
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    for mon in monitors:
        src = assignments.get(mon.index)
        if not src or not Path(src).exists():
            continue
        try:
            with Image.open(src) as img:
                tile = ImageOps.fit(img.convert("RGB"),
                                    (mon.width, mon.height))
        except OSError:
            continue
        canvas.paste(tile, (mon.x - x0, mon.y - y0))
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _STATE["flip"] = 1 - _STATE["flip"]
    dest = paths.CACHE_DIR / _SLOTS[_STATE["flip"]]
    canvas.save(dest, "PNG")
    return dest
