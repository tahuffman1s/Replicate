"""Main window: sidebar navigation, pages, toasts, VM status polling."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from replicate import APP_NAME  # noqa: E402
from replicate.tasks import run_in_thread  # noqa: E402
from replicate.ui.context import TOAST, VM_STATUS, AppContext  # noqa: E402
from replicate.ui.pages.activity import ActivityPage  # noqa: E402
from replicate.ui.pages.images import ImagesPage  # noqa: E402
from replicate.ui.pages.overview import OverviewPage  # noqa: E402
from replicate.ui.pages.servers import ServersPage  # noqa: E402
from replicate.ui.pages.settings import SettingsPage  # noqa: E402

log = logging.getLogger(__name__)

PAGES = [
    ("overview", "Overview", "computer-symbolic"),
    ("servers", "SQL Servers", "network-server-symbolic"),
    ("images", "Images & Clones", "drive-multidisk-symbolic"),
    ("activity", "Activity", "replicate-activity-symbolic"),
    ("settings", "Settings", "emblem-system-symbolic"),
]

STATUS_POLL_SECONDS = 6


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, ctx: AppContext):
        super().__init__(application=app, title=APP_NAME)
        self.ctx = ctx
        self.set_default_size(ctx.config.ui.window_width, ctx.config.ui.window_height)
        self.set_size_request(600, 480)

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(200)
        self.split.set_max_sidebar_width(240)
        self.toasts.set_child(self.split)
        # Narrow windows: the sidebar and the page take turns, with a back button.
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 860sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        # Sidebar
        sidebar_box = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_title(True)
        header.set_title_widget(Adw.WindowTitle(title=APP_NAME, subtitle=""))
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        from gi.repository import Gio

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Settings", "app.preferences")
        section.append("Keyboard Shortcuts", "app.shortcuts")
        section.append("About Replicate", "app.about")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Quit", "app.quit")
        menu.append_section(None, section)
        menu_button.set_tooltip_text("Main menu")
        menu_button.set_primary(True)
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)
        sidebar_box.add_top_bar(header)
        self.sidebar = Gtk.ListBox()
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar.add_css_class("sidebar-list")
        for key, label, icon in PAGES:
            row = Gtk.ListBoxRow()
            row.page_key = key  # type: ignore[attr-defined]
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=label, xalign=0))
            row.set_child(box)
            row.update_property([Gtk.AccessibleProperty.LABEL], [label])
            self.sidebar.append(row)
        self.sidebar.connect("row-selected", self._on_row_selected)
        sidebar_box.set_content(self.sidebar)
        self.split.set_sidebar(Adw.NavigationPage(title=APP_NAME, child=sidebar_box))

        # Content
        self.content_view = Adw.ToolbarView()
        self.content_header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="Overview", subtitle="")
        self.content_header.set_title_widget(self.title_widget)
        self.content_view.add_top_bar(self.content_header)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.content_view.set_content(self.stack)
        self.split.set_content(Adw.NavigationPage(title="Content", child=self.content_view))

        self.pages = {
            "overview": OverviewPage(ctx),
            "servers": ServersPage(ctx),
            "images": ImagesPage(ctx),
            "activity": ActivityPage(ctx),
            "settings": SettingsPage(ctx),
        }
        for key, page in self.pages.items():
            self.stack.add_named(page, key)

        ctx.subscribe(TOAST, self.show_toast)
        self.connect("close-request", self._on_close)
        for name, accel, method in (("refresh", "<primary>r", "on_refresh"), ("add", "<primary>n", "on_add")):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, m=method: self._page_action(m))
            self.add_action(action)
            app.set_accels_for_action(f"win.{name}", [accel])

        self.show_page(ctx.config.ui.last_page if ctx.config.ui.last_page in self.pages else "overview")
        self._poll_status()
        GLib.timeout_add_seconds(STATUS_POLL_SECONDS, self._poll_status)

    # -- navigation --------------------------------------------------------
    def show_page(self, key: str) -> None:
        index = next((i for i, (k, _, _) in enumerate(PAGES) if k == key), 0)
        row = self.sidebar.get_row_at_index(index)
        if row is not None and self.sidebar.get_selected_row() is not row:
            self.sidebar.select_row(row)
        else:
            self._activate_page(key)

    def _on_row_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None:
            self._activate_page(row.page_key)  # type: ignore[attr-defined]

    def _page_action(self, method: str) -> None:
        page = self.pages[self.stack.get_visible_child_name()]
        handler = getattr(page, method, None)
        if handler:
            handler()

    def _activate_page(self, key: str) -> None:
        self.stack.set_visible_child_name(key)
        self.split.set_show_content(True)
        label = next(label for k, label, _ in PAGES if k == key)
        self.title_widget.set_title(label)
        self.ctx.config.ui.last_page = key
        page = self.pages[key]
        if hasattr(page, "on_shown"):
            page.on_shown()

    # -- status polling ----------------------------------------------------
    def _poll_status(self) -> bool:
        if getattr(self, "_polling", False):
            return True
        self._polling = True

        def done(status) -> None:
            self._polling = False
            self.ctx.last_status = status
            self.title_widget.set_subtitle(f"{self.ctx.config.vm.name}: {status.label}" + (f" · {status.ip}" if status.ip else ""))
            self.ctx.emit(VM_STATUS, status)

        def failed(exc: BaseException) -> None:
            self._polling = False
            log.debug("status poll failed: %s", exc)

        run_in_thread(self.ctx.vm.status, on_done=done, on_error=failed, name="status-poll")
        return True

    # -- misc --------------------------------------------------------------
    def show_toast(self, text: str, button: str | None = None, callback=None) -> None:
        toast = Adw.Toast(title=text, timeout=4 if not button else 8)
        if button and callback:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda *_: callback())
        self.toasts.add_toast(toast)

    def _on_close(self, *_args) -> bool:
        width, height = self.get_default_size()
        self.ctx.config.ui.window_width = width
        self.ctx.config.ui.window_height = height
        try:
            self.ctx.save_config()
        except OSError:
            log.exception("could not save config")
        for page in self.pages.values():
            if hasattr(page, "shutdown"):
                page.shutdown()
        return False
