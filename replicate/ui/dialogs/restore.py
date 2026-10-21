"""Attach an image to the VM, then restore or attach the databases on it."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from replicate.config import Mount, RestoredDatabase, SqlServer  # noqa: E402
from replicate.sql.connection import SqlError  # noqa: E402
from replicate.tasks import run_in_thread  # noqa: E402
from replicate.ui.context import MOUNTS_CHANGED, AppContext  # noqa: E402
from replicate.ui.widgets.common import dialog_header, error_dialog, icon_button, wrap_label  # noqa: E402
from replicate.util import human_size  # noqa: E402
from replicate.vhd import restore as rs  # noqa: E402
from replicate.vhd.images import ImageInfo  # noqa: E402

log = logging.getLogger(__name__)


class AttachDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext, image: ImageInfo):
        super().__init__(title="Attach image", content_width=520)
        self.ctx = ctx
        self.image = image
        view = Adw.ToolbarView()
        header, self.go = dialog_header(self, "Attach", self._on_attach)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        info = Adw.PreferencesGroup(title=image.name)
        info.set_description(f"{image.format_label} · {human_size(image.virtual_size)} virtual, {human_size(image.actual_size)} on disk\n{image.path}")
        page.add(info)

        how = Adw.PreferencesGroup(title="How")
        self.targets: list[tuple[str, str]] = [("vm", f"The VM ({ctx.config.vm.name})")]
        for name in sorted(ctx.containers.status_map()):
            self.targets.append((name, f"Container {name}"))
        self.target = Adw.ComboRow(title="Attach to")
        self.target.set_model(Gtk.StringList.new([label for _, label in self.targets]))
        if not ctx.last_status.defined and len(self.targets) > 1:
            self.target.set_selected(1)
        how.add(self.target)
        self.mode = Adw.ComboRow(title="Attach as")
        self.mode.set_model(Gtk.StringList.new(["Copy-on-write clone", "The image itself"]))
        self.mode.set_subtitle("A clone is a small qcow2 file on top of the image, which stays untouched; attaching the image itself writes into it.")
        self.mode.connect("notify::selected", lambda *_: self._sync())
        how.add(self.mode)
        self.label = Adw.EntryRow(title="Label", text=image.name.rsplit(".", 1)[0])
        how.add(self.label)
        self.readonly = Adw.SwitchRow(use_markup=False, title="Read-only", subtitle="Windows can read the files but SQL Server cannot attach them writable")
        how.add(self.readonly)
        page.add(how)
        if image.is_differencing:
            warn = Adw.PreferencesGroup()
            warn.add(wrap_label(
                f"This is a differencing disk whose parent is {image.backing_file}. QEMU cannot follow a VHDX parent "
                "chain; merge it in Windows (Merge-VHD) or attach the parent image instead.", css=["warning"]))
            page.add(warn)

        self._sync()

    def _sync(self) -> None:
        direct = self.mode.get_selected() == 1
        self.readonly.set_sensitive(direct)
        if not direct:
            self.readonly.set_active(False)

    def _on_attach(self, *_args) -> None:
        self.go.set_sensitive(False)
        clone = self.mode.get_selected() == 0
        label = self.label.get_text().strip()
        readonly = self.readonly.get_active()
        target = self.targets[self.target.get_selected()][0]
        parent = self.get_root() or self.ctx_window()

        def failed(exc: BaseException) -> None:
            self.go.set_sensitive(True)
            error_dialog(self, "Could not attach", str(exc))

        if target == "vm":
            def done(mount: Mount) -> None:
                self.ctx.emit(MOUNTS_CHANGED)
                self.close()
                status = self.ctx.last_status
                if status.running and status.agent:
                    RestoreDialog(self.ctx, mount).present(parent)
                else:
                    self.ctx.toast("Attached. Start the VM, then use Restore on the attached image.")

            run_in_thread(self.ctx.mounter.attach, self.image, clone=clone, label=label, readonly=readonly, on_done=done, on_error=failed, name="attach")
            return

        self.ctx.toast("Mounting the image on this computer; your password may be asked for")

        def done_docker(result) -> None:
            mount, files = result
            self.ctx.emit(MOUNTS_CHANGED)
            self.close()
            RestoreDialog(self.ctx, mount, files=files).present(parent)

        run_in_thread(self.ctx.docker_mounter.attach, self.image, target, clone=clone, label=label, readonly=readonly, on_done=done_docker, on_error=failed, name="attach-docker")

    def ctx_window(self):
        from gi.repository import Gtk as _Gtk

        app = _Gtk.Application.get_default()
        return app.get_active_window() if app else None


class RestoreDialog(Adw.Dialog):
    """Bring the disk online in Windows, list what is on it, create databases."""

    def __init__(self, ctx: AppContext, mount: Mount, files: list[dict] | None = None):
        super().__init__(title=f"Restore from {mount.label}", content_width=680, content_height=640)
        self.ctx = ctx
        self.mount = mount
        self.docker = mount.backend == "docker"
        self.files: list[dict] = files or []
        self.attach_plans: list[rs.AttachPlan] = []
        self.backup_plans: list[rs.BackupPlan] = []
        self.plan_rows: list[tuple[Adw.ActionRow, Gtk.CheckButton, Gtk.Entry, object]] = []

        view = Adw.ToolbarView()
        header, self.go = dialog_header(self, "Restore selected", self._on_go)
        view.add_top_bar(header)
        self.page = Adw.PreferencesPage()
        view.set_content(self.page)
        self.set_child(view)

        self.status_group = Adw.PreferencesGroup(title="Disk")
        self.status_row = Adw.ActionRow(use_markup=False, title="Bringing the disk online in Windows…")
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        self.status_row.add_prefix(self.spinner)
        self.status_group.add(self.status_row)
        self.page.add(self.status_group)

        self.target_group = Adw.PreferencesGroup(title="Target")
        self.server_row = Adw.ComboRow(title="SQL Server instance")
        if self.docker:
            self.servers = [s for s in ctx.registry.servers if s.in_docker and s.container == mount.container]
            self.server_row.set_subtitle(f"The container {mount.container} this image is mounted for")
        else:
            self.servers = [s for s in ctx.registry.servers if s.in_vm] or list(ctx.registry.servers)
            self.server_row.set_subtitle("Only an instance inside the VM can see the attached disk")
        self.server_row.set_model(Gtk.StringList.new([s.name for s in self.servers] or ["No servers configured"]))
        self.target_group.add(self.server_row)
        self.replace = Adw.SwitchRow(use_markup=False, title="Replace a database that already has the same name", subtitle="Drops it first; off by default")
        self.target_group.add(self.replace)
        self.onto_clone = Adw.SwitchRow(use_markup=False, title="Restore .bak files onto this disk", subtitle="Data files go next to the backup instead of the instance's default directories")
        self.onto_clone.set_active(ctx.config.vhd.restore_target == "clone")
        self.onto_clone.set_visible(not self.docker)
        self.target_group.add(self.onto_clone)
        self.copy_in = Adw.SwitchRow(use_markup=False, title="Copy the files into the container", subtitle="Instead of using them where they are mounted; done by itself when SQL Server cannot open them there")
        self.copy_in.set_visible(self.docker)
        self.target_group.add(self.copy_in)
        self.page.add(self.target_group)

        self.plans_group = Adw.PreferencesGroup(title="Databases found")
        self.plans_group.set_description("Tick what to bring in; rename as needed. Data files (.mdf) are attached in place; backups (.bak) are restored.")
        self.page.add(self.plans_group)

        self.log_group = Adw.PreferencesGroup(title="Progress")
        self.log_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.log_label.add_css_class("install-log")
        self.log_label.add_css_class("card")
        self.log_group.add(self.log_label)
        self.log_group.set_visible(False)
        self.page.add(self.log_group)

        self.go.set_sensitive(False)
        refresh = icon_button("view-refresh-symbolic", "Look at the disk again", self._online)
        header.pack_end(refresh)
        self._online()

    def _rescan_docker(self) -> None:
        self.spinner.stop()
        self.spinner.set_visible(False)
        if getattr(self, "_scanned_once", False) or not self.files:
            self.files = self.ctx.docker_mounter.files(self.mount)
        self._scanned_once = True
        where = ", ".join(self.mount.drive_letters) or "?"
        self.status_row.set_title(f"Mounted for {self.mount.container} at {where}")
        self.status_row.set_subtitle(f"{len(self.files)} database file{'s' if len(self.files) != 1 else ''} found; the container sees them under /mnt/replicate")
        self._build_plans()

    # -- disk -------------------------------------------------------------
    def _online(self) -> None:
        if self.docker:
            self._rescan_docker()
            return
        self.spinner.set_visible(True)
        self.spinner.start()
        self.status_row.set_title("Bringing the disk online in Windows…")
        self.status_row.set_subtitle("")

        def done(data: dict) -> None:
            self.spinner.stop()
            self.spinner.set_visible(False)
            letters = ", ".join(f"{x}:" for x in data.get("letters", [])) or "no drive letter"
            vols = "; ".join(
                f"{v.get('letter')}: {v.get('label') or 'no label'} {human_size(v.get('sizeBytes'))} ({human_size(v.get('freeBytes'))} free)"
                for v in data.get("volumes", [])
            )
            self.status_row.set_title(f"Online as {letters} (disk {data.get('diskNumber')})")
            self.status_row.set_subtitle(vols or f"{data.get('partitionStyle', '?')} disk, {human_size(data.get('size'))}")
            self.files = data.get("files", [])
            self._build_plans()
            self.ctx.emit(MOUNTS_CHANGED)

        def failed(exc: BaseException) -> None:
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.status_row.set_title("Windows did not bring the disk online")
            self.status_row.set_subtitle(str(exc))

        run_in_thread(self.ctx.mounter.online, self.mount, on_done=done, on_error=failed, name="online")

    def _build_plans(self) -> None:
        for row, *_ in self.plan_rows:
            self.plans_group.remove(row)
        self.plan_rows.clear()
        self.attach_plans = rs.plan_attachments(self.files)
        self.backup_plans = rs.plan_backups(self.files)
        already = {d.name.lower() for d in self.mount.databases}
        def shown(path: str) -> str:
            if not self.docker:
                return path
            try:
                return self.ctx.docker_mounter.container_path(self.mount, path)
            except Exception:  # noqa: BLE001
                return path

        for plan in self.attach_plans:
            self._add_plan_row(plan, "Attach", "\n".join(shown(f) for f in plan.files), plan.database.lower() not in already)
        for plan in self.backup_plans:
            size = next((f.get("sizeBytes", 0) for f in self.files if f["path"] == plan.backup_file), 0)
            self._add_plan_row(plan, "Restore", f"{shown(plan.backup_file)} ({human_size(size)})", plan.database.lower() not in already)
        if not self.plan_rows:
            row = Adw.ActionRow(use_markup=False, title="No database files found", subtitle="Looked for .mdf/.ldf/.ndf and .bak up to four folders deep.")
            self.plans_group.add(row)
            self.plan_rows.append((row, Gtk.CheckButton(), Gtk.Entry(), None))
        self.go.set_sensitive(bool(self.attach_plans or self.backup_plans) and bool(self.servers))

    def _add_plan_row(self, plan, kind: str, detail: str, checked: bool) -> None:
        row = Adw.ActionRow(use_markup=False, title=f"{kind}: {plan.database}", subtitle=detail)
        row.set_subtitle_lines(3)
        check = Gtk.CheckButton(active=checked, valign=Gtk.Align.CENTER)
        row.add_prefix(check)
        name = Gtk.Entry(text=plan.database, valign=Gtk.Align.CENTER, width_chars=18, tooltip_text="Name on the server")
        row.add_suffix(name)
        self.plans_group.add(row)
        self.plan_rows.append((row, check, name, plan))

    # -- go ---------------------------------------------------------------
    def _log(self, text: str) -> None:
        self.log_group.set_visible(True)
        current = self.log_label.get_label()
        self.log_label.set_label((current + "\n" if current else "") + text)

    def _on_go(self, *_args) -> None:
        if not self.servers:
            return
        server: SqlServer = self.servers[self.server_row.get_selected()]
        selected = [(plan, entry.get_text().strip() or plan.database) for _row, check, entry, plan in self.plan_rows if plan is not None and check.get_active()]
        if not selected:
            self.ctx.toast("Nothing selected")
            return
        replace = self.replace.get_active()
        onto_clone = self.onto_clone.get_active() and not self.docker
        copy_in = self.copy_in.get_active() and self.docker
        docker_mode = self.docker
        mount = self.mount
        self.go.set_sensitive(False)
        self.log_label.set_label("")

        def work() -> list[tuple[str, str, str | None]]:
            from replicate.tasks import on_main

            results = []
            docker = self.ctx.containers.docker if docker_mode else None

            def inside(path: str) -> str:
                return self.ctx.docker_mounter.container_path(mount, path) if docker_mode else path

            def run_attach(plan: rs.AttachPlan, name: str, *, copied: bool) -> list[str]:
                if docker_mode and copied:
                    on_main(self._log, "  copying the files into the container")
                    paths = rs.copy_into_container(docker, mount.container, plan.files, name)
                else:
                    paths = [inside(f) for f in plan.files]
                rs.attach_database(conn, rs.AttachPlan(plan.database, paths), name, replace=replace)
                return paths

            def run_restore(plan: rs.BackupPlan, name: str, *, copied: bool) -> None:
                backup = plan.backup_file
                if docker_mode and copied:
                    on_main(self._log, "  copying the backup into the container")
                    backup = rs.copy_into_container(docker, mount.container, [plan.backup_file], name)[0]
                elif docker_mode:
                    backup = inside(backup)
                target_dir = None
                if onto_clone:
                    from pathlib import PureWindowsPath

                    target_dir = str(PureWindowsPath(plan.backup_file).parent)
                rs.restore_database(conn, rs.BackupPlan(plan.database, backup), name, target_dir=target_dir, replace=replace)

            with self.ctx.registry.connect(server, timeout=20) as conn:
                for plan, name in selected:
                    on_main(self._log, f"{plan.source}: {name} …")
                    try:
                        if isinstance(plan, rs.AttachPlan):
                            try:
                                files = run_attach(plan, name, copied=copy_in)
                            except SqlError as exc:
                                if docker_mode and not copy_in and rs.looks_like_io_refusal(str(exc)):
                                    on_main(self._log, "  SQL Server cannot open the files where they are mounted; copying instead")
                                    files = run_attach(plan, name, copied=True)
                                else:
                                    raise
                        else:
                            try:
                                run_restore(plan, name, copied=copy_in)
                            except SqlError as exc:
                                if docker_mode and not copy_in and rs.looks_like_io_refusal(str(exc)):
                                    on_main(self._log, "  SQL Server cannot read the backup where it is mounted; copying instead")
                                    run_restore(plan, name, copied=True)
                                else:
                                    raise
                            files = rs.database_files(conn, name)
                        self.mount.databases.append(RestoredDatabase(server_id=server.id, name=name, mode=plan.source, files=files))
                        self.ctx.save_state()
                        results.append((name, "ok", None))
                        on_main(self._log, f"  ✓ {name}")
                    except SqlError as exc:
                        results.append((name, "failed", str(exc)))
                        on_main(self._log, f"  ✗ {name}: {exc}")
            return results

        def done(results) -> None:
            self.go.set_sensitive(True)
            ok = sum(1 for _, status, _ in results if status == "ok")
            self.ctx.toast(f"{ok} of {len(results)} database{'s' if len(results) != 1 else ''} ready on {server.name}")
            self.ctx.emit(MOUNTS_CHANGED)
            self._build_plans()

        def failed(exc: BaseException) -> None:
            self.go.set_sensitive(True)
            self._log(f"✗ {exc}")

        run_in_thread(work, on_done=done, on_error=failed, name="restore")


def detach_mount(ctx: AppContext, mount: Mount, *, drop_databases: bool, delete_clone: bool, on_done, on_error) -> None:
    """Detach the databases that came from the disk, then the disk itself."""

    def work() -> None:
        if drop_databases and mount.databases:
            by_server: dict[str, list[RestoredDatabase]] = {}
            for db in mount.databases:
                by_server.setdefault(db.server_id, []).append(db)
            for server_id, dbs in by_server.items():
                server = ctx.registry.get(server_id)
                if server is None:
                    continue
                try:
                    with ctx.registry.connect(server, timeout=20) as conn:
                        existing = {n.lower() for n in rs.existing_databases(conn)}
                        for db in dbs:
                            if db.name.lower() not in existing:
                                continue
                            if db.mode == "attach":
                                rs.detach_database(conn, db.name)
                            else:
                                rs.drop_database(conn, db.name)
                except SqlError as exc:
                    log.warning("could not detach databases on %s: %s", server.name, exc)
                    if ctx.last_status.running or mount.backend == "docker":
                        raise
            mount.databases.clear()
            ctx.save_state()
        if mount.backend == "docker":
            ctx.docker_mounter.detach(mount, delete_clone_file=delete_clone)
        else:
            ctx.mounter.detach(mount, delete_clone_file=delete_clone)

    run_in_thread(work, on_done=on_done, on_error=on_error, name="detach")
