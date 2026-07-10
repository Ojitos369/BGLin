"""Slideshow engine: playlist building, ordering and navigation.

Counterpart of LumaLoop's CurrentMediaHandler — owns "what goes on which
screen and when". In "different per screen" mode every monitor keeps its own
playlist position and its own timer, so a 20-second video on one screen
never blocks the other screen's 5-second image rotation.
"""

import json
import math
import random
import time
from pathlib import Path

from . import paths, screens, videowall
from .backends import get_backend, restore_original, save_original
from .catalog import Catalog, is_video
from .compose import compose_span
from .config import Config


class Engine:
    def __init__(self, config: Config | None = None, catalog: Catalog | None = None):
        self.config = config or Config.load()
        self.catalog = catalog or Catalog.load()
        self.playlist: list[str] = []
        self.index = -1                       # position (primary monitor)
        self.paused = False
        self._mon_pos: dict[int, int] = {}    # monitor index -> playlist pos
        self._deadlines: dict = {}            # "all" | monitor index -> monotonic
        self.rebuild()
        self._restore_position()

    # ---------- playlist ----------

    def rebuild(self) -> None:
        """Rescan folder, apply tag filter and ordering."""
        files = self.catalog.scan(self.config.media_path, self.config.auto_tag_on_scan)
        if not self.config.video_enabled:
            files = [f for f in files if not is_video(f)]
        if self.config.filter_enabled and self.config.filter_tags:
            files = self.catalog.filter(files, self.config.filter_tags,
                                        self.config.filter_mode)
        if self.config.order == "shuffle":
            rng = random.Random()
            files = files[:]
            rng.shuffle(files)
        current = self.current()
        old = self.playlist
        self.playlist = files
        # Keep position on the same media if it survived the rebuild
        self.index = files.index(current) if current in files else -1
        for mon_idx, pos in list(self._mon_pos.items()):
            item = old[pos % len(old)] if old else None
            self._mon_pos[mon_idx] = files.index(item) if item in files else mon_idx

    def current(self) -> str | None:
        if 0 <= self.index < len(self.playlist):
            return self.playlist[self.index]
        return None

    # ---------- mode helpers ----------

    def _independent(self, mons) -> bool:
        return (self.config.monitor_mode == "different" and len(mons) > 1
                and bool(self.playlist))

    def _pos(self, mon_index: int) -> int:
        """This monitor's playlist position (staggered start)."""
        if mon_index not in self._mon_pos:
            base = self.index if self.index >= 0 else 0
            self._mon_pos[mon_index] = (base + mon_index) % len(self.playlist)
        return self._mon_pos[mon_index]

    def _delay_for_item(self, item: str | None) -> int:
        """Seconds this item stays on screen: at least the interval, but a
        video is never cut short (LumaLoop behavior)."""
        delay = max(5, self.config.interval_seconds)
        if item and is_video(item) and self.config.video_enabled:
            duration = videowall.video_duration(item)
            if duration:
                delay = max(delay, math.ceil(duration) + 1)
        return delay

    # ---------- navigation (manual + timer) ----------

    def next(self) -> str | None:
        return self._step(+1)

    def prev(self) -> str | None:
        return self._step(-1)

    def _step(self, direction: int) -> str | None:
        if not self.playlist:
            return None
        mons = screens.monitors()
        now = time.monotonic()
        if self._independent(mons):
            for mon in mons:
                pos = (self._pos(mon.index) + direction) % len(self.playlist)
                self._mon_pos[mon.index] = pos
                self._deadlines[mon.index] = now + self._delay_for_item(
                    self.playlist[pos])
            primary = next((m for m in mons if m.primary), mons[0])
            self.index = self._mon_pos[primary.index]
            self._render_different(mons)
            self._save_position()
            return self.playlist[self.index]
        self.index = (self.index + direction) % len(self.playlist)
        self._deadlines["all"] = now + self._delay_for_item(self.current())
        return self._apply_same(mons)

    def show(self, path: str) -> str | None:
        """Jump to a specific item (gallery double-click). In independent
        mode it lands on the primary monitor only."""
        mons = screens.monitors()
        now = time.monotonic()
        if path not in self.playlist:
            self.playlist.insert(max(self.index, 0) + 1, path)
        target = self.playlist.index(path)
        if self._independent(mons):
            primary = next((m for m in mons if m.primary), mons[0])
            self._pos(primary.index)  # ensure others initialized
            self._mon_pos[primary.index] = target
            self._deadlines[primary.index] = now + self._delay_for_item(path)
            self.index = target
            self._render_different(mons)
            self._save_position()
            return path
        self.index = target
        self._deadlines["all"] = now + self._delay_for_item(path)
        return self._apply_same(mons)

    def tick(self) -> float:
        """Advance whatever is due; return seconds until the next deadline.

        The daemon calls this in its loop. Each monitor (or the single
        "all" group) advances on its own schedule.
        """
        if not self.playlist:
            return max(5, self.config.interval_seconds)
        mons = screens.monitors()
        now = time.monotonic()
        if self._independent(mons):
            changed = []
            for mon in mons:
                if now + 0.01 >= self._deadlines.get(mon.index, 0):
                    pos = (self._pos(mon.index) + 1) % len(self.playlist)
                    self._mon_pos[mon.index] = pos
                    self._deadlines[mon.index] = now + self._delay_for_item(
                        self.playlist[pos])
                    changed.append(mon.index)
            if changed:
                primary = next((m for m in mons if m.primary), mons[0])
                self.index = self._pos(primary.index)
                self._render_different(mons, changed)
                self._save_position()
            active = [self._deadlines[m.index] for m in mons]
            return max(1.0, min(active) - now)
        if now + 0.01 >= self._deadlines.get("all", 0):
            self.index = (self.index + 1) % len(self.playlist)
            self._deadlines["all"] = now + self._delay_for_item(self.current())
            self._apply_same(mons)
        return max(1.0, self._deadlines["all"] - now)

    def reset_timers(self) -> None:
        """Restart countdowns from now (used on resume/reload)."""
        now = time.monotonic()
        for key in list(self._deadlines):
            item = (self.current() if key == "all"
                    else self.playlist[self._mon_pos.get(key, 0) % len(self.playlist)]
                    if self.playlist else None)
            self._deadlines[key] = now + self._delay_for_item(item)

    # ---------- rendering ----------

    def _apply_same(self, mons) -> str | None:
        """One item everywhere (or single monitor)."""
        media = self.current()
        if not (media and Path(media).exists()):
            return None
        backend = get_backend(self.config.backend)
        save_original(backend)  # snapshot pre-bglin wallpaper once
        if is_video(media) and self.config.video_enabled:
            # Same video on every monitor; audio (if enabled) only on one
            videowall.stop_all()
            videowall.start([
                {"path": media, "monitor": vars(m), "mode": self.config.mode,
                 "audio": self.config.video_audio and m.primary}
                for m in mons
            ], slot="all")
        else:
            videowall.stop_all()
            backend.set_wallpaper(Path(media), self.config.mode)
        self._save_position()
        return media

    def _render_different(self, mons, changed: list[int] | None = None) -> None:
        """Paint each monitor's current item. `changed` limits which video
        slots are (re)started so untouched videos keep playing."""
        backend = get_backend(self.config.backend)
        save_original(backend)
        videowall.stop("all")  # leaving same-mode: drop the grouped player
        total = len(self.playlist)
        image_assignments: dict[int, str | None] = {}
        any_image_changed = changed is None
        audio_given = False
        for mon in mons:
            item = self.playlist[self._pos(mon.index) % total]
            if is_video(item) and self.config.video_enabled:
                image_assignments[mon.index] = None  # black under the video
                if changed is None or mon.index in changed:
                    videowall.start([{
                        "path": item, "monitor": vars(mon),
                        "mode": self.config.mode,
                        "audio": self.config.video_audio and not audio_given,
                    }], slot=str(mon.index))
                audio_given = True
            else:
                image_assignments[mon.index] = item
                videowall.stop(str(mon.index))
                if changed is not None and mon.index in changed:
                    any_image_changed = True
        if any_image_changed and any(image_assignments.values()):
            canvas = compose_span(image_assignments, mons)
            backend.set_wallpaper(canvas, "spanned")

    # ---------- activate / deactivate ----------

    def deactivate(self) -> bool:
        """Stop painting and put back the pre-bglin wallpaper."""
        videowall.stop_all()
        return restore_original(get_backend(self.config.backend))

    # ---------- state persistence ----------

    def _save_position(self) -> None:
        paths.ensure_dirs()
        paths.STATE_FILE.write_text(json.dumps({
            "current": self.current(),
            "monitor_pos": {str(k): v for k, v in self._mon_pos.items()},
        }))

    def _restore_position(self) -> None:
        if paths.STATE_FILE.exists():
            try:
                data = json.loads(paths.STATE_FILE.read_text())
                current = data.get("current")
                if current in self.playlist:
                    self.index = self.playlist.index(current)
                for key, pos in (data.get("monitor_pos") or {}).items():
                    if isinstance(pos, int) and self.playlist:
                        self._mon_pos[int(key)] = pos % len(self.playlist)
            except (json.JSONDecodeError, ValueError):
                pass

    def status(self) -> dict:
        mons = screens.monitors()
        per_monitor = None
        if self._independent(mons):
            per_monitor = {
                str(m.index): self.playlist[self._pos(m.index) % len(self.playlist)]
                for m in mons
            }
        return {
            "paused": self.paused,
            "current": self.current(),
            "index": self.index,
            "total": len(self.playlist),
            "interval": self.config.interval_seconds,
            "order": self.config.order,
            "mode": self.config.mode,
            "monitor_mode": self.config.monitor_mode,
            "monitors": per_monitor,
            "filter": {
                "enabled": self.config.filter_enabled,
                "mode": self.config.filter_mode,
                "tags": self.config.filter_tags,
            },
        }
