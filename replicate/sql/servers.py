"""The server list, connecting to entries, and linking servers to each other."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from replicate.config import AppConfig, SqlServer, save_config
from replicate.secrets import SecretStore
from replicate.sql.connection import SqlConnection, SqlError
from replicate.util import quote_sql_string as qs
from replicate.vm.manager import VmManager

log = logging.getLogger(__name__)


@dataclass
class LinkInfo:
    name: str
    data_source: str
    provider: str
    is_replicate: bool


class ServerRegistry:
    def __init__(self, config: AppConfig, secrets: SecretStore, vm: VmManager):
        self.config = config
        self.secrets = secrets
        self.vm = vm
        self._vm_ip: str | None = None

    # -- list ----------------------------------------------------------------
    @property
    def servers(self) -> list[SqlServer]:
        return self.config.servers

    def get(self, server_id: str) -> SqlServer | None:
        return self.config.server(server_id)

    def add(self, server: SqlServer, password: str | None) -> None:
        self.config.servers.append(server)
        if password is not None:
            self.secrets.set(server.secret_key, password)
        save_config(self.config)

    def update(self, server: SqlServer, password: str | None) -> None:
        if password:
            self.secrets.set(server.secret_key, password)
        save_config(self.config)

    def remove(self, server: SqlServer) -> None:
        for other in self.config.servers:
            if server.id in other.linked_to:
                other.linked_to.remove(server.id)
        self.config.servers.remove(server)
        self.secrets.delete(server.secret_key)
        save_config(self.config)

    # -- connections ---------------------------------------------------------
    def resolve_host(self, server: SqlServer, *, refresh: bool = False) -> str:
        if server.in_docker:
            return server.host.strip() or "127.0.0.1"
        if server.host.strip().lower() != "vm":
            return server.host.strip()
        if refresh or not self._vm_ip:
            self._vm_ip = self.vm.virsh.ip_address(self.vm.vm.name)
        if not self._vm_ip:
            raise SqlError("The VM has no IP address yet (is it running, with the guest agent up?)")
        return self._vm_ip

    def forget_vm_ip(self) -> None:
        self._vm_ip = None

    def password(self, server: SqlServer) -> str:
        return self.secrets.get(server.secret_key) or ""

    def connect(self, server: SqlServer, *, database: str | None = None, timeout: float = 15) -> SqlConnection:
        host = self.resolve_host(server)
        try:
            return SqlConnection(server, self.password(server), host, timeout=timeout, database=database)
        except SqlError:
            if server.host.strip().lower() == "vm":
                # The VM may have a new address since we last looked.
                host = self.resolve_host(server, refresh=True)
                return SqlConnection(server, self.password(server), host, timeout=timeout, database=database)
            raise

    def test(self, server: SqlServer, password: str | None = None) -> dict[str, str]:
        host = self.resolve_host(server, refresh=True)
        pw = password if password is not None else self.password(server)
        with SqlConnection(server, pw, host, timeout=10) as conn:
            info = conn.version()
            info["host"] = host
            return info

    def databases(self, server: SqlServer) -> list[dict]:
        with self.connect(server) as conn:
            return conn.query(
                "SELECT d.name, d.state_desc AS state, d.recovery_model_desc AS recovery,"
                " CAST(SUM(mf.size) * 8.0 / 1024 AS decimal(18,1)) AS size_mb,"
                " d.create_date"
                " FROM sys.databases d JOIN sys.master_files mf ON mf.database_id = d.database_id"
                " WHERE d.database_id > 4 GROUP BY d.name, d.state_desc, d.recovery_model_desc, d.create_date"
                " ORDER BY d.name"
            )

    # -- linked servers ------------------------------------------------------
    def link_name(self, target: SqlServer) -> str:
        """The name the linked server gets on the source: the target's name, cleaned up."""
        base = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in target.name).strip("_") or target.id
        return base[:100]

    def data_source(self, target: SqlServer, *, from_vm: bool = False, source: SqlServer | None = None) -> str:
        """How the source server reaches the target.

        Two instances inside the VM talk over loopback; two containers talk
        over the shared "replicate" Docker network by container name; a
        container reaches a server on the host through host.docker.internal.
        Otherwise the host name as Replicate sees it, with the fixed port.
        """
        source_where = source.where if source is not None else ("vm" if from_vm else "remote")
        if source_where == "docker" and target.in_docker:
            return f"{target.container},1433"
        if source_where == "vm" and target.in_vm:
            host = "localhost"
        elif source_where == "docker" and self.resolve_host(target) in ("127.0.0.1", "localhost", "::1"):
            host = "host.docker.internal"
        else:
            host = self.resolve_host(target)
        if target.port:
            return f"{host},{target.port}"
        return f"{host}\\{target.instance}" if target.instance else host

    def list_links(self, server: SqlServer) -> list[LinkInfo]:
        with self.connect(server) as conn:
            rows = conn.query(
                "SELECT name, data_source, provider FROM sys.servers WHERE is_linked = 1 ORDER BY name"
            )
        known = {self.link_name(t) for t in self.servers if t.id != server.id}
        return [LinkInfo(r["name"], r["data_source"] or "", r["provider"] or "", r["name"] in known) for r in rows]

    def add_link(self, source: SqlServer, target: SqlServer) -> str:
        """Create a linked server on ``source`` pointing at ``target`` and remember it."""
        name = self.link_name(target)
        data_source = self.data_source(target, source=source)
        remote_user = target.username if target.auth == "sql" else None
        remote_password = self.password(target) if target.auth == "sql" else None
        sql = [
            f"IF EXISTS (SELECT 1 FROM sys.servers WHERE name = {qs(name)}) EXEC sp_dropserver @server = {qs(name)}, @droplogins = 'droplogins';",
            f"EXEC sp_addlinkedserver @server = {qs(name)}, @srvproduct = N'', @provider = N'MSOLEDBSQL', @datasrc = {qs(data_source)};",
            f"EXEC sp_serveroption @server = {qs(name)}, @optname = 'data access', @optvalue = 'true';",
            f"EXEC sp_serveroption @server = {qs(name)}, @optname = 'rpc', @optvalue = 'true';",
            f"EXEC sp_serveroption @server = {qs(name)}, @optname = 'rpc out', @optvalue = 'true';",
        ]
        if remote_user:
            sql.append(
                f"EXEC sp_addlinkedsrvlogin @rmtsrvname = {qs(name)}, @useself = 'false', @locallogin = NULL,"
                f" @rmtuser = {qs(remote_user)}, @rmtpassword = {qs(remote_password or '')};"
            )
        else:
            sql.append(f"EXEC sp_addlinkedsrvlogin @rmtsrvname = {qs(name)}, @useself = 'true', @locallogin = NULL;")
        with self.connect(source) as conn:
            for statement in sql:
                try:
                    conn.execute(statement)
                except SqlError as exc:
                    # Fall back to the older native client provider if MSOLEDBSQL is missing.
                    if "MSOLEDBSQL" in statement and "provider" in str(exc).lower():
                        conn.execute(statement.replace("N'MSOLEDBSQL'", "N'SQLNCLI11'"))
                    else:
                        raise
        if target.id not in source.linked_to:
            source.linked_to.append(target.id)
            save_config(self.config)
        return name

    def remove_link(self, source: SqlServer, target: SqlServer) -> None:
        name = self.link_name(target)
        with self.connect(source) as conn:
            conn.execute(
                f"IF EXISTS (SELECT 1 FROM sys.servers WHERE name = {qs(name)})"
                f" EXEC sp_dropserver @server = {qs(name)}, @droplogins = 'droplogins';"
            )
        if target.id in source.linked_to:
            source.linked_to.remove(target.id)
            save_config(self.config)

    def test_link(self, source: SqlServer, target: SqlServer) -> str:
        name = self.link_name(target)
        with self.connect(source) as conn:
            conn.execute(f"EXEC sp_testlinkedserver {qs(name)};")
            return str(conn.scalar(f"SELECT TOP 1 name FROM {name}.master.sys.databases") or "ok")
