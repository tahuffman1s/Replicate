"""Attach images for a container: clone, mount on the host, let the container see it."""

from __future__ import annotations

import datetime as dt
import logging

from replicate.config import AppState, Mount, save_state
from replicate.docker.engine import MSSQL_UID
from replicate.docker.manager import ContainerManager
from replicate.vhd import nbd
from replicate.vhd.clones import create_clone, delete_clone
from replicate.vhd.images import ImageError, ImageInfo, inspect

log = logging.getLogger(__name__)


class DockerMountError(RuntimeError):
    pass


class DockerMounter:
    def __init__(self, containers: ContainerManager, state: AppState, clones_dir: str):
        self.containers = containers
        self.state = state
        self.clones_dir = clones_dir

    def mounts(self, container: str | None = None) -> list[Mount]:
        return [m for m in self.state.mounts if m.backend == "docker" and (container is None or m.container == container)]

    def attach(self, image: ImageInfo, container: str, *, clone: bool = True, label: str = "", readonly: bool = False) -> tuple[Mount, list[dict]]:
        """Clone (unless told not to), mount under the container's mounts dir, list the database files."""
        if image.error:
            raise DockerMountError(f"{image.name}: {image.error}")
        if image.is_differencing and image.format != "qcow2":
            raise ImageError(f"{image.name} is a differencing disk; QEMU cannot open its parent chain")
        if clone:
            disk_path = str(create_clone(image, self.clones_dir, label or image.name))
            fmt = "qcow2"
        else:
            disk_path, fmt = image.path, image.format
        mount = Mount(image_path=image.path, clone_path=disk_path, backend="docker", container=container,
                      created_at=dt.datetime.now().isoformat(timespec="seconds"), label=label or image.name)
        target = self.containers.mounts_dir(container) / mount.id
        try:
            result = nbd.mount_image(disk_path, fmt, str(target), uid=MSSQL_UID, gid=0, readonly=readonly)
        except nbd.HostMountError as exc:
            if clone:
                delete_clone(disk_path)
            raise DockerMountError(str(exc)) from exc
        mount.host_dir = str(target)
        mount.mountpoints = result.mountpoints
        mount.drive_letters = [self.containers.container_path(container, mp) for mp in result.mountpoints]
        self.state.mounts.append(mount)
        save_state(self.state)
        return mount, nbd.scan_files(result.mountpoints)

    def files(self, mount: Mount) -> list[dict]:
        return nbd.scan_files(mount.mountpoints)

    def container_path(self, mount: Mount, host_path: str) -> str:
        return self.containers.container_path(mount.container, host_path)

    def detach(self, mount: Mount, *, delete_clone_file: bool = False) -> None:
        if mount.host_dir and nbd.is_mounted(mount.host_dir):
            try:
                nbd.unmount_image(mount.host_dir)
            except nbd.HostMountError as exc:
                raise DockerMountError(str(exc)) from exc
        if mount in self.state.mounts:
            self.state.mounts.remove(mount)
        save_state(self.state)
        if delete_clone_file and not mount.is_direct:
            delete_clone(mount.clone_path)

    def reconcile(self) -> None:
        """Forget mounts whose host directory is gone (a reboot unmounts everything)."""
        stale = [m for m in self.mounts() if not (m.host_dir and nbd.is_mounted(m.host_dir))]
        for m in stale:
            self.state.mounts.remove(m)
        if stale:
            save_state(self.state)

    def image_for(self, mount: Mount) -> ImageInfo:
        return inspect(mount.image_path)
