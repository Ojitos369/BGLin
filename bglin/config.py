"""Configuration persisted as JSON in ~/.config/bglin/config.json."""

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

from . import paths

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
FILTER_MODES = ("OR", "AND", "XOR")
MONITOR_MODES = {"same": "Same on all screens", "different": "Different per screen"}


@dataclass
class Config:
    media_dir: str = str(paths.DEFAULT_MEDIA_DIR)
    interval_seconds: int = 300
    order: str = "sequential"
    mode: str = "zoom"
    filter_enabled: bool = False
    filter_mode: str = "OR"
    filter_tags: list = field(default_factory=list)
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
                return cls(**{k: v for k, v in data.items() if k in known})
            except (json.JSONDecodeError, TypeError):
                pass
        cfg = cls()
        cfg.save()
        return cfg

    @property
    def media_path(self) -> Path:
        return Path(self.media_dir).expanduser()
