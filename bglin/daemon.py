"""Background slideshow daemon with a unix-socket control interface.

Run with `bglin daemon`. Control it with `bglin next|prev|pause|resume|
status|reload|stop`, or from the GUI.

Protocol: one JSON line per request/response over the socket.
"""

import json
import os
import socket
import threading
import time
from pathlib import Path

from . import paths, videowall
from .config import Config
from .engine import Engine


class Daemon:
    def __init__(self) -> None:
        self.engine = Engine()
        self._wake = threading.Event()
        self._stop = threading.Event()

    # ---------- control server ----------

    def handle(self, command: dict) -> dict:
        action = command.get("action")
        eng = self.engine
        if action == "next":
            result = eng.next()
            self._wake.set()  # recompute the sleep against fresh deadlines
            return {"ok": True, "current": result}
        if action == "prev":
            result = eng.prev()
            self._wake.set()
            return {"ok": True, "current": result}
        if action == "show":
            result = eng.show(command.get("path", ""))
            self._wake.set()
            return {"ok": True, "current": result}
        if action == "pause":
            eng.paused = True
            self._wake.set()
            return {"ok": True}
        if action == "resume":
            eng.paused = False
            eng.reset_timers()  # don't fast-forward everything that was due
            self._wake.set()
            return {"ok": True}
        if action == "reload":
            eng.config = Config.load()
            eng.rebuild()
            eng.reset_timers()
            self._wake.set()  # re-read interval immediately
            return {"ok": True, "total": len(eng.playlist)}
        if action == "status":
            return {"ok": True, **eng.status()}
        if action == "stop":
            self._stop.set()
            self._wake.set()
            return {"ok": True}
        if action == "off":
            # Stop and put the original (pre-bglin) wallpaper back
            restored = eng.deactivate()
            self._stop.set()
            self._wake.set()
            return {"ok": True, "restored": restored}
        return {"ok": False, "error": f"unknown action: {action}"}

    def _serve(self) -> None:
        if paths.SOCKET_PATH.exists():
            paths.SOCKET_PATH.unlink()
        paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(paths.SOCKET_PATH))
        os.chmod(paths.SOCKET_PATH, 0o600)
        server.listen(4)
        server.settimeout(1.0)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    try:
                        data = conn.makefile().readline()
                        request = json.loads(data) if data.strip() else {}
                        response = self.handle(request)
                    except (json.JSONDecodeError, OSError) as exc:
                        response = {"ok": False, "error": str(exc)}
                    try:
                        conn.sendall((json.dumps(response) + "\n").encode())
                    except OSError:
                        pass
        finally:
            server.close()
            if paths.SOCKET_PATH.exists():
                paths.SOCKET_PATH.unlink()

    # ---------- main loop ----------

    def run(self) -> None:
        server_thread = threading.Thread(target=self._serve, daemon=True)
        server_thread.start()
        # Paint something immediately on start (also arms the timers)
        try:
            if self.engine.current() is None:
                self.engine.next()
            else:
                self.engine.show(self.engine.current())
        except Exception as exc:
            print(f"[bglin] wallpaper error: {exc}", flush=True)
        while not self._stop.is_set():
            if self.engine.paused:
                self._wake.wait(timeout=3600)
                self._wake.clear()
                continue
            try:
                # Advances whatever screen is due; returns time to next change
                delay = self.engine.tick()
            except Exception as exc:  # backend hiccup: log, keep looping
                print(f"[bglin] wallpaper error: {exc}", flush=True)
                delay = 5
            self._wake.wait(timeout=delay)
            self._wake.clear()
        videowall.stop_all()  # don't leave orphan video windows behind
        server_thread.join(timeout=3)


# ---------- client side ----------

def send(command: dict, timeout: float = 5.0) -> dict | None:
    """Send a command to a running daemon. None if daemon not running."""
    if not paths.SOCKET_PATH.exists():
        return None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(str(paths.SOCKET_PATH))
        client.sendall((json.dumps(command) + "\n").encode())
        response = client.makefile().readline()
        client.close()
        return json.loads(response)
    except (OSError, json.JSONDecodeError):
        return None


def is_running() -> bool:
    return send({"action": "status"}, timeout=2.0) is not None


def spawn_detached() -> None:
    """Start the daemon as a detached background process."""
    import subprocess
    import sys
    env = dict(os.environ)
    pkg_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.Popen(
        [sys.executable, "-m", "bglin", "daemon"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
