<a name="readme-top"></a>

<div align="center">
<h3 align="center">bglin</h3>
  <p align="center">
    A slideshow wallpaper manager for Linux — <a href="https://github.com/ojitos369/LumaLoop">LumaLoop</a> for the desktop.
  </p>
</div>

## About The Project

bglin rotates your desktop wallpaper from a local folder, with the same
organization model as LumaLoop (Android): a tag catalog with auto-tagging,
AND/OR/XOR filtering, configurable intervals, sequential/shuffle playback and
multiple display modes. It ships a GTK3 gallery UI, a background daemon and a
CLI.

## Key Features

* **Tag system (LumaLoop-compatible)**
  * **Auto-tagging**: tags derived from file names (`beach_sunset-01.jpg` → `beach`, `sunset`).
  * **Nine filter modes**, the same `TagFilterMode` values LumaLoop uses:
    `and`, `or`, `xor`, `only`, `exact`, `not_any`, `xand`, `not_only`, `not_exact`.
  * **Hidden tags** (media carrying them is never shown) and **ignored tags**
    (no filter mode takes them into account), as in LumaLoop.
  * **Export/Import**: LumaLoop's tag file — the very same `tags.json` works in
    LumaLoop, bglin and VSBG (see below).
* **Mixed media**: images **and videos** as wallpaper, like the original.
  * Videos play muted in a loop via GStreamer in a desktop-layer window
    (no mpv/xwinwrap needed); optional audio toggle in Settings.
* **Slideshow**: configurable interval, sequential or shuffle order.
* **Display modes**: Fill, Fit, Stretch, Center, Tile, Span — mapped to each desktop's native options.
* **Gallery UI (GTK3)**: thumbnail grid with tag chips; double-click sets the wallpaper, right-click edits tags.
* **Daemon + CLI**: unix-socket control (`next`, `prev`, `pause`, `status`, …) — bind keys to it from your DE.
* **Multi-monitor**: same wallpaper on every screen, or a different one per
  screen (images are stitched into a spanned canvas; videos get one window
  per monitor).
* **Master on/off**: turning the slideshow off restores the wallpaper you
  had before bglin (`bglin on` / `bglin off`, or the switch in the GUI).
* **Multi-desktop backends** (auto-detected): Cinnamon, GNOME/Budgie/Unity, MATE, XFCE, KDE Plasma, swww (Wayland WMs), feh (X11 WMs).

> Video wallpapers render as undecorated desktop-type windows
> (komorebi-style), one per monitor. On some setups they may cover
> desktop icons while playing.

## Requirements

* Python ≥ 3.10
* PyGObject + GTK3 (`python3-gi`, `gir1.2-gtk-3.0` — preinstalled on Mint/Cinnamon)
* Pillow (`python3-pil`)
* For videos: GStreamer with `gtksink` (`gir1.2-gst-plugins-base-1.0`,
  `gstreamer1.0-gtk3` — preinstalled on Mint) and `ffmpegthumbnailer` or
  `ffmpeg` for gallery thumbnails

## Install

```sh
./install.sh
```

Then:

```sh
# Drop images here (or change the folder in Settings):
cp ~/my-wallpapers/*.jpg ~/Pictures/bglin/

# Open the gallery / settings
bglin gui

# Start the slideshow daemon (autostart on login):
systemctl --user enable --now bglin.service
```

## CLI

```sh
bglin on          # activate: start the slideshow
bglin off         # deactivate: restore the original wallpaper
bglin next        # next wallpaper
bglin prev        # previous wallpaper
bglin pause       # pause the slideshow
bglin resume      # resume
bglin status      # JSON status
bglin reload      # reload config + rescan folder
bglin show FILE   # set a specific image
bglin stop        # stop the daemon
```

`next`/`prev`/`show` also work without the daemon (one-shot mode).

## Architecture

| LumaLoop (Android)            | bglin (Linux)                              |
|-------------------------------|--------------------------------------------|
| `SlideshowWallpaperService`   | `daemon.py` (systemd user service)         |
| `CurrentMediaHandler`         | `engine.py` (playlist, ordering, position) |
| OpenGL renderer (images)      | `backends.py` (native desktop APIs)        |
| ExoPlayer (videos)            | `videowall.py` (GStreamer desktop window)  |
| Jetpack Compose UI            | `gui.py` (GTK3)                            |
| Tag catalog + backup          | `catalog.py` (JSON in `~/.config/bglin`)   |

State lives in XDG dirs: config/catalog in `~/.config/bglin/`, thumbnail
cache in `~/.cache/bglin/`, playback position in `~/.local/state/bglin/`.

## Shared tag file (LumaLoop / bglin / VSBG)

**Tag catalog → Export/Import JSON…** reads and writes LumaLoop's tag file, so
one file can carry your tags across the three apps:

```json
{
  "catalog": ["beach", "nsfw", "sunset"],
  "mappings": { "beach sunset.jpg": ["beach", "sunset"] },
  "activeTags": ["sunset"],
  "hiddenTags": ["nsfw"],
  "ignoredFilterTags": ["4k"],
  "tagFilterMode": "or",
  "autoTagEnabled": true
}
```

Entries are keyed by **file name**, so the same file works even when each device
keeps its media elsewhere. Importing **merges** the incoming tags into whatever a
file already has and restores the filter state; local tags are kept by absolute
path in `~/.config/bglin/catalog.json`.

## Project License

Distributed under GPL v3, same as LumaLoop.
