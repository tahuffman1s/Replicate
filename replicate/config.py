"""Configuration and persistent state.

Configuration (``~/.config/replicate/config.json``) is what the user set up:
the VM, the SQL Servers, where VHD images live. State
(``~/.local/share/replicate/state.json``) is what Replicate did: which clones
are attached where, which databases came from them. Passwords are kept out of
both; see :mod:`replicate.secrets`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from replicate import paths

CONFIG_VERSION = 1


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# Models


@dataclass
class VmSettings:
    """The Windows VM that hosts the SQL Server instances."""

    name: str = "replicate-sql"
    uri: str = "qemu:///system"
    memory_mib: int = 8192
    vcpus: int = 4
    disk_size: str = "100G"
    images_dir: str = "/var/lib/libvirt/images"
    windows_iso: str = ""
    virtio_win_iso: str = ""
    network: str = "default"
    # Instances Setup-SqlHost.ps1 installs. MSSQLSERVER is the default instance.
    sql_instances: list[str] = field(default_factory=lambda: ["MSSQLSERVER"])
    # SQL login the app itself connects with (sysadmin, created by the setup script).
    admin_login: str = "replicate"
    # Windows account created by the unattended install.
    windows_user: str = "replicate"
    hyperv_enlightenments: bool = True
    secure_boot: bool = True

    @property
    def disk_path(self) -> str:
        return str(Path(self.images_dir) / f"{self.name}.qcow2")

    @property
    def answers_iso(self) -> str:
        return str(Path(self.images_dir) / f"{self.name}-answers.iso")

    @property
    def setup_iso(self) -> str:
        return str(Path(self.images_dir) / f"{self.name}-setup.iso")


@dataclass
class SqlServer:
    """A SQL Server instance Replicate knows about.

    ``vm_managed`` servers run inside the Replicate VM: their host is the VM's
    address (resolved at connect time) and their Windows service can be
    started and stopped from the app. Any other reachable SQL Server can be
    added too; it just has no service controls.
    """

    id: str = field(default_factory=new_id)
    name: str = "SQL Server"
    host: str = "vm"  # "vm" means "the Replicate VM, whatever its address is"
    port: int = 1433
    instance: str = ""  # named instance, resolved through SQL Browser when port is 0
    auth: str = "sql"  # "sql", "windows" (NTLM with a password) or "kerberos" (the host's ticket)
    domain: str = ""  # Windows domain, NetBIOS or DNS name; blank for a local account or SQL login
    username: str = "replicate"
    spn: str = ""  # Kerberos service principal override; blank means MSSQLSvc/host:port
    database: str = "master"
    vm_managed: bool = True
    service_name: str = "MSSQLSERVER"  # Windows service, e.g. MSSQL$ACCT
    backend: str = ""  # "vm", "docker" or "" (a server somewhere else); "" + vm_managed means "vm"
    container: str = ""  # Docker container name when backend == "docker"
    linked_to: list[str] = field(default_factory=list)  # ids of linked servers
    notes: str = ""
    enabled: bool = True

    @property
    def where(self) -> str:
        """vm, docker or remote."""
        if self.backend:
            return self.backend
        return "vm" if self.vm_managed else "remote"

    @property
    def in_docker(self) -> bool:
        return self.where == "docker"

    @property
    def in_vm(self) -> bool:
        return self.where == "vm"

    @property
    def account(self) -> str:
        """How the login reads: user, DOMAIN\\user or user@REALM."""
        if self.auth == "kerberos":
            from replicate.sql.kerberos import principal_for

            return principal_for(self.username, self.domain)
        if self.auth == "windows" and self.domain and "\\" not in self.username:
            return f"{self.domain}\\{self.username}"
        return self.username

    @property
    def auth_label(self) -> str:
        return {"sql": "SQL login", "windows": "Windows", "kerberos": "Kerberos"}.get(self.auth, self.auth)

    @property
    def display_instance(self) -> str:
        return f".\\{self.instance}" if self.instance else "default instance"

    @property
    def secret_key(self) -> str:
        return f"sql:{self.id}"


@dataclass
class Share:
    """A network share Replicate mounts itself (kernel CIFS), so its images are
    readable by QEMU and the root mount helper, not just by the desktop."""

    id: str = field(default_factory=new_id)
    url: str = ""  # smb://server/share/sub/folder
    auth: str = "kerberos"  # kerberos (this computer's ticket), password, or guest
    domain: str = ""
    username: str = ""
    readonly: bool = True
    label: str = ""

    @property
    def secret_key(self) -> str:
        return f"share:{self.id}"


@dataclass
class VhdSettings:
    image_dirs: list[str] = field(default_factory=list)
    shares: list[Share] = field(default_factory=list)
    scan_depth: int = 0  # folder levels to look into under each image folder; 0 means all of them
    clones_dir: str = str(paths.CLONES_DIR)
    # Where restored databases' files go in the guest when restoring from .bak.
    # Empty: the instance's default data directory. "clone": onto the clone disk.
    restore_target: str = "default"


@dataclass
class MonitorSettings:
    interval_seconds: float = 2.0
    history_points: int = 450  # 15 minutes at 2 s
    trace_events: list[str] = field(default_factory=lambda: ["sql_batch_completed", "rpc_completed"])
    trace_min_duration_ms: int = 0
    trace_max_rows: int = 2000
    trace_exclude_apps: list[str] = field(default_factory=lambda: ["Replicate"])


@dataclass
class DockerSettings:
    """SQL Server for Linux in containers: no Windows, no VM, but no Windows authentication either."""

    enabled: bool = True
    engine: str = "docker"  # docker, podman, or auto
    image: str = "mcr.microsoft.com/mssql/server:2022-latest"
    data_root: str = str(paths.DATA_DIR / "docker")  # <data_root>/<container>/data -> /var/opt/mssql
    memory_mib: int = 4096


@dataclass
class KerberosSettings:
    # Hand ticket requests to Kerberus (github.com/tahuffman1s/kerberus) when it is installed.
    use_kerberus: bool = True
    # Where Kerberus is, when it is not on PATH or in a desktop entry (e.g. an AppImage in ~/Applications).
    kerberus_path: str = ""


@dataclass
class UiSettings:
    window_width: int = 1180
    window_height: int = 760
    last_page: str = "overview"
    color_scheme: str = "default"  # default, light, dark


@dataclass
class AppConfig:
    version: int = CONFIG_VERSION
    vm: VmSettings = field(default_factory=VmSettings)
    servers: list[SqlServer] = field(default_factory=list)
    vhd: VhdSettings = field(default_factory=VhdSettings)
    monitor: MonitorSettings = field(default_factory=MonitorSettings)
    kerberos: KerberosSettings = field(default_factory=KerberosSettings)
    docker: DockerSettings = field(default_factory=DockerSettings)
    ui: UiSettings = field(default_factory=UiSettings)

    def server(self, server_id: str) -> SqlServer | None:
        return next((s for s in self.servers if s.id == server_id), None)

    def vm_servers(self) -> list[SqlServer]:
        return [s for s in self.servers if s.in_vm]

    def docker_servers(self) -> list[SqlServer]:
        return [s for s in self.servers if s.in_docker]


# --------------------------------------------------------------------------
# State


@dataclass
class RestoredDatabase:
    server_id: str
    name: str
    mode: str  # "attach" or "restore"
    files: list[str] = field(default_factory=list)


@dataclass
class Mount:
    """A clone (or image) attached to the VM as a disk."""

    id: str = field(default_factory=new_id)
    image_path: str = ""
    clone_path: str = ""  # same as image_path when mounted directly
    vm_name: str = ""
    target_dev: str = ""  # vdb, vdc, ...
    serial: str = ""
    drive_letters: list[str] = field(default_factory=list)
    backend: str = "vm"  # "vm": hot-plugged into the VM; "docker": mounted on the host for a container
    container: str = ""  # the container it was mounted for
    host_dir: str = ""  # where the helper mounted it on the host (backend docker)
    mountpoints: list[str] = field(default_factory=list)
    databases: list[RestoredDatabase] = field(default_factory=list)
    created_at: str = ""
    label: str = ""

    @property
    def is_direct(self) -> bool:
        return self.clone_path == self.image_path


@dataclass
class AppState:
    mounts: list[Mount] = field(default_factory=list)
    vm_install: dict[str, Any] = field(default_factory=dict)  # phase, started_at, message


# --------------------------------------------------------------------------
# (De)serialisation


def _to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


def _from_dict(cls: type, data: Any) -> Any:
    """Build ``cls`` from ``data``, ignoring unknown keys and filling defaults."""
    if data is None:
        return cls()
    kwargs: dict[str, Any] = {}
    hints = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in hints:
            continue
        ftype = hints[key].type
        kwargs[key] = _coerce(ftype, value)
    return cls(**kwargs)


_NESTED: dict[str, type] = {}


def _coerce(ftype: Any, value: Any) -> Any:
    name = ftype if isinstance(ftype, str) else getattr(ftype, "__name__", str(ftype))
    if name in _NESTED and isinstance(value, dict):
        return _from_dict(_NESTED[name], value)
    if name.startswith("list[") and isinstance(value, list):
        inner = name[5:-1]
        if inner in _NESTED:
            return [_from_dict(_NESTED[inner], v) for v in value if isinstance(v, dict)]
        return list(value)
    return value


for _cls in (VmSettings, SqlServer, Share, VhdSettings, MonitorSettings, KerberosSettings, DockerSettings, UiSettings, RestoredDatabase, Mount):
    _NESTED[_cls.__name__] = _cls


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config(path: Path | None = None) -> AppConfig:
    path = path or paths.CONFIG_FILE
    if not path.exists():
        return AppConfig()
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return _from_dict(AppConfig, data)


def save_config(config: AppConfig, path: Path | None = None) -> None:
    path = path or paths.CONFIG_FILE
    config.version = CONFIG_VERSION
    _atomic_write(path, json.dumps(_to_dict(config), indent=2) + "\n")


def load_state(path: Path | None = None) -> AppState:
    path = path or paths.STATE_FILE
    if not path.exists():
        return AppState()
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return _from_dict(AppState, data)


def save_state(state: AppState, path: Path | None = None) -> None:
    path = path or paths.STATE_FILE
    _atomic_write(path, json.dumps(_to_dict(state), indent=2) + "\n", mode=0o644)


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    return _to_dict(config)
