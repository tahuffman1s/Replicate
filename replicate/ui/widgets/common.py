"""Small building blocks the pages share."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk, Pango  # noqa: E402


def icon_button(icon: str, tooltip: str, callback: Callable[[], None] | None = None, *, css: str | None = None) -> Gtk.Button:
    button = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER)
    button.set_tooltip_text(tooltip)
    if css:
        button.add_css_class(css)
    if callback:
        button.connect("clicked", lambda *_: callback())
    return button


def text_button(label: str, callback: Callable[[], None] | None = None, *, css: str | None = None) -> Gtk.Button:
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if css:
        button.add_css_class(css)
    if callback:
        button.connect("clicked", lambda *_: callback())
    return button


def status_label(text: str, kind: str = "neutral") -> Gtk.Label:
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("status-pill")
    label.add_css_class(f"status-{kind}")
    return label


def heading(text: str, description: str | None = None) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    title = Gtk.Label(label=text, xalign=0)
    title.add_css_class("title-4")
    box.append(title)
    if description:
        sub = Gtk.Label(label=description, xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        box.append(sub)
    return box


def confirm(parent: Gtk.Widget, heading_text: str, body: str, ok_label: str, callback: Callable[[], None],
            *, destructive: bool = True, extra: Gtk.Widget | None = None) -> None:
    dialog = Adw.AlertDialog(heading=heading_text, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", ok_label)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    if extra is not None:
        dialog.set_extra_child(extra)

    def on_response(_d: Adw.AlertDialog, response: str) -> None:
        if response == "ok":
            callback()

    dialog.connect("response", on_response)
    dialog.present(parent.get_root() if parent.get_root() else parent)


def error_dialog(parent: Gtk.Widget, heading_text: str, body: str) -> None:
    dialog = Adw.AlertDialog(heading=heading_text, body=body[:2000])
    dialog.add_response("ok", "Close")
    dialog.present(parent.get_root() if parent.get_root() else parent)


class Row(GObject.Object):
    """A dict wrapped for Gio.ListStore."""

    def __init__(self, data: dict[str, Any]):
        super().__init__()
        self.data = data


class Table(Gtk.ScrolledWindow):
    """A Gtk.ColumnView over dict rows.

    ``columns`` is a list of ``(title, key, width, align)``; ``align`` is
    "start" or "end" (numbers). ``formatters`` maps a key to a callable that
    turns the value into text.
    """

    def __init__(self, columns: list[tuple[str, str, int, str]], *, formatters: dict[str, Callable[[Any], str]] | None = None,
                 on_activate: Callable[[dict[str, Any]], None] | None = None, height: int = 220):
        super().__init__()
        self.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.set_min_content_height(height)
        self.set_vexpand(True)
        self.add_css_class("card")
        self.store = Gio.ListStore(item_type=Row)
        self.selection = Gtk.SingleSelection(model=self.store)
        self.view = Gtk.ColumnView(model=self.selection)
        self.view.add_css_class("data-table")
        self.view.set_show_row_separators(True)
        self.formatters = formatters or {}
        for title, key, width, align in columns:
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup, align)
            factory.connect("bind", self._bind, key)
            column = Gtk.ColumnViewColumn(title=title, factory=factory)
            column.set_resizable(True)
            if width:
                column.set_fixed_width(width)
            else:
                column.set_expand(True)
            self.view.append_column(column)
        if on_activate:
            self.view.connect("activate", lambda _v, pos: on_activate(self.store.get_item(pos).data))
        self.set_child(self.view)

    def _setup(self, _factory, item: Gtk.ListItem, align: str) -> None:
        label = Gtk.Inscription()
        label.set_xalign(1.0 if align == "end" else 0.0)
        label.set_text_overflow(Gtk.InscriptionOverflow.ELLIPSIZE_END)
        label.set_min_chars(4)
        label.set_nat_chars(40)
        item.set_child(label)

    def _bind(self, _factory, item: Gtk.ListItem, key: str) -> None:
        row: Row = item.get_item()
        value = row.data.get(key, "")
        fmt = self.formatters.get(key)
        text = fmt(value) if fmt else ("" if value is None else str(value))
        item.get_child().set_text(text.replace("\n", " "))

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.store.remove_all()
        for data in rows:
            self.store.append(Row(data))

    def append_rows(self, rows: list[dict[str, Any]], max_rows: int | None = None) -> None:
        for data in rows:
            self.store.append(Row(data))
        if max_rows is not None:
            extra = self.store.get_n_items() - max_rows
            if extra > 0:
                self.store.splice(0, extra, [])

    def selected(self) -> dict[str, Any] | None:
        item = self.selection.get_selected_item()
        return item.data if item is not None else None

    def clear(self) -> None:
        self.store.remove_all()


def file_chooser(parent: Gtk.Widget, title: str, callback: Callable[[str], None], *, folder: bool = False,
                 filters: list[tuple[str, list[str]]] | None = None) -> None:
    dialog = Gtk.FileDialog(title=title)
    if filters:
        store = Gio.ListStore(item_type=Gtk.FileFilter)
        for name, patterns in filters:
            f = Gtk.FileFilter()
            f.set_name(name)
            for pattern in patterns:
                f.add_pattern(pattern)
            store.append(f)
        dialog.set_filters(store)

    def done(d: Gtk.FileDialog, result) -> None:
        try:
            file = d.select_folder_finish(result) if folder else d.open_finish(result)
        except Exception:  # noqa: BLE001 - cancelled
            return
        if file is not None and file.get_path():
            callback(file.get_path())

    window = parent.get_root()
    if folder:
        dialog.select_folder(window, None, done)
    else:
        dialog.open(window, None, done)


def wrap_label(text: str, *, css: list[str] | None = None) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
    for c in css or []:
        label.add_css_class(c)
    return label


def spinner_row(text: str) -> Gtk.Box:
    box = Gtk.Box(spacing=8)
    spinner = Gtk.Spinner()
    spinner.start()
    box.append(spinner)
    box.append(Gtk.Label(label=text, xalign=0))
    return box
