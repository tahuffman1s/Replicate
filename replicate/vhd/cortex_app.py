"""Working with Cortex, the network drive manager (github.com/tahuffman1s/cortex).

Cortex finds the Windows shares on a network and keeps the ones a person
uses as drives, connected through GVfs with one saved sign-in. Replicate
wants the same shares, but as kernel mounts QEMU and the root helper can
read. This module joins the two:

* the drives Cortex keeps (``~/.config/cortex/drives.json``) are offered as
  image sources, so a share is added once, in Cortex;
* the sign-in Cortex keeps (``account.json`` and the keyring entry it
  stores through the Secret Service) is a way to mount a share here, so the
  password is typed once, in Cortex;
* a drive Cortex has connected is reachable through gvfsd-fuse, so images on
  it are found right away, before the share is mounted by Replicate.

Everything here reads Cortex's files and keyring entry; nothing writes
them. Cortex stays the place where drives and the sign-in are managed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from replicate.util import which

log = logging.getLogger(__name__)

CORTEX_APP_ID = "io.github.tahuffman1s.Cortex"
CORTEX_URL = "https://github.com/tahuffman1s/cortex"
CORTEX_SECRET_SCHEMA = "io.github.tahuffman1s.Cortex.Account"


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cortex"


def _read_json(name: str, default):
    try:
        return json.loads((config_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# -- share paths, the way Cortex writes them -----------------------------------

def split_path(text: str) -> list[str]:
    r"""\\server\share\dir, //server/share/dir or smb://server/share/dir -> [server, share, dir]."""
    from urllib.parse import unquote

    p = text.strip()
    if p.lower().startswith("smb:"):
        p = unquote(p[4:])
    p = p.replace("\\", "/")
    return [x for x in p.split("/") if x]


def smb_uri(path: str) -> str:
    """A share path -> the smb:// URI GIO mounts (what Cortex connects)."""
    parts = split_path(path)
    if not parts:
        return ""
    return "smb://" + parts[0] + "/" + "".join(quote(x, safe="") + "/" for x in parts[1:])


def same_share(a: str, b: str) -> bool:
    """Whether two paths name the same server and share (SMB is case-blind)."""
    pa, pb = [x.lower() for x in split_path(a)], [x.lower() for x in split_path(b)]
    return len(pa) >= 2 and pa[:2] == pb[:2]


def is_within(path: str, root: str) -> bool:
    p, r = [x.lower() for x in split_path(path)], [x.lower() for x in split_path(root)]
    return len(r) >= 2 and p[: len(r)] == r


# -- what Cortex keeps -----------------------------------------------------------

@dataclass
class Drive:
    name: str
    path: str  # \\server\share\dir, as Cortex stores it
    login: bool = False
    sidebar: bool = True

    @property
    def url(self) -> str:
        parts = split_path(self.path)
        return "smb://" + "/".join(parts)

    @property
    def server(self) -> str:
        parts = split_path(self.path)
        return parts[0] if parts else ""


@dataclass
class Account:
    user: str = ""
    domain: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.user)

    def display(self) -> str:
        return f"{self.domain}\\{self.user}" if self.domain else self.user

    def attributes(self) -> dict[str, str]:
        return {"user": self.user, "domain": self.domain}


def drives() -> list[Drive]:
    """The drives Cortex keeps, in Cortex's order (by name)."""
    out: list[Drive] = []
    data = _read_json("drives.json", [])
    if not isinstance(data, list):
        return out
    for entry in data:
        try:
            parts = split_path(str(entry["path"]))
            if len(parts) < 2:
                continue
            out.append(Drive(str(entry["name"]), "\\\\" + "\\".join(parts), bool(entry.get("login", False)), bool(entry.get("sidebar", True))))
        except (KeyError, TypeError, AttributeError):
            continue
    return out


def account() -> Account:
    data = _read_json("account.json", {})
    if not isinstance(data, dict):
        return Account()
    return Account(str(data.get("user", "") or ""), str(data.get("domain", "") or ""))


def installed() -> bool:
    """Has Cortex been set up on this computer (its files exist or it can be run)?"""
    return config_dir().is_dir() or find_cortex().available


def password(acct: Account | None = None) -> str | None:
    """The password Cortex stored for its account, from the desktop keyring.

    None when there is no keyring, no entry, or the keyring is locked and the
    person dismissed the unlock prompt. The lookup is synchronous: call it
    from a worker thread.
    """
    acct = acct or account()
    if not acct.is_set or os.environ.get("REPLICATE_NO_KEYRING") == "1":
        return None
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except Exception as exc:  # noqa: BLE001 - no libsecret typelib
        log.debug("libsecret not available: %s", exc)
        return None
    try:
        schema = Secret.Schema.new(
            CORTEX_SECRET_SCHEMA, Secret.SchemaFlags.NONE,
            {"user": Secret.SchemaAttributeType.STRING, "domain": Secret.SchemaAttributeType.STRING},
        )
        return Secret.password_lookup_sync(schema, acct.attributes(), None)
    except Exception as exc:  # noqa: BLE001 - keyring locked, no service
        log.debug("Cortex password lookup failed: %s", exc)
        return None


# -- what Cortex has connected (GVfs) --------------------------------------------

def gvfs_root() -> Path:
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    return Path(runtime) / "gvfs"


def is_gvfs_path(path: str) -> bool:
    """A path that goes through the desktop's gvfsd-fuse: only this user's session can read it."""
    text = str(path)
    return text.startswith(str(gvfs_root()) + "/") or "/gvfs/smb-share:" in text


def connected_path(drive_path: str) -> str | None:
    """The local folder a drive Cortex has connected shows up as (gvfsd-fuse), or None.

    Needs GIO; returns None headless, when gvfsd-fuse is not running, or when
    the share is not connected right now.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:  # noqa: BLE001
        return None
    uri = smb_uri(drive_path)
    if not uri:
        return None
    gfile = Gio.File.new_for_uri(uri)
    try:
        gfile.find_enclosing_mount(None)
    except GLib.Error:
        return None
    local = gfile.get_path()
    if local and Path(local).is_dir():
        return local
    return None


def connected_drives() -> list[tuple[Drive, str]]:
    """Cortex drives connected right now, with their gvfsd-fuse folders."""
    out = []
    for drive in drives():
        local = connected_path(drive.path)
        if local:
            out.append((drive, local))
    return out


def drive_for_path(local_path: str, connected: list[tuple[Drive, str]] | None = None) -> Drive | None:
    """Which connected Cortex drive a gvfs path belongs to."""
    text = str(local_path)
    for drive, folder in connected if connected is not None else connected_drives():
        if text == folder or text.startswith(folder.rstrip("/") + "/"):
            return drive
    return None


def drive_for_share(url: str, known: list[Drive] | None = None) -> Drive | None:
    """The Cortex drive a Replicate share lives on (same server and share), if any."""
    for drive in known if known is not None else drives():
        if same_share(drive.path, url):
            return drive
    return None


# -- the app itself --------------------------------------------------------------

@dataclass
class Cortex:
    command: list[str]
    source: str = ""  # path, desktop, autostart

    @property
    def available(self) -> bool:
        return bool(self.command)


def _exec_from_desktop(path: Path) -> list[str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^Exec=(.+)$", text, re.MULTILINE)
    if not match:
        return None
    parts = [p for p in shlex.split(match.group(1)) if not p.startswith("%") and p != "--connect"]
    return parts or None


def find_cortex() -> Cortex:
    found = which("cortex")
    if found:
        return Cortex([found], "path")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    candidates = [config_home / "autostart" / f"{CORTEX_APP_ID}.desktop"]
    for base in (data_home / "applications", Path("/usr/local/share/applications"), Path("/usr/share/applications")):
        if base.is_dir():
            candidates += sorted(base.glob("*ortex*.desktop")) + [base / f"{CORTEX_APP_ID}.desktop"]
    for candidate in candidates:
        if candidate.is_file():
            command = _exec_from_desktop(candidate)
            if command and (which(command[0]) or Path(command[0]).is_file()):
                return Cortex(command, "autostart" if "autostart" in str(candidate) else "desktop")
    return Cortex([])


def running() -> bool:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (CORTEX_APP_ID,)), None, Gio.DBusCallFlags.NONE, 2000, None,
        )
        return bool(reply.unpack()[0])
    except Exception as exc:  # noqa: BLE001
        log.debug("cannot ask the session bus: %s", exc)
        return False


def show(cortex: Cortex | None = None) -> bool:
    """Bring Cortex's window up, starting it if it is not running."""
    if running():
        try:
            from gi.repository import Gio, GLib

            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(
                CORTEX_APP_ID, "/" + CORTEX_APP_ID.replace(".", "/"), "org.gtk.Application", "Activate",
                GLib.Variant("(a{sv})", ({},)), None, Gio.DBusCallFlags.NONE, 5000, None,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("activating Cortex failed: %s", exc)
    cortex = cortex or find_cortex()
    if not cortex.available:
        return False
    try:
        subprocess.Popen(cortex.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as exc:
        log.warning("could not start Cortex: %s", exc)
        return False


def describe() -> str:
    """One line for preflight and the settings page."""
    cortex = find_cortex()
    if not cortex.available and not config_dir().is_dir():
        return "not installed"
    acct = account()
    count = len(drives())
    who = acct.display() if acct.is_set else "no sign-in set"
    where = "running" if running() else ("found" if cortex.available else "its files are here")
    return f"{where} · {count} drive{'s' if count != 1 else ''} · {who}"
