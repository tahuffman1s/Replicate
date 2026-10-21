"""Images & Clones: the VHD library, what is attached, and clones on disk."""

from __future__ import annotations

import os
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from replicate.config import Mount  # noqa: E402
from replicate.tasks import run_in_thread  # noqa: E402
from replicate.ui.context import CONFIG_CHANGED, CONTAINERS_CHANGED, MOUNTS_CHANGED, AppContext  # noqa: E402
from replicate.ui.dialogs.restore import AttachDialog, RestoreDialog, detach_mount  # noqa: E402
from replicate.ui.dialogs.share import ShareDialog  # noqa: E402
from replicate.vhd import shares as sh  # noqa: E402
from replicate.ui.widgets.common import confirm, file_chooser, icon_button, status_label, status_page, text_button  # noqa: E402
from replicate.util import human_size  # noqa: E402
from replicate.vhd import clones as cl  # noqa: E402
from replicate.vhd.images import ImageInfo, ScanReport, scan  # noqa: E402


class ImagesPage(Gtk.ScrolledWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.images: list[ImageInfo] = []
        self._scanning = False
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.switcher = Gtk.Stack()
        self.set_child(self.switcher)
        self.empty = status_page(
            "drive-multidisk-symbolic",
            "No images yet",
            "Add the folder your VHD or VHDX images live in, or open a single image. "
            "Replicate attaches copy-on-write clones of them to the VM and restores the databases on them.",
            text_button("Add Folder", self._on_add_folder, css="suggested-action"),
            text_button("Open a File…", self._on_open_file),
        )
        self.switcher.add_named(self.empty, "empty")
        clamp = Adw.Clamp(maximum_size=960, tightening_threshold=760)
        self.switcher.add_named(clamp, "content")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.add_css_class("page-content")
        clamp.set_child(box)

        # Attached
        self.mounts_group = Adw.PreferencesGroup(title="Attached to the VM")
        self.mounts_group.set_description("Disks plugged into the VM right now. Restore from them, or detach them when done.")
        box.append(self.mounts_group)
        self.mount_rows: list[Gtk.Widget] = []

        # Folders
        self.folders_group = Adw.PreferencesGroup(title="Image folders and shares")
        self.folders_group.set_description("Where Replicate looks for images, in every folder underneath. A network share is mounted by Replicate so the VM and containers can read it too.")
        header_box = Gtk.Box(spacing=6)
        header_box.append(text_button("Add share…", self._on_add_share))
        header_box.append(text_button("Add folder", self._on_add_folder))
        self.folders_group.set_header_suffix(header_box)
        box.append(self.folders_group)
        self.folder_rows: list[Gtk.Widget] = []

        # Images
        self.images_group = Adw.PreferencesGroup(title="Images")
        header = Gtk.Box(spacing=6)
        header.append(text_button("Open a file…", self._on_open_file))
        self.rescan_button = text_button("Rescan", self.rescan)
        header.append(self.rescan_button)
        self.images_group.set_header_suffix(header)
        box.append(self.images_group)
        self.image_rows: list[Gtk.Widget] = []

        # Clones
        self.clones_group = Adw.PreferencesGroup(title="Clones on disk")
        self.clones_group.set_description("Copy-on-write files Replicate made. Ones not attached can be deleted.")
        box.append(self.clones_group)
        self.clone_rows: list[Gtk.Widget] = []

        ctx.subscribe(MOUNTS_CHANGED, self.refresh_mounts)
        ctx.subscribe(CONTAINERS_CHANGED, self.refresh_mounts)
        ctx.subscribe(CONFIG_CHANGED, self._refresh_folders)
        self._refresh_folders()
        self.refresh_mounts()
        self.rescan()

    def on_shown(self) -> None:
        self.refresh_mounts()
        self.refresh_clones()

    # -- folders ------------------------------------------------------------
    def _update_empty(self) -> None:
        has_anything = bool(self.ctx.config.vhd.image_dirs or self.ctx.mounter.mounts() or self.ctx.docker_mounter.mounts() or cl.list_clones(self.ctx.config.vhd.clones_dir))
        self.switcher.set_visible_child_name("content" if has_anything else "empty")

    def on_refresh(self) -> None:
        self.rescan()

    def on_add(self) -> None:
        self._on_add_folder()

    def _refresh_folders(self) -> None:
        self._update_empty()
        for row in self.folder_rows:
            self.folders_group.remove(row)
        self.folder_rows.clear()
        dirs = self.ctx.config.vhd.image_dirs
        shares = self.ctx.config.vhd.shares
        if not dirs and not shares:
            row = Adw.ActionRow(use_markup=False, title="No folders yet", subtitle="Add the folder your VHD images live in, a network share, or open a single file below.")
            self.folders_group.add(row)
            self.folder_rows.append(row)
        for share in shares:
            mounted = sh.is_mounted(share)
            row = Adw.ActionRow(use_markup=False, title=sh.display_name(share), subtitle=f"{share.url} · {share.auth} · {'mounted' if mounted else 'not mounted'}")
            row.add_prefix(Gtk.Image.new_from_icon_name("network-server-symbolic"))
            row.set_activatable(True)
            row.connect("activated", lambda _r, share=share: ShareDialog(self.ctx, share, on_mounted=lambda _s: self.rescan()).present(self.get_root()))
            if mounted:
                row.add_suffix(text_button("Unmount", lambda share=share: self._unmount_share(share)))
            else:
                row.add_suffix(text_button("Mount", lambda share=share: self._mount_share(share), css="suggested-action"))
            row.add_suffix(icon_button("user-trash-symbolic", "Forget this share", lambda share=share: self._forget_share(share), css="flat"))
            self.folders_group.add(row)
            self.folder_rows.append(row)
        for d in dirs:
            row = Adw.ActionRow(use_markup=False, title=d)
            row.add_suffix(icon_button("user-trash-symbolic", "Forget this folder", lambda d=d: self._remove_folder(d), css="flat"))
            self.folders_group.add(row)
            self.folder_rows.append(row)

    def _on_add_folder(self) -> None:
        def chosen(path: str) -> None:
            if sh.is_smb_uri(path):
                self._on_add_share(path)
                return
            if path not in self.ctx.config.vhd.image_dirs:
                self.ctx.config.vhd.image_dirs.append(path)
                self.ctx.save_config()
                self.rescan()

        file_chooser(self, "Choose an image folder", chosen, folder=True, allow_uri=True)

    def _on_add_share(self, uri: str = "") -> None:
        share = sh.share_from_uri(uri) if uri else None
        dialog = ShareDialog(self.ctx, on_mounted=lambda _s: self.rescan())
        if share is not None:
            dialog.url.set_text(share.url)
            dialog.label.set_text(share.label)
        dialog.present(self.get_root())

    def _mount_share(self, share) -> None:
        password = self.ctx.secrets.get(share.secret_key) or ""
        self.ctx.toast(f"Mounting {sh.display_name(share)}; your password may be asked for")
        run_in_thread(sh.mount_share, share, password, uid=os.getuid(), gid=os.getgid(),
                      on_done=lambda _f: (self._refresh_folders(), self.rescan()),
                      on_error=lambda e: (self.ctx.toast(str(e)[:200]), self._refresh_folders()), name="mount-share")

    def _unmount_share(self, share) -> None:
        run_in_thread(sh.unmount_share, share, on_done=lambda _r: (self._refresh_folders(), self.rescan()),
                      on_error=lambda e: self.ctx.toast(str(e)[:200]), name="umount-share")

    def _forget_share(self, share) -> None:
        def go() -> None:
            def after(_r=None) -> None:
                if share in self.ctx.config.vhd.shares:
                    self.ctx.config.vhd.shares.remove(share)
                self.ctx.secrets.delete(share.secret_key)
                self.ctx.save_config()
                self.rescan()

            if sh.is_mounted(share):
                run_in_thread(sh.unmount_share, share, on_done=after, on_error=lambda e: (self.ctx.toast(str(e)[:200]), after()), name="umount-share")
            else:
                after()

        confirm(self, f"Forget {sh.display_name(share)}?", "It is unmounted and removed from the list; nothing on the share is touched.", "Forget", go)

    def _remove_folder(self, path: str) -> None:
        self.ctx.config.vhd.image_dirs.remove(path)
        self.ctx.save_config()
        self.rescan()

    def _on_open_file(self) -> None:
        def chosen(path: str) -> None:
            from replicate.vhd.images import inspect

            info = inspect(path)
            if info.error:
                self.ctx.toast(f"Cannot read {info.name}: {info.error}")
                return
            AttachDialog(self.ctx, info).present(self.get_root())

        file_chooser(self, "Open a disk image", chosen, filters=[("Disk images", ["*.vhd", "*.vhdx", "*.qcow2", "*.img", "*.raw", "*.vdi", "*.vmdk"]), ("All files", ["*"])])

    # -- images -------------------------------------------------------------
    def rescan(self) -> None:
        if self._scanning:
            self._rescan_pending = True
            return
        self._scanning = True
        self._rescan_pending = False
        self.rescan_button.set_sensitive(False)
        dirs = list(self.ctx.config.vhd.image_dirs)
        for share in self.ctx.config.vhd.shares:
            if sh.is_mounted(share):
                try:
                    dirs.append(sh.folder_path(share))
                except sh.ShareError:
                    pass
        report = ScanReport()
        if dirs:
            self.images_group.set_description("Scanning…")

        def on_progress(folders: int, found: int) -> None:
            from replicate.tasks import on_main

            on_main(self.images_group.set_description, f"Scanning… {folders:,} folders, {found} image{'s' if found != 1 else ''} so far")

        def done(images: list[ImageInfo]) -> None:
            self._scanning = False
            self.rescan_button.set_sensitive(True)
            self.images = images
            self.images_group.set_description(report.describe(len(images)) if dirs else "")
            self._render_images()
            self.refresh_clones()
            if getattr(self, "_rescan_pending", False):
                self.rescan()

        def failed(exc: BaseException) -> None:
            self._scanning = False
            self.rescan_button.set_sensitive(True)
            self.ctx.toast(f"Scan failed: {exc}")

        run_in_thread(scan, dirs, max_depth=self.ctx.config.vhd.scan_depth, report=report, progress=on_progress, on_done=done, on_error=failed, name="scan")

    def _render_images(self) -> None:
        for row in self.image_rows:
            self.images_group.remove(row)
        self.image_rows.clear()
        if not self.images:
            row = Adw.ActionRow(use_markup=False, title="No images found", subtitle="Add a folder above. Hyper-V disks, New-VHD files and qcow2 images all work.")
            self.images_group.add(row)
            self.image_rows.append(row)
            return
        attached_images = {m.image_path for m in self.ctx.mounter.mounts() + self.ctx.docker_mounter.mounts()}
        for img in self.images:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(img.modified)) if img.modified else ""
            where = f"{img.folder}/" if img.folder else ""
            subtitle = f"{where}{img.name} · {img.format_label} · {human_size(img.virtual_size)} virtual · {human_size(img.actual_size)} on disk · {when}"
            if img.error:
                subtitle = f"{img.error}\n{img.path}"
            title = f"{Path(img.folder).name} / {img.name}" if img.folder else img.name
            row = Adw.ActionRow(use_markup=False, title=title, subtitle=subtitle)
            row.set_tooltip_text(img.path)
            row.set_subtitle_lines(2)
            row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
            if img.is_differencing:
                row.add_suffix(status_label("differencing", "warn"))
            if img.path in attached_images:
                row.add_suffix(status_label("attached", "good"))
            attach = text_button("Attach…", lambda img=img: AttachDialog(self.ctx, img).present(self.get_root()))
            attach.set_sensitive(not img.error)
            row.add_suffix(attach)
            self.images_group.add(row)
            self.image_rows.append(row)

    # -- mounts -------------------------------------------------------------
    def refresh_mounts(self) -> None:
        self._update_empty()
        for row in self.mount_rows:
            self.mounts_group.remove(row)
        self.mount_rows.clear()
        mounts = self.ctx.mounter.mounts() + self.ctx.docker_mounter.mounts()
        if not mounts:
            row = Adw.ActionRow(use_markup=False, title="Nothing attached", subtitle="Pick an image below and attach it.")
            self.mounts_group.add(row)
            self.mount_rows.append(row)
        for m in mounts:
            if m.backend == "docker":
                letters = f"container {m.container}: " + (", ".join(m.drive_letters) or "not mounted")
            else:
                letters = ", ".join(f"{x}:" for x in m.drive_letters) or f"{m.target_dev} (not online yet)"
            dbs = ", ".join(f"{d.name} ({d.mode})" for d in m.databases) or "no databases yet"
            row = Adw.ActionRow(use_markup=False, title=m.label, subtitle=f"{letters} · {'direct' if m.is_direct else 'clone'} of {m.image_path}\n{dbs}")
            row.set_subtitle_lines(2)
            row.add_prefix(Gtk.Image.new_from_icon_name("media-removable-symbolic"))
            row.add_suffix(text_button("Restore…", lambda m=m: self._restore(m), css="suggested-action"))
            row.add_suffix(text_button("Detach", lambda m=m: self._detach(m)))
            self.mounts_group.add(row)
            self.mount_rows.append(row)
        self._render_images()

    def _restore(self, mount: Mount) -> None:
        if mount.backend == "docker":
            if self.ctx.containers.status_map().get(mount.container) != "running":
                self.ctx.toast(f"Start the container {mount.container} first (Overview page)")
                return
            RestoreDialog(self.ctx, mount).present(self.get_root())
            return
        status = self.ctx.last_status
        if not (status.running and status.agent):
            self.ctx.toast("Start the VM first; the disk is online only while Windows runs")
            return
        RestoreDialog(self.ctx, mount).present(self.get_root())

    def _detach(self, mount: Mount) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        drop = Gtk.CheckButton(label="Detach or drop the databases that came from it first", active=bool(mount.databases))
        drop.set_sensitive(bool(mount.databases))
        delete = Gtk.CheckButton(label="Delete the clone file", active=not mount.is_direct)
        delete.set_sensitive(not mount.is_direct)
        box.append(drop)
        box.append(delete)

        def go() -> None:
            self.ctx.toast(f"Detaching {mount.label}…")
            detach_mount(
                self.ctx,
                mount,
                drop_databases=drop.get_active(),
                delete_clone=delete.get_active(),
                on_done=lambda _r: (self.ctx.toast(f"{mount.label} detached"), self.ctx.emit(MOUNTS_CHANGED), self.refresh_clones()),
                on_error=lambda e: (self.ctx.toast(f"Detach failed: {e}"), self.ctx.emit(MOUNTS_CHANGED)),
            )

        body = ("The image is unmounted from this computer. Databases still attached from it would go suspect, so detach them too."
                if mount.backend == "docker" else "The disk disappears from Windows. Databases still attached from it would go suspect, so detach them too.")
        confirm(self, f"Detach {mount.label}?", body, "Detach", go, extra=box)

    # -- clones -------------------------------------------------------------
    def refresh_clones(self) -> None:
        for row in self.clone_rows:
            self.clones_group.remove(row)
        self.clone_rows.clear()
        attached = {m.clone_path for m in self.ctx.state.mounts}
        clones = cl.list_clones(self.ctx.config.vhd.clones_dir)
        if not clones:
            row = Adw.ActionRow(use_markup=False, title="No clones", subtitle=f"They are kept in {self.ctx.config.vhd.clones_dir}")
            self.clones_group.add(row)
            self.clone_rows.append(row)
            return
        for c in clones:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created))
            row = Adw.ActionRow(use_markup=False, title=c.label, subtitle=f"{human_size(c.size_on_disk)} written · {when} · from {c.image_path or '?'}")
            if c.path in attached:
                row.add_suffix(status_label("attached", "good"))
            else:
                row.add_suffix(icon_button("user-trash-symbolic", "Delete this clone", lambda c=c: self._delete_clone(c), css="flat"))
            self.clones_group.add(row)
            self.clone_rows.append(row)

    def _delete_clone(self, clone: cl.CloneInfo) -> None:
        confirm(self, f"Delete {clone.label}?", "Everything written to this clone is lost; the image it was made from is untouched.", "Delete",
                lambda: (cl.delete_clone(clone.path), self.refresh_clones()))
