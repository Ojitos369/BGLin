"""Video wallpaper support.

Two halves:

* A GTK/GStreamer player (run as `python3 -m bglin.videowall --spec JSON`)
  that opens one desktop-type window per assigned monitor, below everything
  else, looping each video muted — the Linux equivalent of LumaLoop's
  ExoPlayer rendering path.
* A tiny process manager (start/stop) used by the engine, with a pid file
  so any bglin invocation (daemon, GUI, one-shot CLI) can replace or kill
  the current video wallpaper.

Spec format: [{"path": ..., "monitor": {"x":0,"y":0,"width":1920,"height":1080},
               "mode": "zoom", "audio": false}, ...]
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from . import paths

PID_FILE = paths.STATE_DIR / "video.pid"
_MARKER = "bglin.videowall"

_duration_cache: dict[str, float | None] = {}


def video_duration(path: str) -> float | None:
    """Video length in seconds via ffprobe (cached), None if unknown."""
    if path in _duration_cache:
        return _duration_cache[path]
    duration = None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if out:
            duration = float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _duration_cache[path] = duration
    return duration


# ---------- process management (engine side) ----------
#
# One player process per "slot": slot "all" covers every monitor (same-mode),
# slot "0"/"1"/... covers one monitor (different-mode). Independent slots
# mean replacing one monitor's video never restarts the others.

def _pid_file(slot: str) -> Path:
    return paths.STATE_DIR / f"video-{slot}.pid"


def _kill_pid_file(pid_file: Path) -> None:
    try:
        pid = int(pid_file.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        if _MARKER in cmdline:  # make sure the pid was not recycled
            os.kill(pid, signal.SIGTERM)
    except (ValueError, FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    pid_file.unlink(missing_ok=True)


def start(entries: list[dict], slot: str = "all") -> None:
    """Replace the video wallpaper in this slot with these windows."""
    stop(slot)
    if not entries:
        return
    env = dict(os.environ)
    # The child resolves `-m bglin.videowall` through PYTHONPATH, not cwd
    pkg_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", _MARKER, "--spec", json.dumps(entries)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _pid_file(slot).write_text(str(proc.pid))


def stop(slot: str = "all") -> None:
    """Kill the video wallpaper windows of one slot."""
    _kill_pid_file(_pid_file(slot))
    if slot == "all" and PID_FILE.exists():  # legacy single pid file
        _kill_pid_file(PID_FILE)


def stop_all() -> None:
    """Kill every video wallpaper window (all slots)."""
    if paths.STATE_DIR.exists():
        for pid_file in paths.STATE_DIR.glob("video-*.pid"):
            _kill_pid_file(pid_file)
    if PID_FILE.exists():
        _kill_pid_file(PID_FILE)


def is_playing() -> bool:
    if not paths.STATE_DIR.exists():
        return False
    for pid_file in list(paths.STATE_DIR.glob("video-*.pid")) + [PID_FILE]:
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text().strip())
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
            if _MARKER in cmdline:
                return True
        except (ValueError, FileNotFoundError):
            continue
    return False


# ---------- player windows (subprocess side) ----------

def _make_window(entry: dict, Gtk, Gdk, Gst, GLib):
    geo = entry["monitor"]
    mode = entry.get("mode", "zoom")

    window = Gtk.Window(title="bglin-videowall")
    window.set_type_hint(Gdk.WindowTypeHint.DESKTOP)
    window.set_decorated(False)
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_keep_below(True)
    window.set_accept_focus(False)
    window.stick()
    window.move(geo["x"], geo["y"])
    window.set_default_size(geo["width"], geo["height"])
    window.resize(geo["width"], geo["height"])

    sink = Gst.ElementFactory.make("gtksink", None)
    if sink is None:
        print("gtksink not available (install gstreamer1.0-gtk3)", file=sys.stderr)
        return None
    # fit keeps aspect; stretch/zoom fill the widget (zoom crops via oversizing)
    sink.set_property("force-aspect-ratio", mode == "scaled")

    playbin = Gst.ElementFactory.make("playbin", None)
    playbin.set_property("uri", Path(entry["path"]).resolve().as_uri())
    playbin.set_property("video-sink", sink)
    playbin.set_property("mute", not entry.get("audio", False))

    video_widget = sink.get_property("widget")

    if mode == "zoom":
        # Crop-to-fill: oversize the video widget and center it in a Fixed
        fixed = Gtk.Fixed()
        window.add(fixed)
        fixed.put(video_widget, 0, 0)
        video_widget.set_size_request(geo["width"], geo["height"])

        def on_caps(pad, *_args):
            caps = pad.get_current_caps()
            if not caps:
                return
            st = caps.get_structure(0)
            ok_w, vw = st.get_int("width")
            ok_h, vh = st.get_int("height")
            if not (ok_w and ok_h and vw and vh):
                return
            scale = max(geo["width"] / vw, geo["height"] / vh)
            w, h = int(vw * scale), int(vh * scale)

            def resize():
                video_widget.set_size_request(w, h)
                fixed.move(video_widget,
                           (geo["width"] - w) // 2, (geo["height"] - h) // 2)
                return False
            GLib.idle_add(resize)

        pad = sink.get_static_pad("sink")
        if pad:
            pad.connect("notify::caps", on_caps)
    else:
        window.add(video_widget)

    # Loop forever on end-of-stream
    bus = playbin.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, message):
        if message.type == Gst.MessageType.EOS:
            playbin.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                0,
            )
        elif message.type == Gst.MessageType.ERROR:
            err, _dbg = message.parse_error()
            print(f"gstreamer error: {err.message}", file=sys.stderr)
            Gtk.main_quit()
    bus.connect("message", on_message)

    window.show_all()
    playbin.set_state(Gst.State.PLAYING)
    return playbin


def _run_player(entries: list[dict]) -> int:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gtk, Gdk, Gst, GLib

    Gst.init(None)

    players = [p for e in entries
               if (p := _make_window(e, Gtk, Gdk, Gst, GLib)) is not None]
    if not players:
        return 1

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, Gtk.main_quit)

    Gtk.main()
    for playbin in players:
        playbin.set_state(Gst.State.NULL)
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="bglin video wallpaper windows")
    parser.add_argument("--spec", required=True,
                        help="JSON list of {path, monitor, mode, audio}")
    args = parser.parse_args()
    return _run_player(json.loads(args.spec))


if __name__ == "__main__":
    sys.exit(main())
