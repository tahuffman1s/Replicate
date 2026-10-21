"""A small wrapper over python-tds (pure Python, no ODBC drivers to bundle)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from replicate.config import SqlServer

log = logging.getLogger(__name__)

try:
    import pytds
except ImportError:  # pragma: no cover
    pytds = None  # type: ignore


class SqlError(RuntimeError):
    pass


HostResolver = Callable[[SqlServer], str]


def resolve_instance_port(host: str, instance: str, timeout: float = 5) -> int:
    """Ask SQL Browser (UDP 1434) which TCP port a named instance listens on."""
    import socket

    request = b"\x04" + instance.encode("ascii", errors="ignore") + b"\x00"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(request, (host, 1434))
            data, _ = sock.recvfrom(4096)
        except OSError as exc:
            raise SqlError(f"SQL Browser on {host} did not answer for instance {instance}: {exc}") from exc
    text = data[3:].decode("ascii", errors="ignore")
    fields = text.split(";")
    for i in range(0, len(fields) - 1, 2):
        if fields[i].lower() == "tcp" and fields[i + 1].isdigit():
            return int(fields[i + 1])
    raise SqlError(f"SQL Browser on {host} knows no TCP port for instance {instance} (is TCP enabled?)")


def build_windows_auth(server: SqlServer, password: str, host: str, port: int):
    """NTLM with a password, or Kerberos with the host's ticket."""
    user = server.username.strip()
    domain = server.domain.strip()
    if "\\" in user and not domain:  # older entries kept DOMAIN\\user in one field
        domain, _, user = user.rpartition("\\")
    if server.auth == "kerberos":
        from replicate.sql import kerberos

        spn = server.spn.strip() or kerberos.service_principal(host, port, server.instance)
        principal = kerberos.principal_for(user, domain) if user else None
        try:
            return kerberos.GssapiAuth(spn, principal)
        except kerberos.KerberosError as exc:
            raise SqlError(str(exc)) from exc
    try:
        from pytds import login as tds_login
    except ImportError as exc:  # pragma: no cover
        raise SqlError(f"Windows authentication is not available: {exc}") from exc
    if not password:
        raise SqlError(f"{server.name}: Windows authentication needs the account's password (or use Kerberos).")
    return tds_login.NtlmAuth(user_name=f"{domain}\\{user}" if domain else user, password=password)


class SqlConnection:
    def __init__(self, server: SqlServer, password: str, host: str, *, timeout: float = 15, database: str | None = None,
                 query_timeout: float | None = None):
        if pytds is None:
            raise SqlError("python-tds is not installed")
        self.server = server
        self.host = host
        user = server.username
        port = server.port or (resolve_instance_port(host, server.instance, timeout) if server.instance else 1433)
        kwargs: dict[str, Any] = {
            "server": host,
            "port": port,
            "database": database or server.database or "master",
            "user": user,
            "password": password,
            "login_timeout": timeout,
            # None means no query timeout (pytds puts this straight on the socket; 0 would make it
            # non-blocking and every query would fail with "Resource temporarily unavailable").
            "timeout": query_timeout,
            "appname": "Replicate",
            "autocommit": True,
            "as_dict": True,
        }
        if server.auth in ("windows", "kerberos"):
            kwargs["user"] = None
            kwargs["password"] = None
            kwargs["auth"] = build_windows_auth(server, password, host, port)
        try:
            self._conn = pytds.connect(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise SqlError(f"{server.name}: {exc}") from exc

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> SqlConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def query(self, sql: str, params: tuple | None = None, *, timeout: int | None = None) -> list[dict]:
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params or ())
                if cur.description is None:
                    return []
                return list(cur.fetchall())
        except Exception as exc:  # noqa: BLE001
            raise SqlError(str(exc)) from exc

    def execute(self, sql: str, params: tuple | None = None, *, timeout: int | None = None) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params or ())
                # Drain result sets so errors raised late (e.g. by RESTORE) surface.
                while cur.nextset():
                    pass
        except Exception as exc:  # noqa: BLE001
            raise SqlError(str(exc)) from exc

    def scalar(self, sql: str, params: tuple | None = None) -> Any:
        rows = self.query(sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def version(self) -> dict[str, str]:
        row = self.query(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)) AS version,"
            " CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition,"
            " CAST(SERVERPROPERTY('MachineName') AS nvarchar(128)) AS machine,"
            " CAST(SERVERPROPERTY('InstanceName') AS nvarchar(128)) AS instance,"
            " CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(32)) AS level,"
            " @@SERVERNAME AS servername"
        )[0]
        return {k: (v or "") for k, v in row.items()}
