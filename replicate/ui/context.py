"""What every page shares: configuration, state, services, and a tiny event bus."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from replicate import paths
from replicate.config import AppConfig, AppState, load_config, load_state, save_config, save_state
from replicate.docker.manager import ContainerManager
from replicate.secrets import SecretStore
from replicate.sql.servers import ServerRegistry
from replicate.vhd.docker_mounter import DockerMounter
from replicate.vhd.mounter import Mounter
from replicate.vm.manager import VmManager, VmStatus

log = logging.getLogger(__name__)

# Topics on the bus.
VM_STATUS = "vm-status"  # payload: VmStatus
SERVERS_CHANGED = "servers-changed"
MOUNTS_CHANGED = "mounts-changed"
CONTAINERS_CHANGED = "containers-changed"
CONFIG_CHANGED = "config-changed"
INSTALL_PROGRESS = "install-progress"  # payload: (message, fraction)
TOAST = "toast"  # payload: str


class AppContext:
    def __init__(self) -> None:
        paths.ensure_dirs()
        self.config: AppConfig = load_config()
        self.state: AppState = load_state()
        self.secrets = SecretStore()
        self.vm = VmManager(self.config, self.state, self.secrets)
        self.registry = ServerRegistry(self.config, self.secrets, self.vm)
        self.mounter = Mounter(self.vm, self.state, self.config.vhd.clones_dir)
        self.containers = ContainerManager(self.config, self.state, self.secrets)
        self.docker_mounter = DockerMounter(self.containers, self.state, self.config.vhd.clones_dir)
        self.docker_mounter.reconcile()
        self.last_status: VmStatus = VmStatus()
        self._subs: dict[str, list[Callable[..., Any]]] = defaultdict(list)

    # -- persistence -------------------------------------------------------
    def save_config(self) -> None:
        save_config(self.config)
        self.mounter.clones_dir = self.config.vhd.clones_dir
        self.docker_mounter.clones_dir = self.config.vhd.clones_dir
        self.emit(CONFIG_CHANGED)

    def save_state(self) -> None:
        save_state(self.state)

    # -- bus ---------------------------------------------------------------
    def subscribe(self, topic: str, callback: Callable[..., Any]) -> Callable[[], None]:
        self._subs[topic].append(callback)

        def unsubscribe() -> None:
            if callback in self._subs[topic]:
                self._subs[topic].remove(callback)

        return unsubscribe

    def emit(self, topic: str, *payload: Any) -> None:
        for callback in list(self._subs.get(topic, [])):
            try:
                callback(*payload)
            except Exception:  # noqa: BLE001
                log.exception("listener for %s failed", topic)

    def toast(self, text: str) -> None:
        self.emit(TOAST, text)

    def kerberus(self):
        """Kerberus, if the user wants it used and it can be found (cheap; re-looked-up each call)."""
        from replicate.sql import kerberus_app

        if not self.config.kerberos.use_kerberus:
            return kerberus_app.Kerberus()
        return kerberus_app.find_kerberus(self.config.kerberos.kerberus_path)
