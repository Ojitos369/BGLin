"""Media catalog with tag support.

Stores per-file tags in ~/.config/bglin/catalog.json. Supports auto-tagging
from file names and the nine filter modes of LumaLoop's tag system.

Import/export use LumaLoop's tag file format (see TagExportData in the LumaLoop
app), so a single tags file can be moved between LumaLoop, bglin and VSBG. In
that format entries are keyed by file name; the local catalog keys by absolute
path, and the two are translated on the way in and out.

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


def _clean_tags(tags: Iterable[str]) -> list[str]:
    return sorted({normalize_tag(t) for t in tags or () if normalize_tag(t)})


# LumaLoop's TagFilterMode values, as they travel in the tags file
FILTER_MODES = ("and", "or", "xor", "only", "exact",
                "not_any", "xand", "not_only", "not_exact")
_LEGACY_MODES = {"OR": "or", "AND": "and", "XOR": "xor"}


def normalize_mode(mode: str) -> str:
    mode = _LEGACY_MODES.get(mode, (mode or "").lower())
    return mode if mode in FILTER_MODES else "or"


def matches_tag_filter(item_tags, active_tags, ignored_tags, mode: str) -> bool:
    """LumaLoop's SharedPreferencesManager.matchesTagFilter, port for port."""
    active = set(active_tags) - set(ignored_tags or ())
    # Nothing left to filter by once the ignored tags are gone: keep the item
    if not active:
        return True
    tags = set(item_tags) - set(ignored_tags or ())

    hits = len(active & tags)
    has_any = hits > 0
    has_all = active <= tags
    # every tag on the item is a selected one, and the item has at least one
    only_selected = bool(tags) and tags <= active
    exactly_all = has_all and tags <= active

    return {
        "and": has_all,
        "or": has_any,
        "xor": hits == 1,
        "only": only_selected,
        "exact": exactly_all,
        "not_any": not has_any,
        "xand": not has_all,
        "not_only": not only_selected,
        "not_exact": not exactly_all,
    }[normalize_mode(mode)]


class Catalog:
    def __init__(self) -> None:
        self.media: dict[str, dict] = {}  # abs path -> {"tags": [...]}
        # Master tag list: tags survive here even when no file carries them
        self.catalog: list[str] = []
        # Media carrying a hidden tag is never shown (LumaLoop's hiddenTags)
        self.hidden_tags: list[str] = []
        # Tags no filter mode takes into account (LumaLoop's ignoredFilterTags)
        self.ignored_filter_tags: list[str] = []
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
        self.hidden_tags = _clean_tags(data.get("hidden_tags", []))
        self.ignored_filter_tags = _clean_tags(data.get("ignored_filter_tags", []))
        self.catalog = _clean_tags(data.get("catalog", []))
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
            {
                "version": 2,
                "media": self.media,
                "catalog": self.catalog,
                "hidden_tags": self.hidden_tags,
                "ignored_filter_tags": self.ignored_filter_tags,
            },
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
                self.add_to_catalog(tags, save=False)
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
        """Master tag list: catalog tags plus the ones in use (LumaLoop's)."""
        tags: set[str] = set(self.catalog)
        for entry in self._entries():
            tags.update(entry.get("tags", []))
        return sorted(tags)

    def tag_counts(self) -> dict[str, int]:
        """Files per tag. Catalog tags with no files count 0, as in LumaLoop."""
        counts: dict[str, int] = {t: 0 for t in self.catalog}
        for entry in self._entries():
            for tag in entry.get("tags", []):
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items()))

    def add_to_catalog(self, tags: Iterable[str], save: bool = True) -> None:
        new = set(self.catalog) | set(_clean_tags(tags))
        if new == set(self.catalog):
            return
        self.catalog = sorted(new)
        if save:
            self.save()

    def tags_of(self, path: str) -> list[str]:
        return list(self.media.get(path, {}).get("tags", []))

    def set_tags(self, path: str, tags: Iterable[str], save: bool = True) -> None:
        if save:
            self.reload_if_changed()
        clean = _clean_tags(tags)
        self.media.setdefault(path, {})["tags"] = clean
        self.add_to_catalog(clean, save=False)
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
        for name in ("catalog", "hidden_tags", "ignored_filter_tags"):
            tags = set(getattr(self, name))
            if old in tags:
                tags.discard(old)
                if new:
                    tags.add(new)
                setattr(self, name, sorted(tags))
        self.save()

    def delete_tag(self, tag: str) -> None:
        self.rename_tag(tag, "")

    def set_hidden_tags(self, tags: Iterable[str]) -> None:
        self.reload_if_changed()
        self.hidden_tags = _clean_tags(tags)
        self.save()

    def set_ignored_filter_tags(self, tags: Iterable[str]) -> None:
        self.reload_if_changed()
        self.ignored_filter_tags = _clean_tags(tags)
        self.save()

    # ---------- filtering ----------

    def is_hidden(self, path: str) -> bool:
        """A file carrying any hidden tag is never shown (LumaLoop's rule)."""
        return bool(set(self.tags_of(path)) & set(self.hidden_tags))

    def filter(self, paths_list: list[str], tags: list[str], mode: str) -> list[str]:
        """Drop hidden media, then apply the selected LumaLoop filter mode."""
        active = _clean_tags(tags)
        return [
            p for p in paths_list
            if not self.is_hidden(p)
            and (not active or matches_tag_filter(self.tags_of(p), active,
                                                  self.ignored_filter_tags, mode))
        ]

    # ---------- backup (LumaLoop tag file format) ----------

    def export_json(self, dest: Path, active_tags: Iterable[str] = (),
                    filter_mode: str = "or", auto_tag: bool = True) -> None:
        """Write the tags file LumaLoop and VSBG read (keys are file names)."""
        mappings: dict[str, list[str]] = {}
        for path, entry in self.media.items():
            tags = entry.get("tags", [])
            if tags:
                mappings.setdefault(Path(path).name, [])
                mappings[Path(path).name] = sorted(
                    set(mappings[Path(path).name]) | set(tags))
        dest.write_text(
            json.dumps(
                {
                    "catalog": self.all_tags(),
                    "mappings": mappings,
                    "activeTags": _clean_tags(active_tags),
                    "hiddenTags": self.hidden_tags,
                    "ignoredFilterTags": self.ignored_filter_tags,
                    "tagFilterMode": normalize_mode(filter_mode),
                    "autoTagEnabled": bool(auto_tag),
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    def import_json(self, src: Path) -> tuple[int, dict]:
        """Read a LumaLoop tags file. Returns (files updated, settings).

        Entries are matched by file name; tags are merged into what a file
        already has, as LumaLoop's importTagData does. The settings dict carries
        the fields that live in bglin's config (active tags, mode, auto-tag) so
        the caller can persist them.
        """
        self.reload_if_changed()
        data = json.loads(src.read_text())

        by_name: dict[str, list[str]] = {}
        for key, tags in (data.get("mappings") or {}).items():
            # Old backups (and LumaLoop's own) may key by URI or absolute path
            name = Path(str(key).split("://")[-1]).name
            by_name.setdefault(name, [])
            by_name[name] = sorted(set(by_name[name]) | set(_clean_tags(tags)))

        updated = 0
        for path, entry in self.media.items():
            incoming = by_name.get(Path(path).name)
            if incoming:
                merged = _clean_tags(set(entry.get("tags", [])) | set(incoming))
                if merged != entry.get("tags", []):
                    updated += 1
                entry["tags"] = merged

        self.add_to_catalog(
            set(data.get("catalog") or ()) | {t for v in by_name.values() for t in v},
            save=False)
        if data.get("hiddenTags") is not None:
            self.hidden_tags = _clean_tags(data["hiddenTags"])
        if data.get("ignoredFilterTags") is not None:
            self.ignored_filter_tags = _clean_tags(data["ignoredFilterTags"])
        self.save()

        settings = {}
        if data.get("activeTags") is not None:
            settings["filter_tags"] = _clean_tags(data["activeTags"])
        if data.get("tagFilterMode") is not None:
            settings["filter_mode"] = normalize_mode(data["tagFilterMode"])
        if data.get("autoTagEnabled") is not None:
            settings["auto_tag_on_scan"] = bool(data["autoTagEnabled"])
        return updated, settings
