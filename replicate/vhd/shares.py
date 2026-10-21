"""Network shares with images on them, mounted by Replicate as real CIFS mounts.

A folder opened through the desktop's own smb:// layer (KIO, GVFS) exists
only for the signed-in user; QEMU runs as another user and the mount helper
as root, so neither could read images there. Replicate therefore mounts the
share itself under /run/replicate/shares/<id> with the kernel CIFS client,
using the user's Kerberos ticket, a password, the sign-in kept by Cortex
(the network drive manager, see cortex_app), or guest access, and treats the
folder inside it as an image folder.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from replicate.config import Share
from replicate.util import which

log = logging.getLogger(__name__)

SHARES_ROOT = "/run/replicate/shares"


class ShareError(RuntimeError):
    pass


@dataclass
class ShareParts:
    server: str
    share: str
    subpath: str = ""  # forward slashes, no leading slash

    @property
    def unc(self) -> str:
        return f"//{self.server}/{self.share}"

    @property
    def url(self) -> str:
        return f"smb://{self.server}/{self.share}" + (f"/{self.subpath}" if self.subpath else "")

    @property
    def windows(self) -> str:
        tail = ("\\" + self.subpath.replace("/", "\\")) if self.subpath else ""
        return f"\\\\{self.server}\\{self.share}{tail}"


def is_smb_uri(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("smb://") or t.startswith("\\\\") or t.startswith("//")


def parse_share_url(text: str) -> ShareParts:
    """smb://server/share/dir, \\\\server\\share\\dir or //server/share/dir."""
    raw = text.strip()
    if not raw:
        raise ShareError("Enter the share, like smb://server/share/folder")
    if raw.lower().startswith("smb://"):
        parsed = urlparse(raw)
        server = parsed.hostname or ""
        parts = [unquote(p) for p in parsed.path.split("/") if p]
    else:
        cleaned = raw.replace("\\", "/")
        parts = [p for p in cleaned.split("/") if p]
        server = parts.pop(0) if parts else ""
    if not server or not parts:
        raise ShareError(f"{raw!r} is not a share path like smb://server/share or \\\\server\\share")
    share = parts[0]
    subpath = "/".join(parts[1:])
    if ".." in subpath.split("/"):
        raise ShareError("The folder inside the share cannot contain ..")
    return ShareParts(server=server, share=share, subpath=subpath)


def mountpoint_for(share: Share) -> str:
    return f"{SHARES_ROOT}/{share.id}"


def _unescape(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _unc_key(source: str) -> str:
    """//Server/Share/, \\\\server\\share -> 'server/share' (what two mounts of one share have in common)."""
    parts = [p for p in source.strip().replace("\\", "/").split("/") if p]
    return "/".join(parts[:2]).lower()


@dataclass
class ExistingMount:
    """A CIFS mount of a share that is already there: Replicate's own, another
    Replicate share's, or one the system made (fstab, systemd, an admin)."""

    mountpoint: str
    source: str
    fstype: str = "cifs"
    options: str = ""

    @property
    def ours(self) -> bool:
        return self.mountpoint.startswith(SHARES_ROOT + "/")

    def option(self, name: str) -> str | None:
        for opt in self.options.split(","):
            key, _, value = opt.partition("=")
            if key == name:
                return value
        return None

    @property
    def readable_by_others(self) -> bool:
        """Can QEMU and containers (other users) read files on it? CIFS applies
        file_mode/dir_mode on the client unless noperm is set; both default to 0755."""
        if self.option("noperm") is not None:
            return True
        try:
            file_mode = int(self.option("file_mode") or "0755", 8)
            dir_mode = int(self.option("dir_mode") or "0755", 8)
        except ValueError:
            return False
        return bool(file_mode & 0o004) and (dir_mode & 0o005) == 0o005

    @property
    def readonly(self) -> bool:
        return self.options.split(",")[0] == "ro" or ",ro," in f",{self.options},"

    def describe(self, share: Share) -> str:
        if self.mountpoint == mountpoint_for(share):
            return "mounted"
        if self.ours:
            return "mounted (shared with another entry for this share)"
        text = f"mounted by the system at {self.mountpoint}"
        if not self.readable_by_others:
            text += " with modes only you can read; mount it here for the VM and containers"
        return text


def _parse_mounts(text: str) -> list[ExistingMount]:
    out = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        source, mountpoint, fstype, options = _unescape(fields[0]), _unescape(fields[1]), fields[2], fields[3]
        if fstype in ("cifs", "smb3") and source.startswith(("//", "\\\\")):
            out.append(ExistingMount(mountpoint, source, fstype, options))
    return out


def current_mounts() -> list[ExistingMount]:
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            return _parse_mounts(fh.read())
    except OSError:
        return []


def _parse_fstab(text: str) -> list[ExistingMount]:
    """CIFS entries in fstab: shares the system mounts itself, on demand with
    x-systemd.automount (a first access brings them up)."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        fields = line.split()
        if len(fields) >= 3 and fields[2] == "cifs" and fields[0].startswith(("//", "\\\\")):
            out.append(ExistingMount(_unescape(fields[1]), _unescape(fields[0]), "cifs", fields[3] if len(fields) > 3 else ""))
    return out


def fstab_mounts() -> list[ExistingMount]:
    try:
        return _parse_fstab(Path("/etc/fstab").read_text(encoding="utf-8"))
    except OSError:
        return []


def find_mount(share: Share, mounts: list[ExistingMount] | None = None) -> ExistingMount | None:
    """Where the share is mounted right now, if anywhere: Replicate's own
    mount for it first, then another Replicate entry's mount of the same
    share, then a mount the system made."""
    try:
        key = _unc_key(parse_share_url(share.url).unc)
    except ShareError:
        return None
    mounts = current_mounts() if mounts is None else mounts
    own = mountpoint_for(share)
    for m in mounts:
        if m.mountpoint == own:
            return m
    same = [m for m in mounts if _unc_key(m.source) == key]
    return next((m for m in same if m.ours), same[0] if same else None)


def folder_path(share: Share, mount: ExistingMount | None = None) -> str:
    """The image folder: the subpath inside whichever mount of the share is in use."""
    parts = parse_share_url(share.url)
    base = mount.mountpoint if mount is not None else (find_mount(share) or ExistingMount(mountpoint_for(share), parts.unc)).mountpoint
    return f"{base}/{parts.subpath}" if parts.subpath else base


def display_name(share: Share) -> str:
    if share.label:
        return share.label
    parts = parse_share_url(share.url)
    return parts.windows


def is_mounted(share: Share) -> bool:
    return find_mount(share) is not None


def mount_status(share: Share) -> str:
    mount = find_mount(share)
    return mount.describe(share) if mount else "not mounted"


def can_unmount(share: Share) -> bool:
    """Only Replicate's own mount for this entry is Replicate's to unmount."""
    mount = find_mount(share)
    return mount is not None and mount.mountpoint == mountpoint_for(share)


def reachable(path: str) -> str | None:
    """None when the folder answers, else why not (a stale mount, the server gone, a missing subfolder)."""
    try:
        os.listdir(path)
        return None
    except FileNotFoundError:
        return f"{path} does not exist"
    except OSError as exc:
        return exc.strerror or str(exc)


def mount_options(share: Share, *, uid: int, gid: int) -> str:
    """CIFS options apart from the credentials: owned by the user, readable by
    everyone (QEMU, the helper), read-only unless the share is meant for writing."""
    opts = [
        f"uid={uid}", f"gid={gid}",
        "file_mode=0644", "dir_mode=0755",
        "noperm", "iocharset=utf8", "_netdev", "nofail", "soft", "actimeo=30",
    ]
    if share.readonly:
        opts.append("ro")
    if share.auth == "kerberos":
        opts += ["sec=krb5", f"cruid={uid}"]
    elif share.auth == "guest":
        opts += ["guest", "sec=none"]
    # password and cortex: credentials travel separately, over the helper's stdin
    return ",".join(opts)


def cifs_support() -> list[str]:
    problems = []
    if not which("mount.cifs"):
        problems.append("mount.cifs is not installed (package cifs-utils)")
    if not which("cifs.upcall"):
        problems.append("cifs.upcall (cifs-utils, with keyutils) is needed for Kerberos mounts")
    return problems


def credentials_for(share: Share, password: str | None) -> dict[str, str] | None:
    """What the helper signs in with: None for Kerberos and guest, else user, domain and password.

    A share with ``auth == "cortex"`` uses the account Cortex keeps and the
    password Cortex stored in the keyring, so nothing is typed twice.
    """
    if share.auth == "password":
        if not share.username:
            raise ShareError("Password access needs a user name")
        return {"username": share.username, "password": password or "", "domain": share.domain}
    if share.auth == "cortex":
        from replicate.vhd import cortex_app

        acct = cortex_app.account()
        if not acct.is_set:
            raise ShareError("Cortex has no sign-in yet: open Cortex and set your account under Sign-in, or sign in another way here")
        secret = cortex_app.password(acct)
        if secret is None:
            raise ShareError(f"Cortex's password for {acct.display()} could not be read from the keyring (is it unlocked?); "
                             "open Cortex and save the sign-in again, or use a user name and password here")
        return {"username": acct.user, "password": secret, "domain": acct.domain}
    return None


def mount_share(share: Share, password: str | None, *, uid: int, gid: int) -> str:
    """Make the share's image folder available and return it.

    A mount that is already there is used when it will do: Replicate's own
    (remounted if it went stale), another Replicate entry's mount of the
    same share, or a system mount (fstab, systemd automount, an admin's)
    whose files other users can read. A system mount with modes only the
    owner can read is left alone and Replicate mounts the share again, for
    QEMU and containers, under its own mount point. Mounting the same share
    twice is fine for CIFS.
    """
    from replicate.vhd import nbd

    parts = parse_share_url(share.url)
    target = mountpoint_for(share)
    existing = find_mount(share)
    if existing is None:
        for entry in fstab_mounts():
            if _unc_key(entry.source) == _unc_key(parts.unc):
                reachable(entry.mountpoint)  # a first access brings an automount up
                existing = find_mount(share)
                break
    if existing is not None:
        folder = folder_path(share, existing)
        problem = reachable(folder)
        if existing.mountpoint == target:
            if problem is None:
                return folder
            log.info("own mount of %s is not answering (%s); mounting it again", share.url, problem)
            try:
                nbd._privileged(["umount-share", target])
            except nbd.HostMountError as exc:
                raise ShareError(f"The share's mount stopped answering ({problem}) and could not be unmounted: {exc}") from exc
        elif problem is None and existing.readable_by_others:
            return folder
        elif problem is None:
            log.info("%s is mounted at %s with modes other users cannot read; mounting it for them too", share.url, existing.mountpoint)
        elif existing.ours:
            log.info("another entry's mount of %s is not answering (%s); mounting our own", share.url, problem)
        else:
            raise ShareError(f"The share is mounted by the system at {existing.mountpoint}, but that mount is not answering: {problem}. "
                             "Is the network or VPN up? Unmount it there and try again.")
    if not which("mount.cifs"):
        raise ShareError("mount.cifs is not installed; install cifs-utils")
    args = ["mount-share", parts.unc, target, "--options", mount_options(share, uid=uid, gid=gid)]
    stdin = None
    credentials = credentials_for(share, password)
    if credentials is not None:
        args.append("--credentials-stdin")
        stdin = json.dumps(credentials).encode()
    try:
        nbd._privileged(args, input_data=stdin)
    except nbd.HostMountError as exc:
        raise ShareError(_explain(str(exc), share)) from exc
    folder = f"{target}/{parts.subpath}" if parts.subpath else target
    if not Path(folder).is_dir():
        raise ShareError(f"The share mounted, but it has no folder {parts.subpath or '/'}")
    return folder


def unmount_share(share: Share) -> None:
    """Unmount Replicate's own mount for this entry. A system mount, or another
    entry's, is left where it is."""
    from replicate.vhd import nbd

    if not can_unmount(share):
        return
    try:
        nbd._privileged(["umount-share", mountpoint_for(share)])
    except nbd.HostMountError as exc:
        raise ShareError(str(exc)) from exc


def _explain(message: str, share: Share) -> str:
    text = message.lower()
    if "permission denied" in text or "error(13)" in text:
        if share.auth == "kerberos":
            return ("The server refused the Kerberos ticket. Check that this computer holds a ticket (Kerberus or kinit), "
                    "that cifs.upcall is installed, and that the share allows your account. " + message)
        if share.auth == "cortex":
            return "The server did not accept the sign-in Cortex keeps; check it in Cortex: " + message
        return "The user name or password was not accepted: " + message
    if "no such device" in text or "error(19)" in text:
        return "The kernel has no CIFS support loaded: " + message
    if "host is down" in text or "error(112)" in text or "network is unreachable" in text or "could not resolve" in text:
        return "The server could not be reached (VPN?): " + message
    if "required key not available" in text or "error(126)" in text:
        return ("Kerberos mount needs a ticket and cifs.upcall: get a ticket first (Kerberus or kinit) and install cifs-utils "
                "with keyutils. " + message)
    return message


def default_auth() -> str:
    """How a new share signs in: with Cortex's account when Cortex has one, else the Kerberos ticket."""
    from replicate.vhd import cortex_app

    return "cortex" if cortex_app.account().is_set else "kerberos"


def share_from_uri(uri: str) -> Share:
    parts = parse_share_url(uri)
    return Share(url=parts.url, auth=default_auth(), label=re.sub(r"[/\\\\]+", " / ", parts.windows.strip("\\\\")))


def share_from_drive(drive, subpath: str = "") -> Share:
    """A Replicate share for a Cortex drive (optionally a folder inside it), signed in through Cortex."""
    parts = parse_share_url(drive.path)
    if subpath:
        parts.subpath = "/".join(p for p in [parts.subpath, subpath.strip("/\\").replace("\\", "/")] if p)
    return Share(url=parts.url, auth="cortex", label=drive.name)
