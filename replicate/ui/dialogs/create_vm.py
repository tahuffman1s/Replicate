"""Create the SQL host VM: confirm the settings, set the Windows password, go."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from replicate.tasks import Progress, run_in_thread  # noqa: E402
from replicate.ui.context import INSTALL_PROGRESS, AppContext  # noqa: E402
from replicate.ui.widgets.common import dialog_header, error_dialog  # noqa: E402
from replicate.vm.manager import CreateOptions  # noqa: E402


class CreateVmDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext, *, recreate: bool = False):
        super().__init__(title="Create the SQL host VM", content_width=520)
        self.ctx = ctx
        self.recreate = recreate
        vm = ctx.config.vm

        view = Adw.ToolbarView()
        header, self.create_button = dialog_header(
            self, "Recreate" if recreate else "Create", self._on_create, destructive=recreate
        )
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        summary = Adw.PreferencesGroup(title="What will be created")
        summary.set_description(
            "Windows 11 installs unattended, then SQL Server 2022 Developer with the instances below. "
            "Everything runs in the background; the VM keeps installing even if you close Replicate."
        )
        for title, value in (
            ("VM name", vm.name),
            ("Memory", f"{vm.memory_mib} MiB"),
            ("vCPUs", str(vm.vcpus)),
            ("System disk", f"{vm.disk_size} (grows as used) in {vm.images_dir}"),
            ("SQL Server instances", ", ".join(vm.sql_instances)),
            ("Windows account", vm.windows_user),
            ("Windows ISO", vm.windows_iso or "not set"),
            ("virtio-win ISO", vm.virtio_win_iso or "not set"),
        ):
            row = Adw.ActionRow(use_markup=False, title=title, subtitle=value)
            row.set_subtitle_selectable(True)
            summary.add(row)
        change = Gtk.Button(label="Change in Settings", valign=Gtk.Align.CENTER)
        change.connect("clicked", self._go_settings)
        summary.set_header_suffix(change)
        page.add(summary)

        creds = Adw.PreferencesGroup(title="Windows password")
        creds.set_description(
            f"For the {vm.windows_user} account inside the VM. Kept in your keyring; "
            "you need it only if you sign in to the Windows desktop yourself."
        )
        self.password = Adw.PasswordEntryRow(title="Password")
        self.confirm = Adw.PasswordEntryRow(title="Again")
        creds.add(self.password)
        creds.add(self.confirm)
        page.add(creds)

        if recreate:
            warning = Adw.PreferencesGroup()
            warning.add(Adw.ActionRow(use_markup=False, title="This erases the current VM", subtitle="Its disk and everything inside Windows are deleted before the new install starts.", icon_name="dialog-warning-symbolic"))
            page.add(warning)

    def _go_settings(self, *_args) -> None:
        window = self.get_root()
        self.close()
        if window is not None and hasattr(window, "show_page"):
            window.show_page("settings")

    def _on_create(self, *_args) -> None:
        password = self.password.get_text()
        if len(password) < 4:
            error_dialog(self, "Choose a password", "The Windows account needs a password of at least 4 characters.")
            return
        if password != self.confirm.get_text():
            error_dialog(self, "Passwords differ", "The two passwords are not the same.")
            return
        problems = self.ctx.vm.validate_for_create()
        if problems:
            error_dialog(self, "Not ready to create the VM", "\n\n".join(problems))
            return
        options = CreateOptions(windows_password=password, recreate=self.recreate)
        start_install(self.ctx, options)
        self.close()


def start_install(ctx: AppContext, options: CreateOptions) -> None:
    progress = Progress(lambda message, fraction: ctx.emit(INSTALL_PROGRESS, message, fraction))

    def done(_result) -> None:
        ctx.toast("The SQL host VM is ready")
        ctx.emit(INSTALL_PROGRESS, "Ready", 1.0)
        from replicate.ui.context import SERVERS_CHANGED

        ctx.emit(SERVERS_CHANGED)

    def failed(exc: BaseException) -> None:
        ctx.emit(INSTALL_PROGRESS, f"Failed: {exc}", -1.0)
        ctx.toast(f"VM creation failed: {exc}")

    run_in_thread(ctx.vm.create, options, progress, on_done=done, on_error=failed, name="vm-create")
    ctx.toast("Creating the VM in the background")


def resume_install(ctx: AppContext) -> None:
    progress = Progress(lambda message, fraction: ctx.emit(INSTALL_PROGRESS, message, fraction))

    def done(_result) -> None:
        ctx.toast("The SQL host VM is ready")
        ctx.emit(INSTALL_PROGRESS, "Ready", 1.0)
        from replicate.ui.context import SERVERS_CHANGED

        ctx.emit(SERVERS_CHANGED)

    def failed(exc: BaseException) -> None:
        ctx.emit(INSTALL_PROGRESS, f"Failed: {exc}", -1.0)

    run_in_thread(ctx.vm.resume_install, progress, on_done=done, on_error=failed, name="vm-resume")
