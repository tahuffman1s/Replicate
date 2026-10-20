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

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Travis Huffman",
            version=__version__,
            website="https://github.com/tahuffman1s/Replicate",
            issue_url="https://github.com/tahuffman1s/Replicate/issues",
            license_type=Gtk.License.GPL_3_0,
            comments=(
                "Restore SQL Server databases from VHD images into a Windows VM that "
                "Replicate creates and runs for you, and watch what the servers are doing."
            ),
        )
        about.present(self.window)
