"""Media catalog with tag support.

Stores per-file tags in ~/.config/bglin/catalog.json. Supports auto-tagging
from file names and AND/OR/XOR filtering, mirroring LumaLoop's tag system.
"""

import json
import re
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


class Catalog:
    def __init__(self) -> None:
        self.media: dict[str, dict] = {}  # abs path -> {"tags": [...]}
        self.hidden_tags: list[str] = []

    # ---------- persistence ----------

    @classmethod
    def load(cls) -> "Catalog":
        cat = cls()
        if paths.CATALOG_FILE.exists():
            try:
                data = json.loads(paths.CATALOG_FILE.read_text())
                cat.media = data.get("media", {})
                cat.hidden_tags = data.get("hidden_tags", [])
            except json.JSONDecodeError:
                pass
        return cat

    def save(self) -> None:
        paths.ensure_dirs()
        paths.CATALOG_FILE.write_text(
            json.dumps(
                {"media": self.media, "hidden_tags": self.hidden_tags},
                indent=2,
                ensure_ascii=False,
            )
        )

    # ---------- scanning ----------

    def scan(self, folder: Path, auto_tag: bool = True) -> list[str]:
        """Sync catalog with folder contents. Returns sorted list of paths."""
        found = []
        if folder.is_dir():
            for p in sorted(folder.rglob("*")):
                if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS:
                    found.append(str(p))
        current = set(found)
        # Drop entries for files that no longer exist
        for gone in [k for k in self.media if k not in current]:
            del self.media[gone]
        # Add new files
        for path in found:
            if path not in self.media:
                tags = self.auto_tags(Path(path).stem) if auto_tag else []
                self.media[path] = {"tags": tags}
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

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for entry in self.media.values():
            tags.update(entry.get("tags", []))
        return sorted(tags)

    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.media.values():
            for tag in entry.get("tags", []):
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items()))

    def tags_of(self, path: str) -> list[str]:
        return list(self.media.get(path, {}).get("tags", []))

    def set_tags(self, path: str, tags: Iterable[str]) -> None:
        self.media.setdefault(path, {})["tags"] = sorted({t.strip().lower() for t in tags if t.strip()})
        self.save()

    def rename_tag(self, old: str, new: str) -> None:
        new = new.strip().lower()
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
        data = json.loads(src.read_text())
        by_name = data.get("media", {})
        updated = 0
        for path, entry in self.media.items():
            name = Path(path).name
            if name in by_name:
                entry["tags"] = sorted({t.lower() for t in by_name[name]})
                updated += 1
        self.save()
        return updated
