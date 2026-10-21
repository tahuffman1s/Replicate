"""Run SQL Server for Linux in Docker (or Podman) containers.

A container is Replicate's own: it carries labels so it can be found again,
its ``/var/opt/mssql`` lives in a directory under the data root (so the
databases survive container recreation), and a second bind mount with slave
propagation (``/mnt/replicate``) lets disks mounted on the host after the
container started show up inside it, which is how VHD clones get in.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from replicate.util import CommandError, run, which

log = logging.getLogger(__name__)

LABEL = "io.github.tahuffman1s.replicate"
MSSQL_UID = 10001  # the mssql user inside Microsoft's image
CONTAINER_MOUNTS = "/mnt/replicate"  # where host mounts appear inside the container
CONTAINER_DATA = "/var/opt/mssql"
DEFAULT_IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"


class DockerError(RuntimeError):
    pass


@dataclass
class ContainerInfo:
    name: str
    id: str = ""
    status: str = "unknown"  # created, running, paused, exited, ...
    image: str = ""
    port: int = 0
    data_dir: str = ""
    mounts_dir: str = ""
    labels: dict = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass
class ContainerSpec:
    name: str
    port: int
    sa_password: str
    image: str = DEFAULT_IMAGE
    memory_mib: int = 4096
    data_dir: str = ""  # host dir bind-mounted as /var/opt/mssql
    mounts_dir: str = ""  # host dir bind-mounted as /mnt/replicate (slave propagation)
    collation: str = ""
    extra_env: dict = field(default_factory=dict)


def generate_sa_password() -> str:
    """Meets SQL Server's policy: 8+ chars from three of four classes."""
    return secrets.token_urlsafe(18) + "Aa1!"


def free_port(start: int = 14330, end: int = 14400) -> int:
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise DockerError("No free TCP port between 14330 and 14400")


class Docker:
    def __init__(self, command: str = "docker"):
        self.command = command
        self.binary = which(command) or (which("podman") if command == "auto" else None)
        if command == "auto":
            self.binary = which("docker") or which("podman")

    # -- plumbing ----------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.binary is not None

    @property
    def is_podman(self) -> bool:
        return bool(self.binary and self.binary.endswith("podman"))

    def _call(self, *args: str, check: bool = True, timeout: float | None = 120, input_text: str | None = None) -> str:
        if not self.binary:
            raise DockerError("Docker is not installed (or not on PATH)")
        try:
            return run([self.binary, *args], check=check, timeout=timeout, input_text=input_text)
        except CommandError as exc:
            message = (exc.stderr or exc.stdout).strip()
            if "permission denied" in message.lower() and "docker.sock" in message:
                raise DockerError("Your user may not talk to Docker: add it to the docker group and sign in again") from exc
            if "Cannot connect to the Docker daemon" in message or "Is the docker daemon running" in message:
                raise DockerError("The Docker daemon is not running") from exc
            raise DockerError(message or str(exc)) from exc

    def ping(self) -> tuple[bool, str]:
        if not self.binary:
            return False, "Docker is not installed"
        try:
            version = self._call("version", "--format", "{{.Server.Version}}", timeout=15).strip()
            return True, f"{Path(self.binary).name} {version}"
        except DockerError as exc:
            return False, str(exc)

    # -- containers --------------------------------------------------------
    def list(self) -> list[ContainerInfo]:
        out = self._call("ps", "-a", "--filter", f"label={LABEL}=1", "--format", "{{json .}}", timeout=30)
        found: list[ContainerInfo] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = data.get("Names") or data.get("Name") or ""
            if isinstance(name, list):
                name = name[0] if name else ""
            found.append(ContainerInfo(name=name.lstrip("/"), id=data.get("ID") or data.get("Id", ""), status=_status_word(data.get("State") or data.get("Status", "")), image=data.get("Image", "")))
        for info in found:
            self._fill(info)
        return found

    def _fill(self, info: ContainerInfo) -> None:
        try:
            raw = self._call("inspect", info.name, timeout=30)
            data = json.loads(raw)[0]
        except (DockerError, ValueError, IndexError):
            return
        info.labels = data.get("Config", {}).get("Labels", {}) or {}
        info.status = (data.get("State", {}) or {}).get("Status", info.status)
        info.port = int(info.labels.get(f"{LABEL}.port", 0) or 0)
        for mount in data.get("Mounts", []) or []:
            if mount.get("Destination") == CONTAINER_DATA:
                info.data_dir = mount.get("Source", "")
            elif mount.get("Destination") == CONTAINER_MOUNTS:
                info.mounts_dir = mount.get("Source", "")

    def inspect(self, name: str) -> ContainerInfo | None:
        info = ContainerInfo(name=name)
        try:
            raw = self._call("inspect", name, timeout=30)
        except DockerError:
            return None
        try:
            data = json.loads(raw)[0]
        except (ValueError, IndexError):
            return None
        info.id = data.get("Id", "")
        info.image = data.get("Config", {}).get("Image", "")
        self._fill(info)
        return info

    def status(self, name: str) -> str:
        info = self.inspect(name)
        return info.status if info else "absent"

    def create_args(self, spec: ContainerSpec) -> list[str]:
        """The ``docker create`` command line, without the binary (testable)."""
        args = [
            "create", "--name", spec.name, "--hostname", spec.name,
            "--label", f"{LABEL}=1", "--label", f"{LABEL}.port={spec.port}",
            "-p", f"127.0.0.1:{spec.port}:1433",
            "-e", "ACCEPT_EULA=Y", "-e", "MSSQL_PID=Developer",
            "-e", f"MSSQL_SA_PASSWORD={spec.sa_password}",
            "--restart", "unless-stopped",
        ]
        if spec.memory_mib:
            args += ["--memory", f"{int(spec.memory_mib)}m", "-e", f"MSSQL_MEMORY_LIMIT_MB={int(spec.memory_mib * 0.8)}"]
        if spec.collation:
            args += ["-e", f"MSSQL_COLLATION={spec.collation}"]
        for key, value in spec.extra_env.items():
            args += ["-e", f"{key}={value}"]
        if spec.data_dir:
            args += ["--mount", f"type=bind,source={spec.data_dir},target={CONTAINER_DATA}"]
        if spec.mounts_dir:
            # rslave: mounts the host adds under mounts_dir later show up inside.
            args += ["--mount", f"type=bind,source={spec.mounts_dir},target={CONTAINER_MOUNTS},bind-propagation=rslave"]
        # Lets containers reach SQL Servers on the host (and each other) by one name.
        args += ["--add-host", "host.docker.internal:host-gateway"]
        args.append(spec.image)
        return args

    def pull(self, image: str, progress: Callable[[str], None] | None = None, timeout: float = 3600) -> None:
        if not self.binary:
            raise DockerError("Docker is not installed")
        proc = subprocess.Popen([self.binary, "pull", image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last = ""
        deadline = time.monotonic() + timeout
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line and line != last and progress:
                progress(line[:120])
                last = line
            if time.monotonic() > deadline:
                proc.kill()
                raise DockerError(f"Pulling {image} took longer than {int(timeout / 60)} minutes")
        if proc.wait() != 0:
            raise DockerError(f"Pulling {image} failed: {last}")

    def create(self, spec: ContainerSpec) -> str:
        for directory in (spec.data_dir, spec.mounts_dir):
            if directory:
                path = Path(directory)
                path.mkdir(parents=True, exist_ok=True)
                # The mssql user inside must be able to write; the host user keeps access through the group.
                try:
                    path.chmod(0o777 if directory == spec.data_dir else 0o755)
                except OSError:
                    pass
        return self._call(*self.create_args(spec), timeout=120).strip()

    def start(self, name: str) -> None:
        self._call("start", name)

    def stop(self, name: str, timeout_s: int = 60) -> None:
        self._call("stop", "-t", str(timeout_s), name, timeout=timeout_s + 30)

    def remove(self, name: str, *, force: bool = False) -> None:
        args = ["rm", "-f", name] if force else ["rm", name]
        self._call(*args)

    def logs(self, name: str, tail: int = 200) -> str:
        return self._call("logs", "--tail", str(tail), name, check=False, timeout=30)

    def exec(self, name: str, *cmd: str, user: str | None = None, timeout: float = 120) -> str:
        args = ["exec"]
        if user:
            args += ["-u", user]
        return self._call(*args, name, *cmd, timeout=timeout)

    def copy_in(self, name: str, host_path: str, container_path: str) -> None:
        self._call("cp", host_path, f"{name}:{container_path}", timeout=None)

    def ready(self, name: str) -> bool:
        """SQL Server inside says it is listening (from the container log)."""
        text = self.logs(name, tail=300)
        return "SQL Server is now ready for client connections" in text or "Recovery is complete" in text

    def wait_ready(self, name: str, timeout: float = 240, progress: Callable[[str], None] | None = None) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status(name)
            if status not in ("running", "created"):
                raise DockerError(f"The container stopped ({status}); its log says:\n{self.logs(name, tail=20)}")
            if self.ready(name):
                return True
            if progress:
                progress(f"Waiting for SQL Server in {name} to start")
            time.sleep(3)
        return False


def _status_word(text: str) -> str:
    text = (text or "").strip().lower()
    for word in ("running", "paused", "exited", "created", "restarting", "dead", "removing"):
        if text.startswith(word) or text.startswith("up") and word == "running":
            return word
    match = re.match(r"^(\w+)", text)
    return match.group(1) if match else "unknown"
