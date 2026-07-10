"""CLI entry point: python3 -m bglin <command>."""

import argparse
import json
import sys

from . import __version__, daemon, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bglin",
        description="Slideshow wallpaper manager for Linux (LumaLoop-inspired).",
    )
    parser.add_argument("--version", action="version", version=f"bglin {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="open the gallery/settings window (default)")
    sub.add_parser("daemon", help="run the slideshow daemon in the foreground")
    sub.add_parser("next", help="switch to the next wallpaper")
    sub.add_parser("prev", help="switch to the previous wallpaper")
    sub.add_parser("pause", help="pause the slideshow")
    sub.add_parser("resume", help="resume the slideshow")
    sub.add_parser("status", help="print daemon status as JSON")
    sub.add_parser("reload", help="reload config and rescan the folder")
    sub.add_parser("stop", help="stop the daemon")
    sub.add_parser("on", help="activate: start the slideshow daemon")
    sub.add_parser("off", help="deactivate: stop and restore the original wallpaper")
    show = sub.add_parser("show", help="set a specific image as wallpaper")
    show.add_argument("path")

    args = parser.parse_args(argv)
    command = args.command or "gui"
    paths.ensure_dirs()

    if command == "gui":
        from .gui import main as gui_main
        return gui_main()

    if command == "daemon":
        if daemon.is_running():
            print("bglin daemon is already running.", file=sys.stderr)
            return 1
        daemon.Daemon().run()
        return 0

    if command == "on":
        if daemon.is_running():
            daemon.send({"action": "resume"})
            print("daemon already running; slideshow resumed")
        else:
            daemon.spawn_detached()
            print("daemon started")
        return 0

    if command == "off":
        response = daemon.send({"action": "off"})
        if response is None:  # no daemon: clean up directly
            from .engine import Engine
            restored = Engine().deactivate()
            print("original wallpaper restored" if restored
                  else "nothing to restore")
        else:
            print("slideshow stopped, original wallpaper restored")
        return 0

    # One-shot commands: try the daemon first, fall back to a direct engine
    payload = {"action": command}
    if command == "show":
        import os
        payload["path"] = os.path.abspath(args.path)
    response = daemon.send(payload)
    if response is None:
        if command in ("next", "prev", "show"):
            from .engine import Engine
            engine = Engine()
            if command == "next":
                result = engine.next()
            elif command == "prev":
                result = engine.prev()
            else:
                result = engine.show(payload["path"])
            print(result or "no images in playlist")
            return 0 if result else 1
        if command == "stop":
            from . import videowall
            if videowall.is_playing():
                videowall.stop_all()
                print("stopped orphan video wallpaper")
                return 0
        print("bglin daemon is not running.", file=sys.stderr)
        return 1
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
