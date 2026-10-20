"""Overview: the VM, its SQL Server services, and what is attached to it."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from replicate.host import preflight  # noqa: E402
from replicate.tasks import run_in_thread  # noqa: E402
from replicate.ui.context import INSTALL_PROGRESS, MOUNTS_CHANGED, SERVERS_CHANGED, VM_STATUS, AppContext  # noqa: E402
from replicate.ui.dialogs.create_vm import CreateVmDialog, resume_install  # noqa: E402
from replicate.ui.widgets.common import confirm, set_status, status_label, status_page, text_button  # noqa: E402
from replicate.vm.manager import VmStatus  # noqa: E402

log = logging.getLogger(__name__)


class OverviewPage(Gtk.ScrolledWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self._services: list[dict] = []
        self._service_rows: dict[str, Adw.SwitchRow] = {}
        self._busy = False
        self._services_timer: int | None = None
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)
        self.banner = Adw.Banner()
        self.banner.set_button_label("Details")
        self.banner.connect("button-clicked", lambda *_: self._show_preflight())
        self._preflight = None
        outer.append(self.banner)

        # Either the "nothing here yet" page or the real content.
        self.switcher = Gtk.Stack()
        self.switcher.set_vexpand(True)
        outer.append(self.switcher)
        self.empty = status_page(
            "computer-symbolic",
            "No SQL host VM yet",
            "Replicate creates a Windows VM with SQL Server for you, in the background. "
            "Set the Windows ISO and the instance names in Settings first, then create it here.",
            text_button("Create the VM", self._on_create, css="suggested-action"),
            text_button("Open Settings", lambda: self.get_root().show_page("settings")),
        )
        self.switcher.add_named(self.empty, "empty")
        clamp = Adw.Clamp(maximum_size=900, tightening_threshold=700)
        self.switcher.add_named(clamp, "content")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.add_css_class("page-content")
        clamp.set_child(box)

        # Hero card
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hero.add_css_class("card")
        hero.add_css_class("vm-hero")
        top = Gtk.Box(spacing=12)
        icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        icon.set_pixel_size(40)
        top.append(icon)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.vm_name = Gtk.Label(xalign=0)
        self.vm_name.add_css_class("title-1")
        self.vm_sub = Gtk.Label(xalign=0, wrap=True)
        self.vm_sub.add_css_class("dim-label")
        titles.append(self.vm_name)
        titles.append(self.vm_sub)
        top.append(titles)
        self.status_pill = status_label("…")
        self.status_pill.set_valign(Gtk.Align.START)
        top.append(self.status_pill)
        hero.append(top)

        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.progress_label = Gtk.Label(xalign=0, wrap=True)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_box.append(self.progress_label)
        self.progress_box.append(self.progress_bar)
        self.progress_box.set_visible(False)
        hero.append(self.progress_box)

        self.controls = Gtk.Box(spacing=8)
        self.btn_create = text_button("Create the VM", self._on_create, css="suggested-action")
        self.btn_start = text_button("Start", lambda: self._vm_action("start"), css="suggested-action")
        self.btn_shutdown = text_button("Shut down", lambda: self._vm_action("shutdown"))
        self.btn_pause = text_button("Pause", lambda: self._vm_action("suspend"))
        self.btn_resume = text_button("Resume", lambda: self._vm_action("resume"), css="suggested-action")
        self.btn_force = text_button("Force off", self._on_force_off, css="destructive-action")
        self.btn_screen = text_button("Open screen", self._open_console)
        self.btn_watch = text_button("Follow the install", self._on_resume_watch)
        for b in (self.btn_create, self.btn_start, self.btn_shutdown, self.btn_pause, self.btn_resume, self.btn_force, self.btn_screen, self.btn_watch):
            self.controls.append(b)
        hero.append(self.controls)
        box.append(hero)

        # SQL services
        self.services_group = Adw.PreferencesGroup(title="SQL Server instances")
        self.services_group.set_description("Start or stop an instance's Windows service without touching the VM.")
        header = Gtk.Box(spacing=6)
        header.append(text_button("Start all", lambda: self._all_services(True)))
        header.append(text_button("Stop all", lambda: self._all_services(False)))
        self.services_group.set_header_suffix(header)
        self.services_placeholder = Adw.ActionRow(use_markup=False, title="Start the VM to see its services")
        self.services_group.add(self.services_placeholder)
        box.append(self.services_group)

        # Attached disks
        self.mounts_group = Adw.PreferencesGroup(title="Attached images")
        self.mounts_group.set_description("Clones and images currently plugged into the VM.")
        go = text_button("Images & Clones", lambda: self.get_root().show_page("images"))
        self.mounts_group.set_header_suffix(go)
        self._mount_rows: list[Gtk.Widget] = []
        box.append(self.mounts_group)

        ctx.subscribe(VM_STATUS, self._on_status)
        ctx.subscribe(INSTALL_PROGRESS, self._on_progress)
        ctx.subscribe(MOUNTS_CHANGED, self._refresh_mounts)
        ctx.subscribe(SERVERS_CHANGED, lambda: self._refresh_services(force=True))
        self._on_status(ctx.last_status)
        self._refresh_mounts()
        self._run_preflight()

    # -- preflight ----------------------------------------------------------
    def _run_preflight(self) -> None:
        def done(result) -> None:
            self._preflight = result
            if result.ok:
                self.banner.set_revealed(False)
            else:
                count = len(result.problems)
                self.banner.set_title(f"This computer is missing {count} thing{'s' if count != 1 else ''} Replicate needs")
                self.banner.set_revealed(True)

        run_in_thread(preflight, self.ctx.config.vm.uri, on_done=done, on_error=lambda e: None, name="preflight")

    def _show_preflight(self) -> None:
        result = self._preflight
        body = "\n\n".join(f"• {p}" for p in (result.problems if result else [])) or "Nothing is missing."
        if result and result.notes:
            body += "\n\nAlso worth knowing:\n" + "\n".join(f"• {n}" for n in result.notes)
        dialog = Adw.AlertDialog(heading="What this computer needs", body=body)
        dialog.add_response("close", "Close")
        dialog.add_response("again", "Check Again")
        dialog.set_response_appearance("again", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("again")
        dialog.connect("response", lambda _d, r: self._run_preflight() if r == "again" else None)
        dialog.present(self.get_root())

    # -- status -------------------------------------------------------------
    def _on_status(self, status: VmStatus) -> None:
        vm = self.ctx.config.vm
        self.vm_name.set_label(vm.name)
        details = [f"{vm.memory_mib // 1024} GiB RAM", f"{vm.vcpus} vCPUs", f"{len(vm.sql_instances)} instance{'s' if len(vm.sql_instances) != 1 else ''}"]
        if status.ip:
            details.append(status.ip)
        self.vm_sub.set_label(" · ".join(details))
        kind = "good" if status.running and status.agent else "warn" if status.running or status.paused or status.installing else "neutral"
        set_status(self.status_pill, status.label, kind)

        defined = status.defined
        self.switcher.set_visible_child_name("content" if defined or status.installing else "empty")
        self.btn_create.set_visible(not defined)
        self.btn_start.set_visible(defined and not status.running and not status.paused)
        self.btn_shutdown.set_visible(status.running)
        self.btn_pause.set_visible(status.running)
        self.btn_resume.set_visible(status.paused)
        self.btn_force.set_visible(status.running or status.paused)
        self.btn_screen.set_visible(defined)
        phase = self.ctx.state.vm_install.get("phase")
        self.btn_watch.set_visible(defined and phase in ("windows", "sql") and not getattr(self, "_watching", False))
        if status.installing and not self.progress_box.get_visible():
            self.progress_label.set_label(status.install_phase or "Installing")
            self.progress_bar.pulse()
            self.progress_box.set_visible(True)
        if not status.installing and phase in ("done", None, "", "failed") and self.progress_bar.get_fraction() >= 1.0:
            self.progress_box.set_visible(False)
        self._refresh_services()

    def _on_progress(self, message: str, fraction: float) -> None:
        self._watching = True
        self.progress_box.set_visible(True)
        self.progress_label.set_label(message)
        if fraction < 0:
            self.progress_bar.pulse()
        else:
            self.progress_bar.set_fraction(fraction)
        if fraction >= 1.0 or message.startswith("Failed"):
            self._watching = False
            GLib.timeout_add_seconds(8, lambda: (self.progress_box.set_visible(False), False)[1])

    # -- VM actions ---------------------------------------------------------
    def _on_create(self) -> None:
        CreateVmDialog(self.ctx).present(self.get_root())

    def _on_resume_watch(self) -> None:
        self._watching = True
        self.btn_watch.set_visible(False)
        resume_install(self.ctx)

    def _vm_action(self, action: str) -> None:
        func = getattr(self.ctx.vm, action)
        labels = {"start": "Starting the VM", "shutdown": "Asking Windows to shut down", "suspend": "Pausing the VM", "resume": "Resuming the VM"}
        self.ctx.toast(labels.get(action, action))

        def done(_r) -> None:
            self.ctx.registry.forget_vm_ip()
            self.get_root()._poll_status()

        run_in_thread(func, on_done=done, on_error=lambda e: self.ctx.toast(f"{action} failed: {e}"), name=f"vm-{action}")

    def _on_force_off(self) -> None:
        confirm(
            self,
            "Force the VM off?",
            "This is like pulling the power cord: SQL Server will not shut down cleanly. Use Shut down when you can.",
            "Force off",
            lambda: self._vm_action("force_off"),
        )

    def _open_console(self) -> None:
        try:
            self.ctx.vm.open_console()
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(str(exc))

    # -- services -----------------------------------------------------------
    def _refresh_services(self, force: bool = False) -> None:
        status = self.ctx.last_status
        if not (status.running and status.agent):
            self._set_service_rows([])
            self.services_placeholder.set_title("Start the VM to see its services" if status.defined else "Create the VM to get SQL Server instances")
            self.services_placeholder.set_visible(True)
            return
        if self._busy and not force:
            return
        self._busy = True

        def done(services: list[dict]) -> None:
            self._busy = False
            self._services = services
            self._set_service_rows(services)

        def failed(exc: BaseException) -> None:
            self._busy = False
            log.debug("service query failed: %s", exc)

        run_in_thread(self.ctx.vm.sql_services, on_done=done, on_error=failed, name="sql-services")

    def _set_service_rows(self, services: list[dict]) -> None:
        wanted = {s["name"] for s in services}
        for name, row in list(self._service_rows.items()):
            if name not in wanted:
                self.services_group.remove(row)
                del self._service_rows[name]
        self.services_placeholder.set_visible(not services)
        if not services:
            return
        by_service = {s.service_name: s for s in self.ctx.config.vm_servers()}
        for svc in services:
            name = svc["name"]
            running = svc.get("status") == "Running"
            row = self._service_rows.get(name)
            registered = by_service.get(name)
            title = registered.name if registered else svc.get("display", name)
            subtitle = f"{name} · {svc.get('status', '?')}" + (f" · port {registered.port}" if registered else "")
            if row is None:
                row = Adw.SwitchRow(use_markup=False, title=title, subtitle=subtitle)
                row.set_active(running)
                row.connect("notify::active", self._on_service_toggled, name)
                self._service_rows[name] = row
                self.services_group.add(row)
            else:
                row.set_title(title)
                row.set_subtitle(subtitle)
                if row.get_active() != running:
                    row.handler_block_by_func(self._on_service_toggled)
                    row.set_active(running)
                    row.handler_unblock_by_func(self._on_service_toggled)

    def _on_service_toggled(self, row: Adw.SwitchRow, _pspec, name: str) -> None:
        start = row.get_active()
        row.set_sensitive(False)
        row.set_subtitle(f"{name} · {'Starting' if start else 'Stopping'}…")

        def done(_r) -> None:
            row.set_sensitive(True)
            self.ctx.toast(f"{name} {'started' if start else 'stopped'}")
            self._refresh_services(force=True)

        def failed(exc: BaseException) -> None:
            row.set_sensitive(True)
            self.ctx.toast(f"{name}: {exc}")
            self._refresh_services(force=True)

        run_in_thread(self.ctx.vm.set_sql_service, name, start, on_done=done, on_error=failed, name="sql-service")

    def _all_services(self, start: bool) -> None:
        names = [s["name"] for s in self._services]
        if not names:
            self.ctx.toast("No SQL Server services known yet")
            return

        def work() -> None:
            for name in names:
                self.ctx.vm.set_sql_service(name, start)

        run_in_thread(
            work,
            on_done=lambda _r: (self.ctx.toast("All instances " + ("started" if start else "stopped")), self._refresh_services(force=True)),
            on_error=lambda e: (self.ctx.toast(str(e)), self._refresh_services(force=True)),
            name="sql-services-all",
        )

    # -- mounts -------------------------------------------------------------
    def _refresh_mounts(self) -> None:
        for row in self._mount_rows:
            self.mounts_group.remove(row)
        self._mount_rows.clear()
        mounts = self.ctx.mounter.mounts()
        if not mounts:
            row = Adw.ActionRow(use_markup=False, title="Nothing attached", subtitle="Attach a VHD on the Images and Clones page to restore from it.")
            self.mounts_group.add(row)
            self._mount_rows.append(row)
            return
        for m in mounts:
            dbs = ", ".join(d.name for d in m.databases) or "no databases yet"
            letters = ", ".join(f"{x}:" for x in m.drive_letters) or m.target_dev
            row = Adw.ActionRow(use_markup=False, title=m.label, subtitle=f"{letters} · {'direct' if m.is_direct else 'clone'} · {dbs}")
            row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
            self.mounts_group.add(row)
            self._mount_rows.append(row)

    def on_shown(self) -> None:
        self._refresh_mounts()
        self._refresh_services()
