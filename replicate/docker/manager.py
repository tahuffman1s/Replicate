"""Replicate's SQL Server containers: create, run, register, remove."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from replicate.config import AppConfig, AppState, SqlServer, save_config
from replicate.docker.engine import CONTAINER_MOUNTS, Docker, DockerError, ContainerInfo, ContainerSpec, free_port, generate_sa_password
from replicate.secrets import SecretStore
from replicate.tasks import Progress

log = logging.getLogger(__name__)

NETWORK = "replicate"  # user-defined bridge so containers reach each other by name


class ContainerManager:
    def __init__(self, config: AppConfig, state: AppState, secrets: SecretStore):
        self.config = config
        self.state = state
        self.secrets = secrets
        self._docker: Docker | None = None

    @property
    def settings(self):
        return self.config.docker

    @property
    def docker(self) -> Docker:
        if self._docker is None or self._docker.command != self.settings.engine:
            self._docker = Docker(self.settings.engine)
        return self._docker

    @property
    def available(self) -> bool:
        return self.settings.enabled and self.docker.available

    # -- paths -------------------------------------------------------------
    def container_root(self, name: str) -> Path:
        return Path(self.settings.data_root).expanduser() / name

    def data_dir(self, name: str) -> Path:
        return self.container_root(name) / "data"

    def mounts_dir(self, name: str) -> Path:
        return self.container_root(name) / "mounts"

    def container_path(self, name: str, host_path: str) -> str:
        """Where a file under the container's mounts directory appears inside it."""
        root = str(self.mounts_dir(name).resolve())
        resolved = str(Path(host_path).resolve())
        if resolved == root or resolved.startswith(root + "/"):
            return CONTAINER_MOUNTS + resolved[len(root):]
        raise DockerError(f"{host_path} is not under {root}, so the container cannot see it")

    # -- registry links ------------------------------------------------------
    def server_for(self, name: str) -> SqlServer | None:
        return next((s for s in self.config.servers if s.in_docker and s.container == name), None)

    def containers(self) -> list[ContainerInfo]:
        if not self.available:
            return []
        return self.docker.list()

    def status_map(self) -> dict[str, str]:
        try:
            return {c.name: c.status for c in self.containers()}
        except DockerError as exc:
            log.debug("docker list failed: %s", exc)
            return {}

    # -- lifecycle -------------------------------------------------------------
    def ensure_network(self) -> None:
        try:
            self.docker._call("network", "inspect", NETWORK, timeout=15)  # noqa: SLF001
        except DockerError:
            self.docker._call("network", "create", NETWORK, timeout=30)  # noqa: SLF001

    def create(self, name: str, *, port: int = 0, memory_mib: int | None = None, image: str = "",
               sa_password: str = "", progress: Progress | None = None) -> SqlServer:
        """Pull the image if needed, create and start the container, wait for SQL Server, register it."""
        if not self.available:
            raise DockerError("Docker is not available")
        if not name or not all(ch.isalnum() or ch in "-_." for ch in name):
            raise DockerError("Container names use letters, digits, dots, dashes and underscores")
        if self.docker.inspect(name) is not None:
            raise DockerError(f"A container named {name} already exists")
        port = port or free_port()
        image = image or self.settings.image
        memory = memory_mib if memory_mib is not None else self.settings.memory_mib
        sa_password = sa_password or generate_sa_password()
        say = progress.update if progress else (lambda m, f=None: None)

        say(f"Pulling {image} (first time only, about 500 MB)", 0.05)
        self.docker.pull(image, progress=lambda line: say(line, None))
        say("Creating the container", 0.6)
        self.ensure_network()
        spec = ContainerSpec(name=name, port=port, sa_password=sa_password, image=image, memory_mib=memory,
                             data_dir=str(self.data_dir(name)), mounts_dir=str(self.mounts_dir(name)))
        self.docker.create(spec)
        self.docker._call("network", "connect", NETWORK, name, timeout=30)  # noqa: SLF001
        say("Starting SQL Server", 0.7)
        self.docker.start(name)
        if not self.docker.wait_ready(name, timeout=300, progress=lambda m: say(m, 0.85)):
            raise DockerError(f"SQL Server in {name} did not come up in time; see its log")

        server = SqlServer(name=f"{name} (Docker)", host="127.0.0.1", port=port, auth="sql", username="sa",
                           vm_managed=False, service_name="", backend="docker", container=name)
        self.config.servers.append(server)
        self.secrets.set(server.secret_key, sa_password)
        save_config(self.config)
        say("Ready", 1.0)
        return server

    def start(self, name: str) -> None:
        self.docker.start(name)

    def stop(self, name: str) -> None:
        self.docker.stop(name)

    def remove(self, name: str, *, delete_data: bool) -> None:
        if self.docker.inspect(name) is not None:
            self.docker.remove(name, force=True)
        server = self.server_for(name)
        if server is not None:
            for other in self.config.servers:
                if server.id in other.linked_to:
                    other.linked_to.remove(server.id)
            self.config.servers.remove(server)
            self.secrets.delete(server.secret_key)
            save_config(self.config)
        if delete_data:
            shutil.rmtree(self.container_root(name), ignore_errors=True)

    def logs(self, name: str, tail: int = 200) -> str:
        return self.docker.logs(name, tail=tail)
