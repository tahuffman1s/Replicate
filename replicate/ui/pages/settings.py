"""Settings: the VM, images and clones, monitoring, appearance, host checks."""

from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from replicate import paths  # noqa: E402
from replicate.host import preflight  # noqa: E402
from replicate.tasks import Progress, run_in_thread  # noqa: E402
from replicate.ui.context import AppContext  # noqa: E402
from replicate.ui.dialogs.create_vm import CreateVmDialog  # noqa: E402
from replicate.ui.widgets.common import confirm, file_chooser, text_button  # noqa: E402
from replicate.vm.manager import VIRTIO_WIN_URL, WINDOWS_ISO_PAGE  # noqa: E402


class SettingsPage(Adw.PreferencesPage):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        vm = ctx.config.vm
        self.set_title("Settings")

        # -- VM ------------------------------------------------------------
        g = Adw.PreferencesGroup(title="SQL host VM")
        g.set_description("Changes to size and name apply when the VM is created or recreated.")
        self._entry(g, "VM name", vm.name, lambda v: setattr(vm, "name", v.strip() or "replicate-sql"))
        self._spin(g, "Memory (MiB)", vm.memory_mib, 2048, 262144, 1024, lambda v: setattr(vm, "memory_mib", int(v)))
        self._spin(g, "Virtual CPUs", vm.vcpus, 1, 128, 1, lambda v: setattr(vm, "vcpus", int(v)))
        self._entry(g, "System disk size (e.g. 100G)", vm.disk_size, lambda v: setattr(vm, "disk_size", v.strip() or "100G"))
        self._entry(g, "SQL Server instances (comma separated; MSSQLSERVER is the default instance)", ", ".join(vm.sql_instances),
                    lambda v: setattr(vm, "sql_instances", [x.strip() for x in v.split(",") if x.strip()]))
        self._entry(g, "SQL login Replicate connects with", vm.admin_login, lambda v: setattr(vm, "admin_login", v.strip() or "replicate"))
        self._entry(g, "Windows account name", vm.windows_user, lambda v: setattr(vm, "windows_user", v.strip() or "replicate"))
        self._switch(g, "Hyper-V enlightenments", "Faster Windows guest; turn off only if the VM misbehaves", vm.hyperv_enlightenments, lambda v: setattr(vm, "hyperv_enlightenments", v))
        self.add(g)

        g = Adw.PreferencesGroup(title="Installation media and storage")
        self.windows_row = self._file_row(g, "Windows 11 ISO", vm.windows_iso, lambda v: setattr(vm, "windows_iso", v), f"Download from {WINDOWS_ISO_PAGE}")
        self.virtio_row = self._file_row(g, "virtio-win ISO", vm.virtio_win_iso, lambda v: setattr(vm, "virtio_win_iso", v), "Drivers and the guest agent for Windows")
        dl = text_button("Download", self._download_virtio)
        self.virtio_row.add_suffix(dl)
        self._entry(g, "libvirt connection URI", vm.uri, lambda v: setattr(vm, "uri", v.strip() or "qemu:///system"))
        self._entry(g, "libvirt network", vm.network, lambda v: setattr(vm, "network", v.strip() or "default"))
        self.images_row = self._folder_row(g, "VM disk directory", vm.images_dir, lambda v: setattr(vm, "images_dir", v),
                                           "Where the system disk goes; the default needs an administrator password once")
        self.add(g)

        # -- VHD -----------------------------------------------------------
        g = Adw.PreferencesGroup(title="Images and clones")
        self._folder_row(g, "Clones directory", ctx.config.vhd.clones_dir, self._set_clones_dir,
                         "qcow2 overlays live here; the libvirt qemu user must be able to reach it")
        combo = Adw.ComboRow(title="Restore .bak files onto", subtitle="Default for the restore dialog")
        combo.set_model(Gtk.StringList.new(["The instance's default directories", "The clone disk itself"]))
        combo.set_selected(1 if ctx.config.vhd.restore_target == "clone" else 0)
        combo.connect("notify::selected", lambda c, _p: self._save(lambda: setattr(ctx.config.vhd, "restore_target", "clone" if c.get_selected() == 1 else "default")))
        g.add(combo)
        self.add(g)

        # -- Monitoring ----------------------------------------------------
        g = Adw.PreferencesGroup(title="Monitoring and tracing")
        m = ctx.config.monitor
        self._spin(g, "Sample interval (seconds)", m.interval_seconds, 1, 60, 1, lambda v: setattr(m, "interval_seconds", float(v)))
        self._spin(g, "History kept (samples)", m.history_points, 60, 7200, 60, lambda v: setattr(m, "history_points", int(v)))
        self._spin(g, "Trace rows kept", m.trace_max_rows, 100, 50000, 100, lambda v: setattr(m, "trace_max_rows", int(v)))
        self._entry(g, "Applications left out of the trace (comma separated)", ", ".join(m.trace_exclude_apps),
                    lambda v: setattr(m, "trace_exclude_apps", [x.strip() for x in v.split(",") if x.strip()]))
        self.add(g)

        # -- Appearance ----------------------------------------------------
        g = Adw.PreferencesGroup(title="Appearance")
        scheme = Adw.ComboRow(title="Colour scheme")
        scheme.set_model(Gtk.StringList.new(["Follow the system", "Light", "Dark"]))
        scheme.set_selected({"light": 1, "dark": 2}.get(ctx.config.ui.color_scheme, 0))

        def on_scheme(c, _p):
            ctx.config.ui.color_scheme = ["default", "light", "dark"][c.get_selected()]
            ctx.save_config()
            Gtk.Application.get_default().apply_color_scheme()

        scheme.connect("notify::selected", on_scheme)
        g.add(scheme)
        self.add(g)

        # -- Host ----------------------------------------------------------
        g = Adw.PreferencesGroup(title="This computer")
        self.host_row = Adw.ActionRow(use_markup=False, title="Checking…")
        g.add(self.host_row)
        g.add(Adw.ActionRow(use_markup=False, title="Configuration file", subtitle=str(paths.CONFIG_FILE), subtitle_selectable=True))
        g.add(Adw.ActionRow(use_markup=False, title="Passwords kept in", subtitle="the desktop keyring" if ctx.secrets.backend == "keyring" else str(paths.SECRETS_FILE)))
        g.add(Adw.ActionRow(use_markup=False, title="Log file", subtitle=str(paths.LOG_DIR / "replicate.log"), subtitle_selectable=True))
        again = text_button("Check again", self._check_host)
        g.set_header_suffix(again)
        self.add(g)

        # -- Danger --------------------------------------------------------
        g = Adw.PreferencesGroup(title="Start over")
        row = Adw.ActionRow(use_markup=False, title="Recreate the VM", subtitle="Erases the current VM and installs Windows and SQL Server again with the settings above")
        row.add_suffix(text_button("Recreate…", lambda: CreateVmDialog(ctx, recreate=True).present(self.get_root()), css="destructive-action"))
        g.add(row)
        row = Adw.ActionRow(use_markup=False, title="Delete the VM", subtitle="Removes the libvirt domain and, if you choose, its disk")
        row.add_suffix(text_button("Delete…", self._delete_vm, css="destructive-action"))
        g.add(row)
        self.add(g)
        self._check_host()

    # -- row helpers ---------------------------------------------------------
    def _save(self, apply) -> None:
        apply()
        self.ctx.save_config()

    def _entry(self, group, title, value, setter) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title, text=str(value))
        row.connect("apply", lambda r: self._save(lambda: setter(r.get_text())))
        row.set_show_apply_button(True)
        group.add(row)
        return row

    def _spin(self, group, title, value, lo, hi, step, setter) -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        row.set_value(value)
        row.connect("notify::value", lambda r, _p: self._save(lambda: setter(r.get_value())))
        group.add(row)
        return row

    def _switch(self, group, title, subtitle, value, setter) -> Adw.SwitchRow:
        row = Adw.SwitchRow(use_markup=False, title=title, subtitle=subtitle, active=value)
        row.connect("notify::active", lambda r, _p: self._save(lambda: setter(r.get_active())))
        group.add(row)
        return row

    def _file_row(self, group, title, value, setter, subtitle="") -> Adw.ActionRow:
        row = Adw.ActionRow(use_markup=False, title=title, subtitle=value or subtitle)
        row.set_subtitle_selectable(True)

        def chosen(path: str) -> None:
            self._save(lambda: setter(path))
            row.set_subtitle(path)

        row.add_suffix(text_button("Choose…", lambda: file_chooser(self, title, chosen, filters=[("ISO images", ["*.iso"]), ("All files", ["*"])])))
        group.add(row)
        return row

    def _folder_row(self, group, title, value, setter, subtitle="") -> Adw.ActionRow:
        row = Adw.ActionRow(use_markup=False, title=title, subtitle=f"{value}\n{subtitle}" if subtitle else value)
        row.set_subtitle_selectable(True)

        def chosen(path: str) -> None:
            self._save(lambda: setter(path))
            row.set_subtitle(f"{path}\n{subtitle}" if subtitle else path)

        row.add_suffix(text_button("Choose…", lambda: file_chooser(self, title, chosen, folder=True)))
        group.add(row)
        return row

    def _set_clones_dir(self, path: str) -> None:
        self.ctx.config.vhd.clones_dir = path
        self.ctx.mounter.clones_dir = path

    # -- actions -------------------------------------------------------------
    def _download_virtio(self) -> None:
        dest = Path(self.ctx.config.vm.images_dir if os.access(self.ctx.config.vm.images_dir, os.W_OK) else paths.DATA_DIR) / "virtio-win.iso"
        self.ctx.toast("Downloading virtio-win.iso…")
        progress = Progress(lambda msg, frac: self.virtio_row.set_subtitle(msg))

        def done(path: Path) -> None:
            self.ctx.config.vm.virtio_win_iso = str(path)
            self.ctx.save_config()
            self.virtio_row.set_subtitle(str(path))
            self.ctx.toast("virtio-win.iso is ready")

        run_in_thread(self.ctx.vm.download_virtio_win, dest, progress, on_done=done, on_error=lambda e: self.ctx.toast(f"Download failed: {e} ({VIRTIO_WIN_URL})"), name="virtio-dl")

    def _check_host(self) -> None:
        self.host_row.set_title("Checking…")
        self.host_row.set_subtitle("")

        def done(result) -> None:
            if result.ok:
                self.host_row.set_title("Ready: libvirt, qemu-img, KVM and an ISO tool are available")
            else:
                self.host_row.set_title("Something is missing")
            self.host_row.set_subtitle("\n".join(result.problems + result.notes))

        run_in_thread(preflight, self.ctx.config.vm.uri, on_done=done, on_error=lambda e: self.host_row.set_title(str(e)), name="preflight")

    def _delete_vm(self) -> None:
        check = Gtk.CheckButton(label="Also delete the system disk and setup CDs", active=False)
        name = self.ctx.config.vm.name

        def go() -> None:
            run_in_thread(self.ctx.vm.delete, check.get_active(), on_done=lambda _r: (self.ctx.toast(f"{name} deleted"), self.get_root()._poll_status()),
                          on_error=lambda e: self.ctx.toast(f"Delete failed: {e}"), name="vm-delete")

        confirm(self, f"Delete the VM {name}?", "The VM is powered off and removed from libvirt. Attached clones stay on disk.", "Delete", go, extra=check)
