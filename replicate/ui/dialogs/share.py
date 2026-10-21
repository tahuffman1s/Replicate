"""Add a network share that holds images; Replicate mounts it itself."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from replicate.config import Share  # noqa: E402
from replicate.tasks import run_in_thread  # noqa: E402
from replicate.ui.context import CONFIG_CHANGED, AppContext  # noqa: E402
from replicate.ui.widgets.common import dialog_header, error_dialog  # noqa: E402
from replicate.vhd import shares as sh  # noqa: E402

AUTHS = ["kerberos", "password", "guest"]


class ShareDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext, share: Share | None = None, *, on_mounted=None):
        self.editing = share is not None
        super().__init__(title="Edit network share" if self.editing else "Add network share", content_width=560)
        self.ctx = ctx
        self.share = share or Share()
        self.on_mounted = on_mounted
        view = Adw.ToolbarView()
        header, self.go = dialog_header(self, "Mount" if not self.editing else "Save and Mount", self._on_go)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

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
        self.auth.set_model(Gtk.StringList.new(["This computer's Kerberos ticket", "User name and password", "Guest"]))
        self.auth.set_selected(AUTHS.index(self.share.auth) if self.share.auth in AUTHS else 0)
        self.auth.connect("notify::selected", lambda *_: self._sync())
        auth.add(self.auth)
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
        return AUTHS[self.auth.get_selected()]

    def _sync(self) -> None:
        mode = self._mode()
        self.domain.set_visible(mode == "password")
        self.user.set_visible(mode == "password")
        self.password.set_visible(mode == "password")
        self.ticket_row.set_visible(mode == "kerberos")
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
        if password:
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
