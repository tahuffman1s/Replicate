"""The Adw.Application."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from replicate import APP_ID, APP_NAME, __version__, paths  # noqa: E402
from replicate.ui.context import AppContext  # noqa: E402

log = logging.getLogger(__name__)


class ReplicateApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)
        self.ctx: AppContext | None = None
        self.window = None
        self._add_action("quit", lambda *_: self.quit(), ["<primary>q"])
        self._add_action("about", self._on_about)
        self._add_action("preferences", lambda *_: self.window and self.window.show_page("settings"), ["<primary>comma"])
        self._add_action("shortcuts", self._on_shortcuts, ["<primary>question"])
        for index, key in enumerate(("overview", "servers", "images", "activity", "settings"), start=1):
            self._add_action(f"page-{key}", lambda *_, k=key: self.window and self.window.show_page(k), [f"<alt>{index}"])

    def _add_action(self, name: str, callback, accels: list[str] | None = None) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(str(paths.STYLE_CSS))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        theme.add_search_path(str(paths.ICONS_DIR))
        if paths.SOURCE_ICONS_DIR.is_dir():
            theme.add_search_path(str(paths.SOURCE_ICONS_DIR))
        Gtk.Window.set_default_icon_name(APP_ID)
        self.ctx = AppContext()
        self.apply_color_scheme()

    def apply_color_scheme(self) -> None:
        scheme = self.ctx.config.ui.color_scheme if self.ctx else "default"
        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(
            {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}.get(scheme, Adw.ColorScheme.DEFAULT)
        )

    def do_activate(self) -> None:
        if self.window is None:
            from replicate.ui.window import MainWindow

            self.window = MainWindow(self, self.ctx)
        self.window.present()

    def _on_shortcuts(self, *_args) -> None:
        from replicate.ui.shortcuts import ShortcutsWindow

        ShortcutsWindow(self.window).present()

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Travis Huffman",
            developers=["Travis Huffman"],
            version=__version__,
            website="https://github.com/tahuffman1s/Replicate",
            issue_url="https://github.com/tahuffman1s/Replicate/issues",
            support_url="https://github.com/tahuffman1s/Replicate/discussions",
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2026 Travis Huffman",
            comments=(
                "Restore SQL Server databases from VHD images into a Windows VM that "
                "Replicate creates and runs for you, and watch what the servers are doing."
            ),
            debug_info=self._debug_info(),
            debug_info_filename="replicate-debug.txt",
        )
        about.add_link("Source code", "https://github.com/tahuffman1s/Replicate")
        about.present(self.window)

    def _debug_info(self) -> str:
        import platform
        import shutil

        from replicate.vm.virsh import Virsh

        lines = [
            f"Replicate {__version__}",
            f"Python {platform.python_version()} on {platform.platform()}",
            f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
            f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
            f"AppImage: {'yes' if 'APPIMAGE' in __import__('os').environ else 'no'}",
            "",
        ]
        for tool in ("virsh", "qemu-img", "xorriso", "genisoimage", "virt-viewer", "pkexec"):
            lines.append(f"{tool}: {shutil.which(tool) or 'not found'}")
        if self.ctx:
            ok, message = Virsh(self.ctx.config.vm.uri).connection_ok()
            lines += ["", f"libvirt {self.ctx.config.vm.uri}: {'ok, ' + message if ok else message}"]
            lines.append(f"Config: {paths.CONFIG_FILE}")
            lines.append(f"Log: {paths.LOG_DIR / 'replicate.log'}")
            lines.append(f"Secrets: {self.ctx.secrets.backend}")
        return "\n".join(lines)
