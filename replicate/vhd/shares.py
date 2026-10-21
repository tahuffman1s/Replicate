"""Network shares with images on them, mounted by Replicate as real CIFS mounts.

A folder opened through the desktop's own smb:// layer (KIO, GVFS) exists
only for the signed-in user; QEMU runs as another user and the mount helper
as root, so neither could read images there. Replicate therefore mounts the
share itself under /run/replicate/shares/<id> with the kernel CIFS client,
using the user's Kerberos ticket, a password, or guest access, and treats
the folder inside it as an image folder.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from replicate.config import Share
from replicate.util import which

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


def folder_path(share: Share) -> str:
    parts = parse_share_url(share.url)
    base = mountpoint_for(share)
    return f"{base}/{parts.subpath}" if parts.subpath else base


def display_name(share: Share) -> str:
    if share.label:
        return share.label
    parts = parse_share_url(share.url)
    return parts.windows


def is_mounted(share: Share) -> bool:
    target = mountpoint_for(share)
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) >= 2 and fields[1].replace("\\040", " ") == target:
                    return True
    except OSError:
        pass
    return False


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
    return ",".join(opts)


def cifs_support() -> list[str]:
    problems = []
    if not which("mount.cifs"):
        problems.append("mount.cifs is not installed (package cifs-utils)")
    if not which("cifs.upcall"):
        problems.append("cifs.upcall (cifs-utils, with keyutils) is needed for Kerberos mounts")
    return problems


def mount_share(share: Share, password: str | None, *, uid: int, gid: int) -> str:
    """Mount the share through the root helper; returns the image folder inside it."""
    from replicate.vhd import nbd

    if not which("mount.cifs"):
        raise ShareError("mount.cifs is not installed; install cifs-utils")
    parts = parse_share_url(share.url)
    target = mountpoint_for(share)
    if is_mounted(share):
        return folder_path(share)
    args = ["mount-share", parts.unc, target, "--options", mount_options(share, uid=uid, gid=gid)]
    stdin = None
    if share.auth == "password":
        if not share.username:
            raise ShareError("Password access needs a user name")
        args.append("--credentials-stdin")
        stdin = json.dumps({"username": share.username, "password": password or "", "domain": share.domain}).encode()
    try:
        nbd._privileged(args, input_data=stdin)  # noqa: SLF001
    except nbd.HostMountError as exc:
        raise ShareError(_explain(str(exc), share)) from exc
    folder = folder_path(share)
    if not Path(folder).is_dir():
        raise ShareError(f"The share mounted, but it has no folder {parts.subpath or '/'}")
    return folder


def unmount_share(share: Share) -> None:
    from replicate.vhd import nbd

    if not is_mounted(share):
        return
    try:
        nbd._privileged(["umount-share", mountpoint_for(share)])  # noqa: SLF001
    except nbd.HostMountError as exc:
        raise ShareError(str(exc)) from exc


def _explain(message: str, share: Share) -> str:
    text = message.lower()
    if "permission denied" in text or "error(13)" in text:
        if share.auth == "kerberos":
            return ("The server refused the Kerberos ticket. Check that this computer holds a ticket (Kerberus or kinit), "
                    "that cifs.upcall is installed, and that the share allows your account. " + message)
        return "The user name or password was not accepted: " + message
    if "no such device" in text or "error(19)" in text:
        return "The kernel has no CIFS support loaded: " + message
    if "host is down" in text or "error(112)" in text or "network is unreachable" in text or "could not resolve" in text:
        return "The server could not be reached (VPN?): " + message
    if "required key not available" in text or "error(126)" in text:
        return ("Kerberos mount needs a ticket and cifs.upcall: get a ticket first (Kerberus or kinit) and install cifs-utils "
                "with keyutils. " + message)
    return message


def share_from_uri(uri: str) -> Share:
    parts = parse_share_url(uri)
    return Share(url=parts.url, label=re.sub(r"[/\\\\]+", " / ", parts.windows.strip("\\\\")))
