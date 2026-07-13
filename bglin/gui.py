"""GTK3 gallery and settings UI for bglin.

Layout mirrors LumaLoop's Compose app: a thumbnail gallery with tag chips
and filtering, playback controls in the header bar, and a settings dialog.
"""

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GdkPixbuf, GLib, Gdk, Gio  # noqa: E402

import subprocess
from pathlib import Path

from . import daemon
from .catalog import Catalog, is_video
from .config import (Config, MODES, MODE_LABELS, MONITOR_MODES, ORDERS,
                     FILTER_MODES, FILTER_MODE_LABELS)
from .engine import Engine
from .thumbs import thumbnail_for

CSS = b"""
.tag-chip { border-radius: 14px; padding: 2px 10px; min-height: 24px;
            margin: 1px; }
flowboxchild { padding: 0; }
.thumb-frame { border-radius: 6px; border: 3px solid transparent; }
.current-thumb { border: 3px solid @theme_selected_bg_color; }
.selected-thumb { border: 3px solid @theme_selected_bg_color;
                  background-color: @theme_selected_bg_color; }
.selected-thumb-label { color: @theme_selected_bg_color; font-weight: bold; }
.check-badge { background-color: @theme_selected_bg_color;
               color: @theme_selected_fg_color; border-radius: 0 0 0 6px;
               padding: 1px 6px; font-weight: bold; }
.video-badge { background-color: rgba(0,0,0,0.65); color: white;
               border-radius: 0 0 6px 0; padding: 1px 6px; font-size: 0.8em; }
.selection-bar { background-color: @theme_bg_color; }
"""


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="bglin")
        self.set_default_size(1100, 720)
        self.config = Config.load()
        self.catalog = Catalog.load()
        # Local engine only used when no daemon is running
        self._engine: Engine | None = None
        self._thumb_widgets: dict[str, Gtk.Widget] = {}
        # Selection state (multi-select for bulk tagging)
        self._frames: dict[str, Gtk.Frame] = {}
        self._checks: dict[str, Gtk.Widget] = {}
        self._tag_labels: dict[str, Gtk.Label] = {}
        self._visible: list[str] = []      # paths in display order
        self._selected: set[str] = set()
        self._anchor: str | None = None    # shift-click range origin

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._build_header()
        self._build_body()
        self.connect("key-press-event", self._on_key_press)
        self.refresh_gallery()
        GLib.timeout_add_seconds(3, self._poll_status)

    # ---------- daemon / engine bridge ----------

    def _send(self, action: str, **kw) -> dict | None:
        """Prefer the daemon; fall back to an in-process engine."""
        response = daemon.send({"action": action, **kw})
        if response is not None:
            return response
        if self._engine is None:
            self._engine = Engine(self.config, self.catalog)
        eng = self._engine
        if action == "next":
            return {"ok": True, "current": eng.next()}
        if action == "prev":
            return {"ok": True, "current": eng.prev()}
        if action == "show":
            return {"ok": True, "current": eng.show(kw.get("path", ""))}
        if action == "reload":
            eng.config = self.config
            eng.rebuild()
            return {"ok": True}
        if action == "status":
            return {"ok": True, **eng.status()}
        return {"ok": False}

    # ---------- header ----------

    def _build_header(self) -> None:
        header = Gtk.HeaderBar(show_close_button=True, title="bglin")
        header.set_subtitle("Slideshow wallpaper")
        self.set_titlebar(header)
        self.header = header

        # Master switch: ON = slideshow daemon active, OFF = restore default
        self.master_switch = Gtk.Switch(
            valign=Gtk.Align.CENTER,
            tooltip_text="Activate slideshow / restore default wallpaper")
        self.master_switch.set_active(daemon.is_running())
        self._master_handler = self.master_switch.connect(
            "notify::active", self._on_master_toggled)
        header.pack_start(self.master_switch)

        prev_btn = Gtk.Button.new_from_icon_name(
            "media-skip-backward-symbolic", Gtk.IconSize.BUTTON)
        prev_btn.set_tooltip_text("Previous wallpaper")
        prev_btn.connect("clicked", lambda *_: self._nav("prev"))
        header.pack_start(prev_btn)

        self.play_btn = Gtk.Button.new_from_icon_name(
            "media-playback-pause-symbolic", Gtk.IconSize.BUTTON)
        self.play_btn.set_tooltip_text("Pause/resume slideshow (daemon)")
        self.play_btn.connect("clicked", self._on_play_pause)
        header.pack_start(self.play_btn)

        next_btn = Gtk.Button.new_from_icon_name(
            "media-skip-forward-symbolic", Gtk.IconSize.BUTTON)
        next_btn.set_tooltip_text("Next wallpaper")
        next_btn.connect("clicked", lambda *_: self._nav("next"))
        header.pack_start(next_btn)

        settings_btn = Gtk.Button.new_from_icon_name(
            "preferences-system-symbolic", Gtk.IconSize.BUTTON)
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_settings)
        header.pack_end(settings_btn)

        tags_btn = Gtk.Button.new_from_icon_name(
            "user-bookmarks-symbolic", Gtk.IconSize.BUTTON)
        tags_btn.set_tooltip_text("Tag catalog (export/import)")
        tags_btn.connect("clicked", self._on_tag_catalog)
        header.pack_end(tags_btn)

        refresh_btn = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh_btn.set_tooltip_text("Rescan folder")
        refresh_btn.connect("clicked", lambda *_: self.refresh_gallery(rescan=True))
        header.pack_end(refresh_btn)

        folder_btn = Gtk.Button.new_from_icon_name(
            "folder-open-symbolic", Gtk.IconSize.BUTTON)
        folder_btn.set_tooltip_text("Open wallpaper folder")
        folder_btn.connect("clicked",
                           lambda *_: self._open_folder(self.config.media_dir))
        header.pack_end(folder_btn)

    # ---------- body ----------

    def _build_body(self) -> None:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)

        # Tag filter bar
        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        filter_bar.set_margin_start(12)
        filter_bar.set_margin_end(12)
        filter_bar.set_margin_top(8)
        filter_bar.set_margin_bottom(4)

        self.filter_switch = Gtk.Switch(tooltip_text="Enable tag filter")
        self.filter_switch.set_active(self.config.filter_enabled)
        self.filter_switch.connect("notify::active", self._on_filter_toggled)
        filter_bar.pack_start(Gtk.Label(label="Filter:"), False, False, 0)
        filter_bar.pack_start(self.filter_switch, False, False, 0)

        self.filter_mode_combo = Gtk.ComboBoxText(
            tooltip_text="Tag filter mode (LumaLoop)")
        for m in FILTER_MODES:
            self.filter_mode_combo.append(m, FILTER_MODE_LABELS[m])
        self.filter_mode_combo.set_active_id(self.config.filter_mode)
        self.filter_mode_combo.connect("changed", self._on_filter_mode)
        filter_bar.pack_start(self.filter_mode_combo, False, False, 0)

        chips_scroll = Gtk.ScrolledWindow(hexpand=True)
        chips_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.chips_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        chips_scroll.add(self.chips_box)
        filter_bar.pack_start(chips_scroll, True, True, 0)

        vbox.pack_start(filter_bar, False, False, 0)

        # Gallery
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.flow = Gtk.FlowBox()
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_max_children_per_line(8)
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_margin_start(8)
        self.flow.set_margin_end(8)
        scroll.add(self.flow)
        vbox.pack_start(scroll, True, True, 0)

        # Selection action bar: only visible while something is selected
        self.selection_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.get_style_context().add_class("selection-bar")
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        self.selection_label = Gtk.Label(xalign=0)
        bar.pack_start(self.selection_label, False, False, 0)

        clear_btn = Gtk.Button(label="Clear selection")
        clear_btn.connect("clicked", lambda *_: self._clear_selection())
        bar.pack_end(clear_btn, False, False, 0)

        all_btn = Gtk.Button(label="Select all")
        all_btn.connect("clicked", lambda *_: self._select_all())
        bar.pack_end(all_btn, False, False, 0)

        tag_btn = Gtk.Button(label="Edit tags…")
        tag_btn.get_style_context().add_class("suggested-action")
        tag_btn.connect("clicked",
                        lambda *_: self._edit_tags_dialog(self._selection_list()))
        bar.pack_end(tag_btn, False, False, 0)

        self.selection_revealer.add(bar)
        vbox.pack_start(self.selection_revealer, False, False, 0)

        # Status bar
        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_margin_start(12)
        self.status_label.set_margin_top(4)
        self.status_label.set_margin_bottom(4)
        vbox.pack_start(self.status_label, False, False, 0)

    # ---------- gallery ----------

    def refresh_gallery(self, rescan: bool = True) -> None:
        if rescan:
            self.catalog.scan(self.config.media_path, self.config.auto_tag_on_scan)
        else:
            # The daemon may have touched catalog.json since we last read it
            self.catalog.reload_if_changed()
        files = list(self.catalog.present)
        active = self.config.filter_tags if self.config.filter_enabled else []
        files = self.catalog.filter(files, active, self.config.filter_mode)
        for child in self.flow.get_children():
            self.flow.remove(child)
        self._thumb_widgets.clear()
        self._frames.clear()
        self._checks.clear()
        self._tag_labels.clear()
        self._visible = files
        # Drop selected items that the filter/rescan took off screen
        self._selected &= set(files)
        if self._anchor not in self._selected:
            self._anchor = None
        self._rebuild_chips()
        if not files:
            placeholder = Gtk.Label(
                label=f"No images found.\nDrop wallpapers into:\n{self.config.media_dir}")
            placeholder.set_justify(Gtk.Justification.CENTER)
            placeholder.set_margin_top(60)
            self.flow.add(placeholder)
            self.flow.show_all()
        else:
            for path in files:
                self.flow.add(self._make_thumb_placeholder(path))
            self.flow.show_all()
            threading.Thread(target=self._load_thumbs, args=(files,),
                             daemon=True).start()
        count = len(files)
        self.status_label.set_text(f"{count} image{'s' if count != 1 else ''}"
                                   f" — {self.config.media_dir}")
        self._update_selection_ui()
        self._send("reload")

    def _make_thumb_placeholder(self, path: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        frame = Gtk.Frame()
        frame.get_style_context().add_class("thumb-frame")
        img = Gtk.Image.new_from_icon_name("image-loading", Gtk.IconSize.DIALOG)
        img.set_size_request(200, 130)

        overlay = Gtk.Overlay()
        overlay.add(img)
        if is_video(path):
            badge = Gtk.Label(label=" ▶ VIDEO ")
            badge.set_halign(Gtk.Align.START)
            badge.set_valign(Gtk.Align.START)
            badge.get_style_context().add_class("video-badge")
            overlay.add_overlay(badge)
        # Selection tick, shown only while the item is selected
        check = Gtk.Label(label=" ✓ ")
        check.set_halign(Gtk.Align.END)
        check.set_valign(Gtk.Align.START)
        check.get_style_context().add_class("check-badge")
        check.set_no_show_all(True)
        overlay.add_overlay(check)
        frame.add(overlay)

        event = Gtk.EventBox()
        event.add(frame)
        event.connect("button-press-event", self._on_thumb_click, path)
        event.set_tooltip_text(path)
        box.pack_start(event, False, False, 0)

        tags = self.catalog.tags_of(path)
        tag_label = Gtk.Label(label=", ".join(tags) if tags else "—")
        tag_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        tag_label.set_max_width_chars(24)
        tag_label.get_style_context().add_class("dim-label")
        box.pack_start(tag_label, False, False, 0)

        self._thumb_widgets[path] = img
        self._frames[path] = frame
        self._checks[path] = check
        self._tag_labels[path] = tag_label
        self._paint_selection(path)
        return box

    def _load_thumbs(self, files: list[str]) -> None:
        for path in files:
            thumb = thumbnail_for(path)
            if thumb is None:
                continue
            GLib.idle_add(self._set_thumb, path, str(thumb))

    def _set_thumb(self, path: str, thumb_path: str) -> bool:
        widget = self._thumb_widgets.get(path)
        if widget is not None:
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    thumb_path, 200, 130, True)
                widget.set_from_pixbuf(pix)
            except GLib.Error:
                pass
        return False

    def _on_thumb_click(self, _widget, event, path: str) -> bool:
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)

        if event.button == 3:
            # Right-click outside the selection acts on that item alone
            if path not in self._selected:
                self._set_selection([path], anchor=path)
            self._thumb_context_menu(event)
            return True

        if event.button != 1:
            return False

        if event.type == Gdk.EventType._2BUTTON_PRESS and not (ctrl or shift):
            self._send("show", path=path)
            self.status_label.set_text(f"Wallpaper set: {path}")
            return True

        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False

        if ctrl:
            self._toggle_selection(path)
        elif shift and self._anchor in self._visible:
            start = self._visible.index(self._anchor)
            end = self._visible.index(path)
            lo, hi = sorted((start, end))
            self._set_selection(self._visible[lo:hi + 1], anchor=self._anchor)
        elif self._selected == {path}:
            self._clear_selection()   # click again on the only item: deselect
        else:
            self._set_selection([path], anchor=path)
        return True

    # ---------- selection ----------

    def _selection_list(self) -> list[str]:
        """Selected paths in gallery order."""
        return [p for p in self._visible if p in self._selected]

    def _paint_selection(self, path: str) -> None:
        selected = path in self._selected
        frame = self._frames.get(path)
        if frame is not None:
            style = frame.get_style_context()
            (style.add_class if selected else style.remove_class)("selected-thumb")
        check = self._checks.get(path)
        if check is not None:
            check.set_visible(selected)
        label = self._tag_labels.get(path)
        if label is not None:
            style = label.get_style_context()
            if selected:
                style.add_class("selected-thumb-label")
                style.remove_class("dim-label")
            else:
                style.remove_class("selected-thumb-label")
                style.add_class("dim-label")

    def _set_selection(self, pathsx: list[str], anchor: str | None = None) -> None:
        changed = self._selected ^ set(pathsx)
        self._selected = set(pathsx)
        self._anchor = anchor if anchor is not None else self._anchor
        for path in changed:
            self._paint_selection(path)
        self._update_selection_ui()

    def _toggle_selection(self, path: str) -> None:
        if path in self._selected:
            self._selected.discard(path)
        else:
            self._selected.add(path)
            self._anchor = path
        self._paint_selection(path)
        self._update_selection_ui()

    def _select_all(self) -> None:
        self._set_selection(list(self._visible),
                            anchor=self._visible[0] if self._visible else None)

    def _clear_selection(self) -> None:
        self._set_selection([], anchor=None)

    def _update_selection_ui(self) -> None:
        count = len(self._selected)
        self.selection_revealer.set_reveal_child(count > 0)
        self.selection_label.set_text(
            f"{count} selected — tags apply to all of them" if count != 1
            else "1 selected")

    def _on_key_press(self, _widget, event) -> bool:
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        key = Gdk.keyval_name(event.keyval)
        if key == "Escape" and self._selected:
            self._clear_selection()
            return True
        if ctrl and key in ("a", "A"):
            self._select_all()
            return True
        if ctrl and key in ("t", "T") and self._selected:
            self._edit_tags_dialog(self._selection_list())
            return True
        return False

    def _thumb_context_menu(self, event) -> None:
        targets = self._selection_list()
        if not targets:
            return
        single = targets[0] if len(targets) == 1 else None
        menu = Gtk.Menu()
        entries: list[tuple[str, object]] = []
        if single:
            entries.append(
                ("Set as wallpaper", lambda *_: self._send("show", path=single)))
        entries.append((
            "Edit tags…" if single else f"Edit tags of {len(targets)} items…",
            lambda *_: self._edit_tags_dialog(targets)))
        if single:
            entries.append(("Open containing folder",
                            lambda *_: self._open_folder(str(Path(single).parent))))
        else:
            entries.append(("Clear selection", lambda *_: self._clear_selection()))
        for label, callback in entries:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", callback)
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _open_folder(self, folder: str) -> None:
        subprocess.Popen(["xdg-open", folder],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ---------- tag chips ----------

    def _rebuild_chips(self) -> None:
        for child in self.chips_box.get_children():
            self.chips_box.remove(child)
        for tag, count in self.catalog.tag_counts().items():
            btn = Gtk.ToggleButton(label=f"{tag} ({count})")
            btn.get_style_context().add_class("tag-chip")
            btn.set_active(tag in self.config.filter_tags)
            btn.connect("toggled", self._on_chip_toggled, tag)
            self.chips_box.pack_start(btn, False, False, 0)
        self.chips_box.show_all()

    def _on_chip_toggled(self, btn: Gtk.ToggleButton, tag: str) -> None:
        selected = set(self.config.filter_tags)
        (selected.add if btn.get_active() else selected.discard)(tag)
        self.config.filter_tags = sorted(selected)
        self.config.save()
        if self.config.filter_enabled:
            self.refresh_gallery(rescan=False)

    def _on_filter_toggled(self, switch: Gtk.Switch, _param) -> None:
        self.config.filter_enabled = switch.get_active()
        self.config.save()
        self.refresh_gallery(rescan=False)

    def _on_filter_mode(self, combo: Gtk.ComboBoxText) -> None:
        self.config.filter_mode = combo.get_active_id() or "or"
        self.config.save()
        if self.config.filter_enabled:
            self.refresh_gallery(rescan=False)

    # ---------- playback ----------

    def _nav(self, action: str) -> None:
        response = self._send(action)
        if response and response.get("current"):
            self.status_label.set_text(f"Wallpaper set: {response['current']}")

    def _on_play_pause(self, _btn) -> None:
        status = daemon.send({"action": "status"})
        if status is None:
            self._start_daemon()
            return
        daemon.send({"action": "resume" if status.get("paused") else "pause"})
        self._poll_status()

    def _on_master_toggled(self, switch: Gtk.Switch, _param) -> None:
        if switch.get_active():
            if not daemon.is_running():
                self._start_daemon()
        else:
            response = daemon.send({"action": "off"})
            if response is None:
                # No daemon: restore directly from this process
                from .engine import Engine
                Engine(self.config, self.catalog).deactivate()
            self.status_label.set_text("Slideshow off — default wallpaper restored")
        self._poll_status()

    def _start_daemon(self) -> None:
        # Prefer the systemd unit (survives logout config); fallback to spawn
        result = subprocess.run(
            ["systemctl", "--user", "start", "bglin.service"],
            capture_output=True)
        if result.returncode != 0:
            daemon.spawn_detached()
        self.status_label.set_text("Slideshow daemon started")
        GLib.timeout_add(1500, lambda: (self._poll_status(), False)[1])

    def _poll_status(self) -> bool:
        status = daemon.send({"action": "status"}, timeout=1.0)
        running = status is not None
        icon = "media-playback-start-symbolic" if (status and status.get("paused")) \
            else "media-playback-pause-symbolic"
        self.play_btn.set_image(
            Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        self.header.set_subtitle(
            "Slideshow active" + (" (paused)" if status and status.get("paused") else "")
            if running else "Slideshow off")
        with self.master_switch.handler_block(self._master_handler):
            self.master_switch.set_active(running)
        return True  # keep the timeout alive

    # ---------- dialogs ----------

    def _edit_tags_dialog(self, targets: str | list[str]) -> None:
        """LumaLoop-style tag editor: toggle existing tags, add new ones.

        Works on one file or on the whole selection. With several files, a tag
        that only some of them carry starts out "inconsistent": leave it alone
        and each file keeps what it had; click it and it is applied (or removed)
        on every selected file.
        """
        if isinstance(targets, str):
            targets = [targets]
        targets = [p for p in targets if p]
        if not targets:
            return
        multi = len(targets) > 1

        self.catalog.reload_if_changed()
        dialog = Gtk.Dialog(
            title=f"Edit tags — {len(targets)} files" if multi else "Edit tags",
            transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Save", Gtk.ResponseType.OK)
        dialog.set_default_size(460, 460)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        if multi:
            names = ", ".join(Path(p).name for p in targets[:3])
            if len(targets) > 3:
                names += f" +{len(targets) - 3} more"
            header = Gtk.Label(label=f"{len(targets)} files: {names}", xalign=0)
            header.set_ellipsize(3)
        else:
            header = Gtk.Label(label=Path(targets[0]).name, xalign=0)
        box.add(header)

        per_file = {p: set(self.catalog.tags_of(p)) for p in targets}
        common = set.intersection(*per_file.values())
        partial = set().union(*per_file.values()) - common

        # Existing tags as toggleable chips (tri-state when multi-selecting)
        box.add(Gtk.Label(
            label="Existing tags (dimmed = only on some of them):" if multi
            else "Existing tags:", xalign=0))
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           max_children_per_line=30,
                           valign=Gtk.Align.START,
                           row_spacing=4, column_spacing=4)
        toggles: dict[str, Gtk.ToggleButton] = {}
        for tag in self.catalog.all_tags():
            btn = Gtk.ToggleButton(label=tag, active=tag in common)
            btn.get_style_context().add_class("tag-chip")
            btn.set_valign(Gtk.Align.START)
            btn.set_halign(Gtk.Align.START)
            if tag in partial:
                btn.set_inconsistent(True)
                # First click resolves the mixed state into a definite one
                btn.connect("toggled", lambda b: b.set_inconsistent(False))
            toggles[tag] = btn
            flow.add(btn)
        chips_scroll = Gtk.ScrolledWindow(vexpand=True)
        chips_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        chips_scroll.add(flow)
        box.pack_start(chips_scroll, True, True, 0)

        # New tags, comma separated (with autocomplete)
        box.add(Gtk.Label(
            label="New tags for all selected (comma separated):" if multi
            else "New tags (comma separated):", xalign=0))
        entry = Gtk.Entry()
        entry.set_placeholder_text("new, tags, here")
        entry.set_activates_default(True)
        self._attach_tag_completion(entry)
        box.add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)

        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            added = {t for t in entry.get_text().split(",") if t.strip()}
            updates: dict[str, set[str]] = {}
            for path in targets:
                tags = set(per_file[path])
                for tag, btn in toggles.items():
                    if btn.get_inconsistent():
                        continue  # untouched mixed tag: each file keeps its own
                    if btn.get_active():
                        tags.add(tag)
                    else:
                        tags.discard(tag)
                tags.update(added)
                updates[path] = tags
            self.catalog.set_tags_many(updates)
            self.refresh_gallery(rescan=False)
        dialog.destroy()

    def _attach_tag_completion(self, entry: Gtk.Entry) -> None:
        """Autocomplete the tag being typed (the part after the last comma)."""
        store = Gtk.ListStore(str)
        for tag in self.catalog.all_tags():
            store.append([tag])
        completion = Gtk.EntryCompletion(model=store, minimum_key_length=1)
        completion.set_text_column(0)

        def last_token(text: str) -> str:
            return text.rsplit(",", 1)[-1].strip().lower()

        completion.set_match_func(
            lambda _c, _key, it: store[it][0].startswith(
                last_token(entry.get_text())) if last_token(entry.get_text())
            else False)

        def on_selected(_completion, model, it):
            text = entry.get_text()
            prefix = text.rsplit(",", 1)[0] + ", " if "," in text else ""
            entry.set_text(prefix + model[it][0] + ", ")
            entry.set_position(-1)
            return True
        completion.connect("match-selected", on_selected)
        entry.set_completion(completion)

    def _on_tag_catalog(self, _btn) -> None:
        dialog = Gtk.Dialog(title="Tag catalog", transient_for=self, modal=True)
        dialog.add_buttons("Close", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(420, 480)
        box = dialog.get_content_area()
        box.set_spacing(8)
        for m in (12,):
            box.set_margin_start(m); box.set_margin_end(m)
            box.set_margin_top(m); box.set_margin_bottom(m)

        store = Gtk.ListStore(str, int, str, str)
        tree = Gtk.TreeView(model=store)
        tree.append_column(Gtk.TreeViewColumn(
            "Tag", Gtk.CellRendererText(), text=0))
        tree.append_column(Gtk.TreeViewColumn(
            "Files", Gtk.CellRendererText(), text=1))
        tree.append_column(Gtk.TreeViewColumn(
            "Hidden", Gtk.CellRendererText(), text=2))
        tree.append_column(Gtk.TreeViewColumn(
            "Ignored", Gtk.CellRendererText(), text=3))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(tree)
        box.pack_start(scroll, True, True, 0)

        def selected_tag() -> str | None:
            model, it = tree.get_selection().get_selected()
            return model[it][0] if it else None

        def populate() -> None:
            store.clear()
            for t, c in self.catalog.tag_counts().items():
                store.append([t, c,
                              "✓" if t in self.catalog.hidden_tags else "",
                              "✓" if t in self.catalog.ignored_filter_tags else ""])

        def refresh_store() -> None:
            populate()
            self.refresh_gallery(rescan=False)

        populate()

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        rename_btn = Gtk.Button(label="Rename…")
        def on_rename(_b):
            tag = selected_tag()
            if not tag:
                return
            prompt = Gtk.Dialog(title=f"Rename '{tag}'", transient_for=dialog,
                                modal=True)
            prompt.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                               "Rename", Gtk.ResponseType.OK)
            entry = Gtk.Entry(text=tag, activates_default=True)
            prompt.set_default_response(Gtk.ResponseType.OK)
            area = prompt.get_content_area()
            area.set_margin_start(12); area.set_margin_end(12)
            area.set_margin_top(12); area.set_margin_bottom(12)
            area.add(entry)
            prompt.show_all()
            if prompt.run() == Gtk.ResponseType.OK and entry.get_text().strip():
                self.catalog.rename_tag(tag, entry.get_text())
                refresh_store()
            prompt.destroy()
        rename_btn.connect("clicked", on_rename)
        actions.pack_start(rename_btn, False, False, 0)

        delete_btn = Gtk.Button(label="Delete")
        def on_delete(_b):
            tag = selected_tag()
            if tag:
                self.catalog.delete_tag(tag)
                self.config.filter_tags = [
                    t for t in self.config.filter_tags if t != tag]
                self.config.save()
                refresh_store()
        delete_btn.connect("clicked", on_delete)
        actions.pack_start(delete_btn, False, False, 0)

        def toggle_list(getter, setter, tooltip: str, label: str) -> Gtk.Button:
            btn = Gtk.Button(label=label, tooltip_text=tooltip)

            def on_toggle(_b):
                tag = selected_tag()
                if not tag:
                    return
                tags = set(getter())
                tags.symmetric_difference_update({tag})
                setter(sorted(tags))
                refresh_store()
            btn.connect("clicked", on_toggle)
            actions.pack_start(btn, False, False, 0)
            return btn

        toggle_list(lambda: self.catalog.hidden_tags,
                    self.catalog.set_hidden_tags,
                    "Media with this tag is never shown", "Hide")
        toggle_list(lambda: self.catalog.ignored_filter_tags,
                    self.catalog.set_ignored_filter_tags,
                    "No filter mode takes this tag into account", "Ignore")

        export_btn = Gtk.Button(label="Export JSON…")
        export_btn.connect("clicked", self._on_export, dialog)
        actions.pack_end(export_btn, False, False, 0)
        import_btn = Gtk.Button(label="Import JSON…")
        import_btn.connect("clicked", self._on_import, dialog)
        actions.pack_end(import_btn, False, False, 0)

        box.pack_start(actions, False, False, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _on_export(self, _btn, parent) -> None:
        chooser = Gtk.FileChooserDialog(title="Export tags", transient_for=parent,
                                        action=Gtk.FileChooserAction.SAVE)
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                            "Export", Gtk.ResponseType.OK)
        chooser.set_current_name("tags.json")
        if chooser.run() == Gtk.ResponseType.OK:
            self.catalog.export_json(Path(chooser.get_filename()),
                                     active_tags=self.config.filter_tags,
                                     filter_mode=self.config.filter_mode,
                                     auto_tag=self.config.auto_tag_on_scan)
        chooser.destroy()

    def _on_import(self, _btn, parent) -> None:
        chooser = Gtk.FileChooserDialog(title="Import tags", transient_for=parent,
                                        action=Gtk.FileChooserAction.OPEN)
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                            "Import", Gtk.ResponseType.OK)
        if chooser.run() == Gtk.ResponseType.OK:
            count, settings = self.catalog.import_json(
                Path(chooser.get_filename()))
            for key, value in settings.items():
                setattr(self.config, key, value)
            self.config.save()
            self.filter_mode_combo.set_active_id(self.config.filter_mode)
            self.refresh_gallery(rescan=False)
            self._info("Import finished", f"Tags applied to {count} files.")
        chooser.destroy()

    def _on_settings(self, _btn) -> None:
        dialog = Gtk.Dialog(title="Settings", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Save", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        for m in (16,):
            grid.set_margin_start(m); grid.set_margin_end(m)
            grid.set_margin_top(m); grid.set_margin_bottom(m)
        dialog.get_content_area().add(grid)
        row = 0

        grid.attach(Gtk.Label(label="Wallpaper folder:", xalign=1), 0, row, 1, 1)
        folder_btn = Gtk.FileChooserButton(
            title="Wallpaper folder", action=Gtk.FileChooserAction.SELECT_FOLDER)
        folder_btn.set_filename(self.config.media_dir)
        grid.attach(folder_btn, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label="Interval (seconds):", xalign=1), 0, row, 1, 1)
        interval = Gtk.SpinButton.new_with_range(5, 86400, 5)
        interval.set_value(self.config.interval_seconds)
        grid.attach(interval, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label="Playback order:", xalign=1), 0, row, 1, 1)
        order = Gtk.ComboBoxText()
        for o in ORDERS:
            order.append(o, o.capitalize())
        order.set_active_id(self.config.order)
        grid.attach(order, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label="Display mode:", xalign=1), 0, row, 1, 1)
        mode = Gtk.ComboBoxText()
        for m in MODES:
            mode.append(m, MODE_LABELS[m])
        mode.set_active_id(self.config.mode)
        grid.attach(mode, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label="Multiple monitors:", xalign=1), 0, row, 1, 1)
        monitor_mode = Gtk.ComboBoxText()
        for key, label in MONITOR_MODES.items():
            monitor_mode.append(key, label)
        monitor_mode.set_active_id(self.config.monitor_mode)
        grid.attach(monitor_mode, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label="Auto-tag new files:", xalign=1), 0, row, 1, 1)
        auto_tag = Gtk.Switch(halign=Gtk.Align.START)
        auto_tag.set_active(self.config.auto_tag_on_scan)
        grid.attach(auto_tag, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label="Video wallpapers:", xalign=1), 0, row, 1, 1)
        video_sw = Gtk.Switch(halign=Gtk.Align.START,
                              tooltip_text="Play videos as wallpaper (GStreamer)")
        video_sw.set_active(self.config.video_enabled)
        grid.attach(video_sw, 1, row, 1, 1); row += 1

        grid.attach(Gtk.Label(label="Video audio:", xalign=1), 0, row, 1, 1)
        audio_sw = Gtk.Switch(halign=Gtk.Align.START,
                              tooltip_text="Unmute video wallpapers")
        audio_sw.set_active(self.config.video_audio)
        grid.attach(audio_sw, 1, row, 1, 1); row += 1

        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            self.config.media_dir = folder_btn.get_filename() or self.config.media_dir
            self.config.interval_seconds = int(interval.get_value())
            self.config.order = order.get_active_id() or "sequential"
            self.config.mode = mode.get_active_id() or "zoom"
            self.config.monitor_mode = monitor_mode.get_active_id() or "same"
            self.config.auto_tag_on_scan = auto_tag.get_active()
            self.config.video_enabled = video_sw.get_active()
            self.config.video_audio = audio_sw.get_active()
            self.config.save()
            self.refresh_gallery(rescan=True)
        dialog.destroy()

    def _info(self, title: str, message: str) -> None:
        md = Gtk.MessageDialog(transient_for=self, modal=True,
                               message_type=Gtk.MessageType.INFO,
                               buttons=Gtk.ButtonsType.OK, text=title)
        md.format_secondary_text(message)
        md.run()
        md.destroy()


class BglinApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.ojitos369.bglin",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.get_active_window() or MainWindow(self)
        win.show_all()
        win.present()


def main() -> int:
    return BglinApp().run(None)
