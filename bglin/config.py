"""Configuration persisted as JSON in ~/.config/bglin/config.json."""

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

from . import catalog, paths

ORDERS = ("sequential", "shuffle")
# Display modes map to gsettings picture-options (and equivalents per backend)
MODES = ("zoom", "scaled", "stretched", "centered", "wallpaper", "spanned")
MODE_LABELS = {
    "zoom": "Fill (zoom)",
    "scaled": "Fit (scaled)",
    "stretched": "Stretch",
    "centered": "Center",
    "wallpaper": "Tile",
    "spanned": "Span monitors",
}
# LumaLoop's TagFilterMode values: the tags file travels between apps with them
FILTER_MODES = catalog.FILTER_MODES
FILTER_MODE_LABELS = {
    "and": "Has ALL selected tags (may have more)",
    "or": "Has AT LEAST ONE selected tag (may have more)",
    "xor": "Has EXACTLY ONE of the selected tags",
    "only": "Has ONLY selected tags, nothing else",
    "exact": "Has EXACTLY all selected tags and nothing else",
    "not_any": "Has NONE of the selected tags",
    "xand": "Does NOT have all of the selected tags",
    "not_only": "Excludes items with only selected tags",
    "not_exact": "Excludes items with exactly the selected tags",
}
MONITOR_MODES = {"same": "Same on all screens", "different": "Different per screen"}


@dataclass
class Config:
    media_dir: str = str(paths.DEFAULT_MEDIA_DIR)
    interval_seconds: int = 300
    order: str = "sequential"
    mode: str = "zoom"
    filter_enabled: bool = False
    filter_mode: str = "or"  # LumaLoop TagFilterMode value
    filter_tags: list = field(default_factory=list)  # LumaLoop's activeTags
    backend: str = "auto"
    auto_tag_on_scan: bool = True
    video_enabled: bool = True
    video_audio: bool = False
    monitor_mode: str = "same"  # "same" | "different"

    def save(self) -> None:
        paths.ensure_dirs()
        paths.CONFIG_FILE.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False)
        )

    @classmethod
    def load(cls) -> "Config":
        if paths.CONFIG_FILE.exists():
            try:
                data = json.loads(paths.CONFIG_FILE.read_text())
                known = {f for f in cls.__dataclass_fields__}
                cfg = cls(**{k: v for k, v in data.items() if k in known})
                # Configs written before the LumaLoop-compatible modes held
                # "OR"/"AND"/"XOR"
                cfg.filter_mode = catalog.normalize_mode(cfg.filter_mode)
                return cfg
            except (json.JSONDecodeError, TypeError):
                pass
        cfg = cls()
        cfg.save()
        return cfg

    @property
    def media_path(self) -> Path:
        return Path(self.media_dir).expanduser()
