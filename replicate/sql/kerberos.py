"""Windows authentication from a Linux host: Kerberos through GSSAPI.

Two things live here:

* :class:`GssapiAuth`, the TDS login handshake python-tds expects from an
  authentication object, built on python-gssapi (MIT or Heimdal krb5
  underneath) so it works with whatever ticket cache the desktop has,
  including sssd and ``kinit``.
* Ticket management: who the host is logged in as, how long the ticket
  lasts, and getting a new one with a password without a terminal.

Everything degrades to a clear error when python-gssapi is not installed.
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
import subprocess
from dataclasses import dataclass

from replicate.util import which

log = logging.getLogger(__name__)

try:
    import gssapi
    from gssapi import raw as gss_raw
except Exception:  # noqa: BLE001 - missing or broken bindings
    gssapi = None  # type: ignore
    gss_raw = None  # type: ignore


class KerberosError(RuntimeError):
    pass


def available() -> bool:
    return gssapi is not None


def _require() -> None:
    if gssapi is None:
        raise KerberosError(
            "Kerberos needs the python-gssapi bindings (package python3-gssapi on Debian, Ubuntu and Fedora; "
            "python3Packages.gssapi on Nix)."
        )


# -- names -----------------------------------------------------------------


def realm_for(domain: str) -> str:
    """The Kerberos realm for an Active Directory domain: its DNS name, upper case."""
    return domain.strip().strip(".").upper()


def principal_for(user: str, domain: str) -> str:
    user = user.strip()
    if "@" in user:
        return user
    if "\\" in user:
        dom, _, name = user.rpartition("\\")
        return f"{name}@{realm_for(dom)}" if dom else name
    return f"{user}@{realm_for(domain)}" if domain.strip() else user


def service_principal(host: str, port: int, instance: str = "") -> str:
    """``MSSQLSvc/fqdn:port``, the SPN SQL Server registers for a TCP listener."""
    fqdn = host.strip()
    try:
        if all(part.isdigit() for part in fqdn.split(".")) and fqdn.count(".") == 3:
            fqdn = socket.gethostbyaddr(fqdn)[0]
        elif "." not in fqdn:
            fqdn = socket.getfqdn(fqdn)
    except OSError:
        pass
    suffix = str(port) if port else instance
    return f"MSSQLSvc/{fqdn}:{suffix}" if suffix else f"MSSQLSvc/{fqdn}"


# -- tickets ---------------------------------------------------------------


@dataclass
class Ticket:
    principal: str
    expires: dt.datetime | None

    @property
    def remaining(self) -> dt.timedelta | None:
        return None if self.expires is None else self.expires - dt.datetime.now(dt.timezone.utc)

    @property
    def valid(self) -> bool:
        remaining = self.remaining
        return remaining is None or remaining.total_seconds() > 60

    def describe(self) -> str:
        remaining = self.remaining
        if remaining is None:
            return f"{self.principal}"
        if remaining.total_seconds() <= 0:
            return f"{self.principal} · expired"
        hours, rest = divmod(int(remaining.total_seconds()), 3600)
        return f"{self.principal} · {hours}h {rest // 60:02d}m left"


def current_ticket() -> Ticket | None:
    """The default credential cache's principal, or None when there is no ticket."""
    if gssapi is not None:
        try:
            creds = gssapi.Credentials(usage="initiate")
            lifetime = creds.lifetime  # seconds; raises when expired on some versions
            expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(lifetime)) if lifetime is not None else None
            return Ticket(str(creds.name), expires)
        except Exception as exc:  # noqa: BLE001 - no ticket, expired, or no cache
            log.debug("no usable credentials: %s", exc)
    klist = which("klist")
    if not klist:
        return None
    proc = subprocess.run([klist], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    return parse_klist(proc.stdout)


def parse_klist(text: str) -> Ticket | None:
    principal = None
    expires: dt.datetime | None = None
    for line in text.splitlines():
        if line.lower().startswith(("default principal:", "principal:")):
            principal = line.split(":", 1)[1].strip()
        elif "krbtgt/" in line:
            # MIT: "10/20/26 09:00:00  10/20/26 19:00:00  krbtgt/REALM@REALM"
            parts = line.split()
            if len(parts) >= 4:
                for fmt in ("%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        expires = dt.datetime.strptime(f"{parts[2]} {parts[3]}", fmt).astimezone(dt.timezone.utc)
                        break
                    except ValueError:
                        continue
    return Ticket(principal, expires) if principal else None


def obtain_ticket(principal: str, password: str) -> Ticket:
    """Log in to the realm and put the ticket in the default cache.

    Uses the krb5 library's password extension through GSSAPI; falls back to
    ``kinit`` reading the password from its standard input.
    """
    principal = principal.strip()
    if not principal or "@" not in principal:
        raise KerberosError("Enter the principal as user@REALM (the realm is the domain's DNS name in capitals).")
    if gssapi is not None and gss_raw is not None and hasattr(gss_raw, "acquire_cred_with_password"):
        try:
            name = gssapi.Name(principal, gssapi.NameType.kerberos_principal)
            result = gss_raw.acquire_cred_with_password(name, password.encode("utf-8"), usage="initiate")
            gss_raw.store_cred(result.creds, usage="initiate", overwrite=True, set_default=True)
            return current_ticket() or Ticket(principal, None)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "Preauthentication failed" in message or "Decrypt integrity check" in message:
                raise KerberosError("The password was not accepted.") from exc
            if "Cannot find KDC" in message or "Cannot resolve" in message or "Cannot contact any KDC" in message:
                raise KerberosError(
                    f"No domain controller could be reached for {principal.rsplit('@', 1)[1]}. "
                    "Check /etc/krb5.conf, DNS and the VPN."
                ) from exc
            if "Client not found" in message:
                raise KerberosError("That account does not exist in the realm.") from exc
            log.debug("acquire_cred_with_password failed: %s", message)
            raise KerberosError(message) from exc
    kinit = which("kinit")
    if not kinit:
        raise KerberosError("Neither python-gssapi nor kinit is available to get a ticket.")
    proc = subprocess.run([kinit, principal], input=password + "\n", capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise KerberosError(proc.stderr.strip() or "kinit failed")
    return current_ticket() or Ticket(principal, None)


def destroy_ticket() -> None:
    kdestroy = which("kdestroy")
    if kdestroy:
        subprocess.run([kdestroy], capture_output=True, check=False)


# -- the TDS handshake -----------------------------------------------------


class GssapiAuth:
    """What python-tds calls an auth object: create the first token, answer the server's, done."""

    def __init__(self, spn: str, principal: str | None = None):
        _require()
        target = gssapi.Name(spn, gssapi.NameType.kerberos_principal)
        creds = None
        if principal:
            creds = gssapi.Credentials(name=gssapi.Name(principal, gssapi.NameType.kerberos_principal), usage="initiate")
        flags = [
            gssapi.RequirementFlag.mutual_authentication,
            gssapi.RequirementFlag.integrity,
            gssapi.RequirementFlag.confidentiality,
            gssapi.RequirementFlag.out_of_sequence_detection,
        ]
        try:
            self._ctx = gssapi.SecurityContext(name=target, creds=creds, usage="initiate", flags=flags, mech=gssapi.MechType.kerberos)
        except Exception as exc:  # noqa: BLE001
            raise KerberosError(f"Could not start Kerberos for {spn}: {exc}") from exc
        self.spn = spn

    def create_packet(self) -> bytes:
        try:
            token = self._ctx.step()
        except Exception as exc:  # noqa: BLE001
            raise KerberosError(_explain(exc, self.spn)) from exc
        return bytes(token or b"")

    def handle_next(self, packet: bytes) -> bytes | None:
        try:
            token = self._ctx.step(packet)
        except Exception as exc:  # noqa: BLE001
            raise KerberosError(_explain(exc, self.spn)) from exc
        if self._ctx.complete and not token:
            return b""
        return bytes(token or b"")

    def close(self) -> None:
        self._ctx = None


def _explain(exc: Exception, spn: str) -> str:
    message = str(exc)
    if "No Kerberos credentials" in message or "No credentials cache" in message or "credentials have been revoked" in message:
        return "No Kerberos ticket on this computer. Get one with 'Get Ticket' or kinit."
    if "Ticket expired" in message or "expired" in message.lower():
        return "The Kerberos ticket has expired; get a new one."
    if "Server not found in Kerberos database" in message or "not found in Kerberos" in message:
        return (
            f"The domain has no service principal {spn}. SQL Server registers it when its service account may; "
            "otherwise an administrator runs setspn, or use the host name the SPN was registered with."
        )
    return message
