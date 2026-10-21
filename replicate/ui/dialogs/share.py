"""Add a network share that holds images; Replicate mounts it itself.

The share can be one of the drives Cortex (the network drive manager) keeps,
picked from a list, and can sign in with the account Cortex keeps, so a
share and its password are set up once, in Cortex.
"""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from replicate.config import Share
from replicate.tasks import run_in_thread
from replicate.ui.context import AppContext
from replicate.ui.widgets.common import (
    dialog_header,
    error_dialog,
    status_label,
    text_button,
)
from replicate.vhd import cortex_app
from replicate.vhd import shares as sh

AUTH_LABELS = {
    "cortex": "The sign-in Cortex keeps",
    "kerberos": "This computer's Kerberos ticket",
    "password": "User name and password",
    "guest": "Guest",
}


class ShareDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext, share: Share | None = None, *, on_mounted=None):
        self.editing = share is not None
        super().__init__(title="Edit network share" if self.editing else "Add network share", content_width=560)
        self.ctx = ctx
        self.share = share or Share(auth=sh.default_auth())
        self.on_mounted = on_mounted
        self.cortex_account = cortex_app.account()
        self.cortex_drives = cortex_app.drives()
        self.auths = list(AUTH_LABELS)
        if not (cortex_app.installed() or self.share.auth == "cortex"):
            self.auths.remove("cortex")
        view = Adw.ToolbarView()
        header, self.go = dialog_header(self, "Mount" if not self.editing else "Save and Mount", self._on_go)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        if self.cortex_drives and not self.editing:
            drives = Adw.PreferencesGroup(title="Your Cortex drives")
            drives.set_description("Shares you already keep in Cortex. Pick one to use it here; add a folder inside it below if the images are in one.")
            for drive in self.cortex_drives:
                row = Adw.ActionRow(use_markup=False, title=drive.name, subtitle=drive.path, activatable=True)
                row.add_prefix(Gtk.Image.new_from_icon_name("network-server-symbolic"))
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _r, drive=drive: self._pick_drive(drive))
                drives.add(row)
            page.add(drives)

        where = Adw.PreferencesGroup(title="Share")
        where.set_description(
            "Replicate mounts the share itself (kernel CIFS under /run/replicate/shares), so the images on it can be "
            "read by the VM, by containers and by the mount helper, not only by your desktop session."
        )
        self.url = Adw.EntryRow(title="Share and folder (smb://server/share/folder or \\\\server\\share\\folder)", text=self.share.url)
        where.add(self.url)
        self.label = Adw.EntryRow(title="Name (optional)", text=self.share.label)
        where.add(self.label)
        self.readonly = Adw.SwitchRow(use_markup=False, title="Read-only", subtitle="Images are only read; clones are written locally", active=self.share.readonly)
        where.add(self.readonly)
        page.add(where)

        auth = Adw.PreferencesGroup(title="Access")
        self.auth = Adw.ComboRow(title="Sign in with")
        self.auth.set_model(Gtk.StringList.new([AUTH_LABELS[a] for a in self.auths]))
        self.auth.set_selected(self.auths.index(self.share.auth) if self.share.auth in self.auths else 0)
        self.auth.connect("notify::selected", lambda *_: self._sync())
        auth.add(self.auth)
        self.cortex_row = Adw.ActionRow(use_markup=False, title="Cortex account", subtitle="")
        self.cortex_row.add_prefix(Gtk.Image.new_from_icon_name("network-workgroup-symbolic"))
        self.cortex_row.add_suffix(text_button("Open Cortex", self._open_cortex))
        auth.add(self.cortex_row)
        self.domain = Adw.EntryRow(title="Domain", text=self.share.domain)
        auth.add(self.domain)
        self.user = Adw.EntryRow(title="User name", text=self.share.username)
        auth.add(self.user)
        self.password = Adw.PasswordEntryRow(title="Password" + (" (blank keeps the saved one)" if self.editing else ""))
        auth.add(self.password)
        self.ticket_row = Adw.ActionRow(use_markup=False, title="Kerberos ticket", subtitle="Checking…")
        auth.add(self.ticket_row)
        page.add(auth)

        self.status_group = Adw.PreferencesGroup()
        self.status_row = Adw.ActionRow(use_markup=False, title="", subtitle="")
        self.status_group.add(self.status_row)
        self.status_group.set_visible(False)
        page.add(self.status_group)

        problems = sh.cifs_support()
        if problems:
            warn = Adw.PreferencesGroup()
            warn.add(Adw.ActionRow(use_markup=False, title="This computer cannot mount shares yet", subtitle="; ".join(problems), icon_name="dialog-warning-symbolic"))
            page.add(warn)
        self._sync()

    def _mode(self) -> str:
        return self.auths[self.auth.get_selected()]

    def _pick_drive(self, drive) -> None:
        picked = sh.share_from_drive(drive)
        self.url.set_text(picked.url)
        if not self.label.get_text().strip():
            self.label.set_text(drive.name)
        if "cortex" in self.auths and self.cortex_account.is_set:
            self.auth.set_selected(self.auths.index("cortex"))
        self.url.grab_focus()

    def _open_cortex(self) -> None:
        if not cortex_app.show():
            Gtk.UriLauncher(uri=cortex_app.CORTEX_URL + "/releases/latest").launch(self.get_root(), None, None, None)

    def _sync(self) -> None:
        mode = self._mode()
        self.domain.set_visible(mode == "password")
        self.user.set_visible(mode == "password")
        self.password.set_visible(mode == "password")
        self.ticket_row.set_visible(mode == "kerberos")
        self.cortex_row.set_visible(mode == "cortex")
        if mode == "cortex":
            acct = self.cortex_account
            if not acct.is_set:
                self.cortex_row.set_subtitle("Cortex has no sign-in yet. Set your account under Sign-in in Cortex, then mount here.")
            else:
                self.cortex_row.set_subtitle(f"{acct.display()} · looking for its password in the keyring…")

                def found(secret) -> None:
                    self.cortex_row.set_subtitle(f"{acct.display()} · password from Cortex, kept in your keyring" if secret
                                                 else f"{acct.display()} · its password could not be read from the keyring; save the sign-in in Cortex again")

                run_in_thread(cortex_app.password, acct, on_done=found, on_error=lambda e: self.cortex_row.set_subtitle(str(e)), name="cortex-password")
        if mode == "kerberos":
            from replicate.sql import kerberos

            def done(ticket) -> None:
                self.ticket_row.set_subtitle(ticket.describe() if ticket else "No ticket on this computer; get one with Kerberus or kinit first")

            run_in_thread(kerberos.current_ticket, on_done=done, on_error=lambda e: self.ticket_row.set_subtitle(str(e)), name="klist")

    def _on_go(self) -> None:
        try:
            parts = sh.parse_share_url(self.url.get_text())
        except sh.ShareError as exc:
            error_dialog(self, "Check the share address", str(exc))
            return
        s = self.share
        s.url = parts.url
        s.label = self.label.get_text().strip()
        s.readonly = self.readonly.get_active()
        s.auth = self._mode()
        s.domain = self.domain.get_text().strip() if s.auth == "password" else ""
        s.username = self.user.get_text().strip() if s.auth == "password" else ""
        password = self.password.get_text() or None
        if s.auth == "password" and not s.username:
            error_dialog(self, "User name needed", "Enter the account to sign in to the share with.")
            return
        if s.auth == "cortex" and not self.cortex_account.is_set:
            error_dialog(self, "Cortex has no sign-in", "Open Cortex and set your account under Sign-in, or choose another way to sign in.")
            return
        if password and s.auth == "password":
            self.ctx.secrets.set(s.secret_key, password)
        pw = password or self.ctx.secrets.get(s.secret_key) or ""
        self.go.set_sensitive(False)
        self.status_group.set_visible(True)
        self.status_row.set_title("Mounting…")
        self.status_row.set_subtitle("Your administrator password may be asked for")

        def done(folder: str) -> None:
            if s not in self.ctx.config.vhd.shares:
                self.ctx.config.vhd.shares.append(s)
            self.ctx.save_config()
            self.ctx.toast(f"{sh.display_name(s)} is mounted")
            if self.on_mounted:
                self.on_mounted(s)
            self.close()

        def failed(exc: BaseException) -> None:
            self.go.set_sensitive(True)
            self.status_row.set_title("Could not mount")
            self.status_row.set_subtitle(str(exc))

        run_in_thread(sh.mount_share, s, pw, uid=os.getuid(), gid=os.getgid(), on_done=done, on_error=failed, name="mount-share")


def cortex_hint_row(ctx: AppContext, on_add) -> Adw.ActionRow | None:
    """A row for the Images page when Cortex keeps drives Replicate is not using yet."""
    drives = cortex_app.drives()
    known = ctx.config.vhd.shares
    unused = [d for d in drives if not any(cortex_app.same_share(d.path, s.url) for s in known)]
    if not unused:
        return None
    names = ", ".join(d.name for d in unused[:3]) + (f" and {len(unused) - 3} more" if len(unused) > 3 else "")
    row = Adw.ActionRow(use_markup=False, title=f"{len(unused)} network drive{'s' if len(unused) != 1 else ''} in Cortex", subtitle=f"{names}. Use one as an image source; it mounts with the sign-in Cortex keeps.")
    row.add_prefix(Gtk.Image.new_from_icon_name("network-workgroup-symbolic"))
    row.add_suffix(status_label("Cortex", "neutral"))
    row.add_suffix(text_button("Add…", on_add))
    return row
