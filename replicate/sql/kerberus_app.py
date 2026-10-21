"""Working with Kerberus, the Kerberos ticket manager (github.com/tahuffman1s/kerberus).

Kerberus keeps tickets in the machine's ordinary credential caches and
renews them in the background, so Replicate's Kerberos sign-in already uses
whatever Kerberus issued. This module adds the parts that make the two feel
like one thing: finding Kerberus, reading its view of the caches (several
principals, renewability, who is default), and asking it to sign in for a
principal instead of prompting for the password here.

Kerberus is reached three ways, in this order:

* its GApplication on the session bus (``io.github.tahuffman1s.Kerberus``)
  when it is running, for the sign-in dialog;
* its command line (``kerberus list --json``), found on PATH or through the
  Exec line of its desktop or autostart entry, which is how an AppImage
  announces itself;
* nothing at all, in which case Replicate falls back to GSSAPI and klist.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from replicate.util import which

log = logging.getLogger(__name__)

KERBERUS_APP_ID = "io.github.tahuffman1s.Kerberus"
KERBERUS_URL = "https://github.com/tahuffman1s/kerberus"


@dataclass
class CacheInfo:
    principal: str
    cache: str
    default: bool
    state: str  # valid, expiring, expired
    renewable: bool
    expires: dt.datetime | None
    renew_until: dt.datetime | None = None

    @property
    def realm(self) -> str:
        return self.principal.rpartition("@")[2]

    def describe(self) -> str:
        if self.expires is None:
            return f"{self.principal} · {self.state}"
        remaining = self.expires - dt.datetime.now(self.expires.tzinfo)
        if remaining.total_seconds() <= 0:
            return f"{self.principal} · expired"
        hours, rest = divmod(int(remaining.total_seconds()), 3600)
        tail = ", renewed by Kerberus" if self.renewable else ""
        return f"{self.principal} · {hours}h {rest // 60:02d}m left{tail}"


@dataclass
class Kerberus:
    command: list[str] = field(default_factory=list)  # how to run it, e.g. ["kerberus"] or ["/path/K.AppImage"]
    source: str = ""  # "path", "desktop", "autostart", "setting"

    @property
    def available(self) -> bool:
        return bool(self.command)

    @property
    def label(self) -> str:
        return " ".join(self.command) if self.command else "not found"


def _exec_from_desktop(path: Path) -> list[str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^Exec=(.+)$", text, re.M)
    if not match:
        return None
    parts = shlex.split(match.group(1))
    parts = [p for p in parts if not p.startswith("%") and p != "--background"]
    if parts and Path(parts[0]).name.lower().startswith(("kerberus", "io.github.tahuffman1s.kerberus")):
        return parts
    return parts or None


def find_kerberus(preferred: str = "") -> Kerberus:
    """Locate Kerberus: a configured path, PATH, or the desktop entries it leaves behind."""
    if preferred:
        path = Path(preferred).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return Kerberus([str(path)], "setting")
    found = which("kerberus")
    if found:
        return Kerberus([found], "path")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    candidates = [config_home / "autostart" / f"{KERBERUS_APP_ID}.desktop"]
    for base in (data_home / "applications", Path("/usr/local/share/applications"), Path("/usr/share/applications")):
        if base.is_dir():
            candidates += sorted(base.glob("*erberus*.desktop")) + [base / f"{KERBERUS_APP_ID}.desktop"]
    for candidate in candidates:
        if candidate.is_file():
            command = _exec_from_desktop(candidate)
            if command and (which(command[0]) or Path(command[0]).is_file()):
                return Kerberus(command, "autostart" if "autostart" in str(candidate) else "desktop")
    return Kerberus()


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.astimezone()  # Kerberus prints local naive times
    return when


def parse_list_json(text: str) -> list[CacheInfo]:
    data = json.loads(text)
    caches: list[CacheInfo] = []
    for entry in data:
        tickets = entry.get("tickets", [])
        realm = entry.get("principal", "").rpartition("@")[2]
        tgt = next((t for t in tickets if t.get("service") == f"krbtgt/{realm}@{realm}"), None)
        tgt = tgt or next((t for t in tickets if str(t.get("service", "")).startswith("krbtgt/")), tickets[0] if tickets else None)
        caches.append(
            CacheInfo(
                principal=entry.get("principal", ""),
                cache=entry.get("cache", ""),
                default=bool(entry.get("default")),
                state=entry.get("state", "unknown"),
                renewable=bool(entry.get("renewable")),
                expires=_parse_time(tgt.get("expires")) if tgt else None,
                renew_until=_parse_time(tgt.get("renew_until")) if tgt else None,
            )
        )
    return caches


def list_caches(kerberus: Kerberus, timeout: float = 20) -> list[CacheInfo]:
    """Every principal Kerberus knows about, the default first."""
    if not kerberus.available:
        return []
    try:
        proc = subprocess.run([*kerberus.command, "list", "--json"], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("kerberus list failed: %s", exc)
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        return parse_list_json(proc.stdout)
    except (ValueError, AttributeError) as exc:
        log.debug("kerberus list output not understood: %s", exc)
        return []


def cache_for(caches: list[CacheInfo], principal: str) -> CacheInfo | None:
    wanted = principal.strip().lower()
    if not wanted:
        return next((c for c in caches if c.default), caches[0] if caches else None)
    for cache in caches:
        if cache.principal.lower() == wanted:
            return cache
    user = wanted.split("@", 1)[0]
    return next((c for c in caches if c.principal.lower().split("@", 1)[0] == user), None)


def running() -> bool:
    """Is Kerberus on the session bus right now?"""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (KERBERUS_APP_ID,)), None, Gio.DBusCallFlags.NONE, 2000, None,
        )
        return bool(reply.unpack()[0])
    except Exception as exc:
        log.debug("cannot ask the session bus: %s", exc)
        return False


def _activate_action(name: str, parameter) -> bool:
    """Call a GAction on the running Kerberus over D-Bus (org.gtk.Actions.Activate)."""
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # (s av a{sv}): the parameter travels as a one-element array of variants.
        params = [parameter] if parameter is not None else []
        bus.call_sync(
            KERBERUS_APP_ID, "/" + KERBERUS_APP_ID.replace(".", "/"), "org.gtk.Actions", "Activate",
            GLib.Variant("(sava{sv})", (name, params, {})), None, Gio.DBusCallFlags.NONE, 5000, None,
        )
        return True
    except Exception as exc:
        log.debug("activating %s on Kerberus failed: %s", name, exc)
        return False


def sign_in(kerberus: Kerberus, principal: str, *, wait_seconds: float = 8.0) -> bool:
    """Open Kerberus's sign-in dialog for ``principal``; start Kerberus first if needed.

    Returns True when the request reached Kerberus. The ticket itself arrives
    whenever the user finishes there; callers refresh the ticket status later.
    """
    from gi.repository import GLib

    param = GLib.Variant("s", principal)
    if running() and _activate_action("sign-in", param):
        return True
    if not kerberus.available:
        return False
    try:
        subprocess.Popen(kerberus.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log.warning("could not start Kerberus: %s", exc)
        return False
    import time

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if running():
            time.sleep(0.5)  # let its window come up before the dialog
            return _activate_action("sign-in", param)
    return True  # it is starting; the user can press Get Ticket there


def show(kerberus: Kerberus) -> bool:
    if running() and _activate_action("show", None):
        return True
    if not kerberus.available:
        return False
    try:
        subprocess.Popen(kerberus.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def switch(kerberus: Kerberus, principal: str, timeout: float = 20) -> bool:
    """Make ``principal``'s cache the default one (kswitch), so tools that only
    look at the default cache, such as cifs.upcall for a Kerberos share mount,
    use that ticket."""
    if not kerberus.available:
        found = which("kswitch")
        if not found:
            return False
        proc = subprocess.run([found, "-p", principal], capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode == 0
    try:
        proc = subprocess.run([*kerberus.command, "switch", principal], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("kerberus switch failed: %s", exc)
        return False
    return proc.returncode == 0


def maintain_once(kerberus: Kerberus, timeout: float = 90) -> bool:
    """One pass of Kerberus's background work: renew what can be renewed and
    sign in again, with the password it remembers, where a ticket ran out.
    The quiet way to get a ticket back without a dialog."""
    if not kerberus.available:
        return False
    try:
        proc = subprocess.run([*kerberus.command, "watch", "--once"], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("kerberus watch --once failed: %s", exc)
        return False
    return proc.returncode == 0


def usable(cache: CacheInfo | None) -> bool:
    return cache is not None and cache.state != "expired" and (cache.expires is None or cache.expires > dt.datetime.now(cache.expires.tzinfo))


def wait_for_ticket(kerberus: Kerberus, principal: str, *, seconds: float = 120, poll: float = 3) -> CacheInfo | None:
    """Poll until Kerberus holds a usable ticket for ``principal`` (the default one when blank)."""
    import time

    deadline = time.monotonic() + seconds
    while True:
        found = cache_for(list_caches(kerberus), principal)
        if usable(found):
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
