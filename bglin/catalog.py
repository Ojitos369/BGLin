"""Media catalog with tag support.

Stores per-file tags in ~/.config/bglin/catalog.json. Supports auto-tagging
from file names and AND/OR/XOR filtering, mirroring LumaLoop's tag system.

The GUI and the daemon each hold a Catalog over the same file, so every
mutation re-reads the file first (cheap: an mtime stat) and writes through an
atomic replace. Without that, whichever process saved last would overwrite the
other's tags with its stale in-memory copy.
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

from . import paths

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS

# Tokens produced by cameras/screenshots that make useless tags
_TOKEN_RE = re.compile(r"[a-zA-ZáéíóúñÁÉÍÓÚÑüÜ]{3,}")
_STOP_TOKENS = {"img", "image", "photo", "foto", "pic", "screenshot", "wallpaper", "background"}


def normalize_tag(tag: str) -> str:
    return " ".join(tag.strip().lower().split())


class Catalog:
    def __init__(self) -> None:
        self.media: dict[str, dict] = {}  # abs path -> {"tags": [...]}
        self.hidden_tags: list[str] = []
        # Paths found by the last scan. Entries for files outside this set stay
        # in self.media so their tags survive a folder change or a moved file.
        self.present: list[str] = []
        self._mtime: float = -1.0

    # ---------- persistence ----------

    @classmethod
    def load(cls) -> "Catalog":
        cat = cls()
        cat._read_file()
        cat.present = sorted(cat.media)
        return cat

    def _read_file(self) -> bool:
        """Load catalog.json into memory. False if it is missing/unreadable."""
        try:
            stat = paths.CATALOG_FILE.stat()
            data = json.loads(paths.CATALOG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        media = data.get("media", {})
        if isinstance(media, dict):
            self.media = {
                k: {"tags": sorted({normalize_tag(t) for t in v.get("tags", []) if normalize_tag(t)})}
                for k, v in media.items()
                if isinstance(v, dict)
            }
        self.hidden_tags = data.get("hidden_tags", [])
        self._mtime = stat.st_mtime
        return True

    def reload_if_changed(self) -> bool:
        """Re-read the file when another process (daemon/GUI) has written it."""
        try:
            mtime = paths.CATALOG_FILE.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        return self._read_file()

    def save(self) -> None:
        paths.ensure_dirs()
        payload = json.dumps(
            {"media": self.media, "hidden_tags": self.hidden_tags},
            indent=2,
            ensure_ascii=False,
        )
        # Atomic: a crash or a concurrent reader never sees a half-written file
        fd, tmp = tempfile.mkstemp(dir=str(paths.CATALOG_FILE.parent),
                                   prefix=".catalog-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
            os.replace(tmp, paths.CATALOG_FILE)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise
        try:
            self._mtime = paths.CATALOG_FILE.stat().st_mtime
        except OSError:
            self._mtime = -1.0

    # ---------- scanning ----------

    def scan(self, folder: Path, auto_tag: bool = True) -> list[str]:
        """Sync catalog with folder contents. Returns sorted list of paths.

        Tags of files that are not in the folder right now are kept (a moved
        or temporarily unplugged file must not lose its tags), they are just
        left out of the returned list and of the tag counts.
        """
        self.reload_if_changed()
        found = []
        if folder.is_dir():
            for p in sorted(folder.rglob("*")):
                if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS:
                    found.append(str(p))
        dirty = False
        for path in found:
            if path not in self.media:
                tags = self.auto_tags(Path(path).stem) if auto_tag else []
                self.media[path] = {"tags": tags}
                dirty = True
        self.present = found
        if dirty:
            self.save()
        return found

    @staticmethod
    def auto_tags(stem: str) -> list[str]:
        """Derive tags from a file name, LumaLoop-style.

        "beach_sunset-01" -> ["beach", "sunset"]
        """
        tokens = [t.lower() for t in _TOKEN_RE.findall(stem)]
        return sorted({t for t in tokens if t not in _STOP_TOKENS})

    # ---------- tags ----------

    def _entries(self) -> Iterable[dict]:
        """Entries of the files currently in the library."""
        if not self.present:
            return self.media.values()
        return (self.media[p] for p in self.present if p in self.media)

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for entry in self._entries():
            tags.update(entry.get("tags", []))
        return sorted(tags)

    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._entries():
            for tag in entry.get("tags", []):
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items()))

    def tags_of(self, path: str) -> list[str]:
        return list(self.media.get(path, {}).get("tags", []))

    def set_tags(self, path: str, tags: Iterable[str], save: bool = True) -> None:
        if save:
            self.reload_if_changed()
        clean = sorted({normalize_tag(t) for t in tags if normalize_tag(t)})
        self.media.setdefault(path, {})["tags"] = clean
        if save:
            self.save()

    def set_tags_many(self, updates: dict[str, Iterable[str]]) -> None:
        """Apply several files' tags in one read-modify-write cycle."""
        self.reload_if_changed()
        for path, tags in updates.items():
            self.set_tags(path, tags, save=False)
        self.save()

    def add_tags(self, paths_list: Iterable[str], tags: Iterable[str]) -> None:
        self.reload_if_changed()
        new = {normalize_tag(t) for t in tags if normalize_tag(t)}
        for path in paths_list:
            self.set_tags(path, set(self.tags_of(path)) | new, save=False)
        self.save()

    def remove_tags(self, paths_list: Iterable[str], tags: Iterable[str]) -> None:
        self.reload_if_changed()
        drop = {normalize_tag(t) for t in tags if normalize_tag(t)}
        for path in paths_list:
            self.set_tags(path, set(self.tags_of(path)) - drop, save=False)
        self.save()

    def rename_tag(self, old: str, new: str) -> None:
        self.reload_if_changed()
        new = normalize_tag(new)
        for entry in self.media.values():
            tags = set(entry.get("tags", []))
            if old in tags:
                tags.discard(old)
                if new:
                    tags.add(new)
                entry["tags"] = sorted(tags)
        self.save()

    def delete_tag(self, tag: str) -> None:
        self.rename_tag(tag, "")

    # ---------- filtering ----------

    def filter(self, paths_list: list[str], tags: list[str], mode: str) -> list[str]:
        """Filter paths by tags with OR/AND/XOR logic (LumaLoop semantics)."""
        if not tags:
            return paths_list
        wanted = set(tags)
        out = []
        for p in paths_list:
            have = set(self.tags_of(p))
            hits = len(wanted & have)
            ok = (
                hits > 0 if mode == "OR"
                else hits == len(wanted) if mode == "AND"
                else hits == 1  # XOR: exactly one of the selected tags
            )
            if ok:
                out.append(p)
        return out

    # ---------- backup (export/import like LumaLoop's tag catalog) ----------

    def export_json(self, dest: Path) -> None:
        dest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "media": {Path(k).name: v.get("tags", []) for k, v in self.media.items()},
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    def import_json(self, src: Path) -> int:
        """Match backup entries by file name. Returns count of updated files."""
        self.reload_if_changed()
        data = json.loads(src.read_text())
        by_name = data.get("media", {})
        updated = 0
        for path, entry in self.media.items():
            name = Path(path).name
            if name in by_name:
                entry["tags"] = sorted({normalize_tag(t) for t in by_name[name]
                                        if normalize_tag(t)})
                updated += 1
        self.save()
        return updated
