"""Monitor geometry detection (xrandr with Gdk fallback)."""

import re
import subprocess
from dataclasses import dataclass

_XRANDR_RE = re.compile(
    r"^\s*(\d+):\s+(\+?\*?\+?)\S+\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)")


@dataclass
class Monitor:
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool
    name: str = ""


def monitors() -> list[Monitor]:
    mons = _from_xrandr()
    if not mons:
        mons = _from_gdk()
    if not mons:  # last resort: one virtual 1080p screen
        mons = [Monitor(0, 0, 0, 1920, 1080, True)]
    return mons


def _from_xrandr() -> list[Monitor]:
    try:
        out = subprocess.run(
            ["xrandr", "--listactivemonitors"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    mons = []
    for line in out.splitlines():
        m = _XRANDR_RE.match(line)
        if m:
            mons.append(Monitor(
                index=int(m.group(1)),
                x=int(m.group(5)), y=int(m.group(6)),
                width=int(m.group(3)), height=int(m.group(4)),
                primary="*" in m.group(2),
                name=m.group(7),
            ))
    return mons


def _from_gdk() -> list[Monitor]:
    try:
        import gi
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk
        Gdk.init([])
        display = Gdk.Display.get_default()
        if display is None:
            return []
        mons = []
        for i in range(display.get_n_monitors()):
            mon = display.get_monitor(i)
            geo = mon.get_geometry()
            mons.append(Monitor(i, geo.x, geo.y, geo.width, geo.height,
                                mon.is_primary()))
        return mons
    except Exception:
        return []


def virtual_bounds(mons: list[Monitor]) -> tuple[int, int, int, int]:
    """(x, y, width, height) of the bounding box of all monitors."""
    x0 = min(m.x for m in mons)
    y0 = min(m.y for m in mons)
    x1 = max(m.x + m.width for m in mons)
    y1 = max(m.y + m.height for m in mons)
    return x0, y0, x1 - x0, y1 - y0
