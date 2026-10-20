"""Add or edit a SQL Server, and link servers to each other."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from replicate.config import SqlServer  # noqa: E402
from replicate.tasks import run_in_thread  # noqa: E402
from replicate.ui.context import SERVERS_CHANGED, AppContext  # noqa: E402
from replicate.ui.widgets.common import dialog_header, error_dialog, status_label  # noqa: E402


class ServerDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext, server: SqlServer | None = None):
        self.editing = server is not None
        super().__init__(title="Edit SQL Server" if self.editing else "Add SQL Server", content_width=560)
        self.ctx = ctx
        self.server = server or SqlServer(name="", vm_managed=False, host="", service_name="")
        s = self.server

        view = Adw.ToolbarView()
        header, self.save_button = dialog_header(self, "Save" if self.editing else "Add", self._on_save)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        general = Adw.PreferencesGroup(title="Server")
        self.name = Adw.EntryRow(title="Display name", text=s.name)
        general.add(self.name)
        self.in_vm = Adw.SwitchRow(use_markup=False, title="Runs inside the Replicate VM", subtitle="Its address follows the VM and its service can be started and stopped here")
        self.in_vm.set_active(s.vm_managed or s.host.lower() == "vm")
        self.in_vm.connect("notify::active", lambda *_: self._sync_vm_rows())
        general.add(self.in_vm)
        self.host = Adw.EntryRow(title="Host name or address", text="" if s.host.lower() == "vm" else s.host)
        general.add(self.host)
        self.instance = Adw.EntryRow(title="Instance name (blank for the default instance)", text=s.instance)
        general.add(self.instance)
        self.port = Adw.SpinRow.new_with_range(0, 65535, 1)
        self.port.set_title("TCP port")
        self.port.set_subtitle("0 asks SQL Browser for a named instance's port")
        self.port.set_value(s.port)
        general.add(self.port)
        self.service = Adw.EntryRow(title="Windows service name", text=s.service_name)
        general.add(self.service)
        page.add(general)

        auth = Adw.PreferencesGroup(title="Authentication")
        self.auth = Adw.ComboRow(title="Method")
        self.auth.set_model(Gtk.StringList.new(["SQL Server login", "Windows (NTLM)"]))
        self.auth.set_selected(1 if s.auth == "windows" else 0)
        auth.add(self.auth)
        self.user = Adw.EntryRow(title="Login (DOMAIN\\user for Windows)", text=s.username)
        auth.add(self.user)
        self.password = Adw.PasswordEntryRow(title="Password" + (" (blank keeps the saved one)" if self.editing else ""))
        auth.add(self.password)
        self.database = Adw.EntryRow(title="Default database", text=s.database)
        auth.add(self.database)
        page.add(auth)

        notes = Adw.PreferencesGroup(title="Notes")
        self.notes = Adw.EntryRow(title="Notes", text=s.notes)
        notes.add(self.notes)
        page.add(notes)

        test_group = Adw.PreferencesGroup()
        test_box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.test_button = Gtk.Button(label="Test connection")
        self.test_button.connect("clicked", self._on_test)
        test_box.append(self.test_button)
        self.test_result = Gtk.Label(wrap=True, xalign=0)
        self.test_result.add_css_class("dim-label")
        test_box.append(self.test_result)
        test_group.add(test_box)
        page.add(test_group)

        self._sync_vm_rows()

    def _sync_vm_rows(self) -> None:
        in_vm = self.in_vm.get_active()
        self.host.set_sensitive(not in_vm)
        self.service.set_sensitive(in_vm)
        if in_vm and not self.service.get_text():
            inst = self.instance.get_text().strip()
            self.service.set_text("MSSQLSERVER" if not inst else f"MSSQL${inst}")

    def _collect(self) -> tuple[SqlServer, str | None]:
        s = self.server
        s.name = self.name.get_text().strip() or (self.host.get_text().strip() or "SQL Server")
        s.vm_managed = self.in_vm.get_active()
        s.host = "vm" if s.vm_managed else self.host.get_text().strip()
        s.instance = self.instance.get_text().strip()
        s.port = int(self.port.get_value())
        s.service_name = self.service.get_text().strip() if s.vm_managed else ""
        s.auth = "windows" if self.auth.get_selected() == 1 else "sql"
        s.username = self.user.get_text().strip()
        s.database = self.database.get_text().strip() or "master"
        s.notes = self.notes.get_text()
        password = self.password.get_text() or None
        return s, password

    def _validate(self, s: SqlServer) -> str | None:
        if not s.vm_managed and not s.host:
            return "Enter the server's host name or address."
        if not s.username:
            return "Enter the login to connect with."
        if not s.port and not s.instance:
            return "Enter a TCP port, or an instance name so SQL Browser can find the port."
        return None

    def _on_test(self, *_args) -> None:
        s, password = self._collect()
        problem = self._validate(s)
        if problem:
            self.test_result.set_label(problem)
            return
        self.test_button.set_sensitive(False)
        self.test_result.set_label("Connecting…")

        def done(info: dict) -> None:
            self.test_button.set_sensitive(True)
            self.test_result.set_label(f"OK: {info.get('servername')} · {info.get('edition')} {info.get('version')} at {info.get('host')}")

        def failed(exc: BaseException) -> None:
            self.test_button.set_sensitive(True)
            self.test_result.set_label(str(exc))

        pw = password if password is not None else (self.ctx.registry.password(s) if self.editing else "")
        run_in_thread(self.ctx.registry.test, s, pw, on_done=done, on_error=failed, name="sql-test")

    def _on_save(self, *_args) -> None:
        s, password = self._collect()
        problem = self._validate(s)
        if problem:
            error_dialog(self, "Missing details", problem)
            return
        if self.editing:
            self.ctx.registry.update(s, password)
        else:
            self.ctx.registry.add(s, password or "")
        self.ctx.emit(SERVERS_CHANGED)
        self.close()


class LinksDialog(Adw.Dialog):
    """Linked servers on one server: switch other registered servers on or off."""

    def __init__(self, ctx: AppContext, source: SqlServer):
        super().__init__(title=f"Linked servers on {source.name}", content_width=560)
        self.ctx = ctx
        self.source = source
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        self.group = Adw.PreferencesGroup(title="Servers Replicate knows")
        self.group.set_description(
            "Turning a server on creates a linked server (sp_addlinkedserver) on "
            f"{source.name}, so queries there can reach it as [LinkedName].db.schema.table. "
            "SQL logins are mapped with the target's saved credentials; Windows logins pass through."
        )
        page.add(self.group)
        self.rows: dict[str, Adw.SwitchRow] = {}
        for target in ctx.registry.servers:
            if target.id == source.id:
                continue
            row = Adw.SwitchRow(use_markup=False, title=target.name, subtitle=f"as [{ctx.registry.link_name(target)}]")
            row.set_active(target.id in source.linked_to)
            row.connect("notify::active", self._on_toggle, target)
            test = Gtk.Button(icon_name="emblem-ok-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Test the link")
            test.add_css_class("flat")
            test.connect("clicked", self._on_test, target, row)
            row.add_suffix(test)
            self.rows[target.id] = row
            self.group.add(row)
        if not self.rows:
            self.group.add(Adw.ActionRow(use_markup=False, title="Add another server first", subtitle="Links are between servers in the list."))

        self.existing = Adw.PreferencesGroup(title="Linked servers on the server right now")
        page.add(self.existing)
        self.existing_rows: list[Gtk.Widget] = []
        self._load_existing()

    def _load_existing(self) -> None:
        for r in self.existing_rows:
            self.existing.remove(r)
        self.existing_rows.clear()
        loading = Adw.ActionRow(use_markup=False, title="Loading…")
        self.existing.add(loading)
        self.existing_rows.append(loading)

        def done(links) -> None:
            for r in self.existing_rows:
                self.existing.remove(r)
            self.existing_rows.clear()
            if not links:
                row = Adw.ActionRow(use_markup=False, title="None")
                self.existing.add(row)
                self.existing_rows.append(row)
            for link in links:
                row = Adw.ActionRow(use_markup=False, title=link.name, subtitle=f"{link.provider} → {link.data_source}")
                if link.is_replicate:
                    row.add_suffix(status_label("Replicate", "good"))
                self.existing.add(row)
                self.existing_rows.append(row)

        def failed(exc: BaseException) -> None:
            loading.set_title("Could not read sys.servers")
            loading.set_subtitle(str(exc))

        run_in_thread(self.ctx.registry.list_links, self.source, on_done=done, on_error=failed, name="links")

    def _on_toggle(self, row: Adw.SwitchRow, _pspec, target: SqlServer) -> None:
        enable = row.get_active()
        row.set_sensitive(False)
        func = self.ctx.registry.add_link if enable else self.ctx.registry.remove_link

        def done(result) -> None:
            row.set_sensitive(True)
            self.ctx.toast(f"Linked {target.name} as [{result}]" if enable else f"Removed the link to {target.name}")
            self.ctx.emit(SERVERS_CHANGED)
            self._load_existing()

        def failed(exc: BaseException) -> None:
            row.set_sensitive(True)
            row.handler_block_by_func(self._on_toggle)
            row.set_active(not enable)
            row.handler_unblock_by_func(self._on_toggle)
            error_dialog(self, "Linking failed", str(exc))

        run_in_thread(func, self.source, target, on_done=done, on_error=failed, name="link")

    def _on_test(self, _button, target: SqlServer, row: Adw.SwitchRow) -> None:
        row.set_subtitle("Testing…")

        def done(result: str) -> None:
            row.set_subtitle(f"as [{self.ctx.registry.link_name(target)}] · OK (saw {result})")

        def failed(exc: BaseException) -> None:
            row.set_subtitle(f"as [{self.ctx.registry.link_name(target)}] · {exc}")

        run_in_thread(self.ctx.registry.test_link, self.source, target, on_done=done, on_error=failed, name="link-test")


class DatabasesDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext, server: SqlServer):
        super().__init__(title=f"Databases on {server.name}", content_width=620)
        self.ctx = ctx
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        from replicate.ui.widgets.common import Table

        self.table = Table(
            [("Database", "name", 0, "start"), ("State", "state", 90, "start"), ("Recovery", "recovery", 90, "start"), ("Size (MB)", "size_mb", 100, "end"), ("Created", "create_date", 150, "start")],
            formatters={"size_mb": lambda v: f"{float(v):,.1f}", "create_date": lambda v: str(v)[:16]},
            height=360,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("page-content")
        self.status = Gtk.Label(label="Loading…", xalign=0)
        self.status.add_css_class("dim-label")
        box.append(self.status)
        box.append(self.table)
        view.set_content(box)
        self.set_child(view)

        def done(rows) -> None:
            self.table.set_rows(rows)
            self.status.set_label(f"{len(rows)} user database{'s' if len(rows) != 1 else ''}")

        run_in_thread(ctx.registry.databases, server, on_done=done, on_error=lambda e: self.status.set_label(str(e)), name="dbs")
