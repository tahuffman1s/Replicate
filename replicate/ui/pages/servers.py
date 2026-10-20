"""SQL Servers: the list, connection checks, links, databases."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from replicate.config import SqlServer  # noqa: E402
from replicate.tasks import run_in_thread  # noqa: E402
from replicate.ui.context import SERVERS_CHANGED, VM_STATUS, AppContext  # noqa: E402
from replicate.ui.dialogs.server import DatabasesDialog, LinksDialog, ServerDialog  # noqa: E402
from replicate.ui.widgets.common import confirm, icon_button, set_status, status_label, status_page, text_button  # noqa: E402


class ServersPage(Gtk.ScrolledWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.switcher = Gtk.Stack()
        self.set_child(self.switcher)
        self.empty = status_page(
            "network-server-symbolic",
            "No SQL Servers yet",
            "Instances inside the Replicate VM appear here once it is set up. "
            "You can also add any SQL Server you can reach.",
            text_button("Add Server", self._on_add, css="suggested-action"),
        )
        self.switcher.add_named(self.empty, "empty")
        clamp = Adw.Clamp(maximum_size=900, tightening_threshold=700)
        self.switcher.add_named(clamp, "content")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.add_css_class("page-content")
        clamp.set_child(box)

        self.group = Adw.PreferencesGroup(title="Servers")
        self.group.set_description(
            "Instances inside the Replicate VM appear here by themselves once the VM is set up. "
            "Add any other SQL Server you can reach; link servers to each other from a server's menu."
        )
        header = Gtk.Box(spacing=6)
        header.append(text_button("Check all", self._check_all))
        add = text_button("Add server", self._on_add, css="suggested-action")
        header.append(add)
        self.group.set_header_suffix(header)
        box.append(self.group)
        self.rows: list[Gtk.Widget] = []
        self._status: dict[str, tuple[str, str]] = {}

        ctx.subscribe(SERVERS_CHANGED, self.refresh)
        ctx.subscribe(VM_STATUS, lambda _s: None)
        self.refresh()

    def refresh(self) -> None:
        for row in self.rows:
            self.group.remove(row)
        self.rows.clear()
        servers = self.ctx.registry.servers
        self.switcher.set_visible_child_name("content" if servers else "empty")
        for server in servers:
            self.group.add(self._make_row(server))

    def _make_row(self, server: SqlServer) -> Adw.ActionRow:
        where = "Replicate VM" if server.vm_managed else server.host
        instance = f"\\{server.instance}" if server.instance else ""
        port = f":{server.port}" if server.port else ""
        links = ""
        if server.linked_to:
            names = [s.name for s in (self.ctx.registry.get(i) for i in server.linked_to) if s]
            links = " · linked to " + ", ".join(names)
        row = Adw.ActionRow(use_markup=False, title=server.name, subtitle=f"{where}{instance}{port} · {server.username} ({'SQL' if server.auth == 'sql' else 'Windows'}){links}")
        row.set_activatable(True)
        row.connect("activated", lambda *_: self._on_edit(server))
        row.add_prefix(Gtk.Image.new_from_icon_name("network-server-symbolic" if not server.vm_managed else "computer-symbolic"))
        text, kind = self._status.get(server.id, ("", "neutral"))
        pill = status_label(text or "not checked", kind)
        row.add_suffix(pill)
        row.pill = pill  # type: ignore[attr-defined]
        row.add_suffix(icon_button("emblem-ok-symbolic", "Check the connection", lambda: self._check(server, row), css="flat"))
        menu_button = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER)
        menu_button.add_css_class("flat")
        popover = Gtk.Popover()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for label, cb in (
            ("Edit…", lambda: self._on_edit(server)),
            ("Linked servers…", lambda: LinksDialog(self.ctx, server).present(self.get_root())),
            ("Databases…", lambda: DatabasesDialog(self.ctx, server).present(self.get_root())),
            ("Watch activity", lambda: self._watch(server)),
            ("Remove", lambda: self._on_remove(server)),
        ):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.set_halign(Gtk.Align.FILL)
            b.get_child().set_xalign(0)
            if label == "Remove":
                b.add_css_class("destructive-action")

            def handler(_b, callback=cb):
                popover.popdown()
                callback()

            b.connect("clicked", handler)
            menu_box.append(b)
        popover.set_child(menu_box)
        menu_button.set_popover(popover)
        row.add_suffix(menu_button)
        self.rows.append(row)
        return row

    def _set_pill(self, row: Adw.ActionRow, text: str, kind: str) -> None:
        set_status(row.pill, text, kind)  # type: ignore[attr-defined]

    def on_refresh(self) -> None:
        self._check_all()

    def on_add(self) -> None:
        self._on_add()

    def _check(self, server: SqlServer, row: Adw.ActionRow) -> None:
        self._set_pill(row, "checking…", "neutral")

        def done(info: dict) -> None:
            self._status[server.id] = (f"online · {info.get('version', '')[:10]}", "good")
            self._set_pill(row, *self._status[server.id])

        def failed(exc: BaseException) -> None:
            self._status[server.id] = ("offline", "bad")
            self._set_pill(row, "offline", "bad")
            row.set_tooltip_text(str(exc))

        run_in_thread(self.ctx.registry.test, server, on_done=done, on_error=failed, name="sql-check")

    def _check_all(self) -> None:
        for row, server in zip(self.rows, self.ctx.registry.servers):
            if isinstance(row, Adw.ActionRow) and hasattr(row, "pill"):
                self._check(server, row)

    def _on_add(self) -> None:
        ServerDialog(self.ctx).present(self.get_root())

    def _on_edit(self, server: SqlServer) -> None:
        ServerDialog(self.ctx, server).present(self.get_root())

    def _on_remove(self, server: SqlServer) -> None:
        confirm(
            self,
            f"Remove {server.name}?",
            "Only Replicate's entry is removed; the server itself is untouched.",
            "Remove",
            lambda: (self.ctx.registry.remove(server), self.ctx.emit(SERVERS_CHANGED)),
        )

    def _watch(self, server: SqlServer) -> None:
        window = self.get_root()
        window.pages["activity"].select_server(server)
        window.show_page("activity")
