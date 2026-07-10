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
                     FILTER_MODES)
from .engine import Engine
from .thumbs import thumbnail_for

CSS = b"""
.tag-chip { border-radius: 14px; padding: 2px 10px; min-height: 24px;
            margin: 1px; }
flowboxchild { padding: 0; }
.thumb-frame { border-radius: 6px; }
.current-thumb { border: 3px solid @theme_selected_bg_color; }
.video-badge { background-color: rgba(0,0,0,0.65); color: white;
               border-radius: 0 0 6px 0; padding: 1px 6px; font-size: 0.8em; }
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

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._build_header()
        self._build_body()
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

        self.filter_mode_combo = Gtk.ComboBoxText()
        for m in FILTER_MODES:
            self.filter_mode_combo.append(m, m)
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
        files = sorted(self.catalog.media.keys())
        if self.config.filter_enabled and self.config.filter_tags:
            files = self.catalog.filter(files, self.config.filter_tags,
                                        self.config.filter_mode)
        for child in self.flow.get_children():
            self.flow.remove(child)
        self._thumb_widgets.clear()
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
        self._send("reload")

    def _make_thumb_placeholder(self, path: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        frame = Gtk.Frame()
        frame.get_style_context().add_class("thumb-frame")
        img = Gtk.Image.new_from_icon_name("image-loading", Gtk.IconSize.DIALOG)
        img.set_size_request(200, 130)
        if is_video(path):
            overlay = Gtk.Overlay()
            overlay.add(img)
            badge = Gtk.Label(label=" ▶ VIDEO ")
            badge.set_halign(Gtk.Align.START)
            badge.set_valign(Gtk.Align.START)
            badge.get_style_context().add_class("video-badge")
            overlay.add_overlay(badge)
            frame.add(overlay)
        else:
            frame.add(img)

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

    def _on_thumb_click(self, _widget, event, path: str) -> None:
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            self._send("show", path=path)
            self.status_label.set_text(f"Wallpaper set: {path}")
        elif event.button == 3:
            self._thumb_context_menu(event, path)

    def _thumb_context_menu(self, event, path: str) -> None:
        menu = Gtk.Menu()
        entries = (
            ("Set as wallpaper", lambda *_: self._send("show", path=path)),
            ("Edit tags…", lambda *_: self._edit_tags_dialog(path)),
            ("Open containing folder",
             lambda *_: self._open_folder(str(Path(path).parent))),
        )
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
        self.config.filter_mode = combo.get_active_id() or "OR"
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

    def _edit_tags_dialog(self, path: str) -> None:
        """LumaLoop-style tag editor: toggle existing tags, add new ones."""
        dialog = Gtk.Dialog(title="Edit tags", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Save", Gtk.ResponseType.OK)
        dialog.set_default_size(460, 420)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.add(Gtk.Label(label=path.split("/")[-1], xalign=0))

        current = set(self.catalog.tags_of(path))

        # Existing tags as toggleable chips
        box.add(Gtk.Label(label="Existing tags:", xalign=0))
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           max_children_per_line=30,
                           valign=Gtk.Align.START,
                           row_spacing=4, column_spacing=4)
        toggles: dict[str, Gtk.ToggleButton] = {}
        for tag in self.catalog.all_tags():
            btn = Gtk.ToggleButton(label=tag, active=tag in current)
            btn.get_style_context().add_class("tag-chip")
            btn.set_valign(Gtk.Align.START)
            btn.set_halign(Gtk.Align.START)
            toggles[tag] = btn
            flow.add(btn)
        chips_scroll = Gtk.ScrolledWindow(vexpand=True)
        chips_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        chips_scroll.add(flow)
        box.pack_start(chips_scroll, True, True, 0)

        # New tags, comma separated (with autocomplete)
        box.add(Gtk.Label(label="New tags (comma separated):", xalign=0))
        entry = Gtk.Entry()
        entry.set_placeholder_text("new, tags, here")
        entry.set_activates_default(True)
        self._attach_tag_completion(entry)
        box.add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)

        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            tags = {t for t, b in toggles.items() if b.get_active()}
            tags.update(t for t in entry.get_text().split(",") if t.strip())
            self.catalog.set_tags(path, tags)
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

        store = Gtk.ListStore(str, int)
        for tag, count in self.catalog.tag_counts().items():
            store.append([tag, count])
        tree = Gtk.TreeView(model=store)
        tree.append_column(Gtk.TreeViewColumn(
            "Tag", Gtk.CellRendererText(), text=0))
        tree.append_column(Gtk.TreeViewColumn(
            "Files", Gtk.CellRendererText(), text=1))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(tree)
        box.pack_start(scroll, True, True, 0)

        def selected_tag() -> str | None:
            model, it = tree.get_selection().get_selected()
            return model[it][0] if it else None

        def refresh_store() -> None:
            store.clear()
            for t, c in self.catalog.tag_counts().items():
                store.append([t, c])
            self.refresh_gallery(rescan=False)

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
        chooser.set_current_name("bglin-tags.json")
        if chooser.run() == Gtk.ResponseType.OK:
            from pathlib import Path
            self.catalog.export_json(Path(chooser.get_filename()))
        chooser.destroy()

    def _on_import(self, _btn, parent) -> None:
        chooser = Gtk.FileChooserDialog(title="Import tags", transient_for=parent,
                                        action=Gtk.FileChooserAction.OPEN)
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                            "Import", Gtk.ResponseType.OK)
        if chooser.run() == Gtk.ResponseType.OK:
            from pathlib import Path
            count = self.catalog.import_json(Path(chooser.get_filename()))
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
