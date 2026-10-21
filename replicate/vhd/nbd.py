"""Mount images on the host through the privileged helper (for the Docker backend)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from replicate.util import CommandError, run, which

log = logging.getLogger(__name__)

HELPER = Path(__file__).resolve().parent / "mount_helper.py"
IMAGE_FORMATS = {"qcow2": "qcow2", "vhdx": "vhdx", "vpc": "vpc", "raw": "raw", "vdi": "vdi", "vmdk": "vmdk"}


class HostMountError(RuntimeError):
    pass


@dataclass
class HostMount:
    device: str
    target: str
    mounts: list[dict] = field(default_factory=list)

    @property
    def mountpoints(self) -> list[str]:
        return [m["mountpoint"] for m in self.mounts if m.get("mounted")]


def _system_python() -> str:
    """A Python the host trusts to run as root (not the AppImage's bundled one)."""
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3", "/run/current-system/sw/bin/python3"):
        if Path(candidate).exists():
            return candidate
    found = which("python3")
    if found and "appimage" not in found.lower() and ".mount_" not in found:
        return found
    return sys.executable


def _privileged(args: list[str], input_data: bytes | None = None) -> dict:
    python = _system_python()
    cmd = [python, str(HELPER), *args]
    if os.geteuid() != 0:
        helper = which("pkexec") or which("sudo")
        if not helper:
            raise HostMountError("Mounting an image needs pkexec (polkit) or sudo")
        cmd = [helper, *cmd] if helper.endswith("pkexec") else [helper, "-n", *cmd]
    try:
        out = run(cmd, timeout=300, input_text=input_data.decode("utf-8") if input_data is not None else None)
    except CommandError as exc:
        text = (exc.stdout or "").strip()
        try:
            data = json.loads(text.splitlines()[-1]) if text else {}
        except (ValueError, IndexError):
            data = {}
        if data.get("error"):
            raise HostMountError(data["error"]) from exc
        if exc.returncode in (126, 127) and "pkexec" in cmd[0]:
            raise HostMountError("Authentication was cancelled or refused") from exc
        raise HostMountError((exc.stderr or exc.stdout or str(exc)).strip()) from exc
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise HostMountError(f"The mount helper said something unexpected: {out[:200]}") from exc


def mount_image(image_path: str, image_format: str, target_dir: str, *, uid: int, gid: int = 0, readonly: bool = False) -> HostMount:
    args = ["mount", image_path, target_dir, "--format", IMAGE_FORMATS.get(image_format, "auto"), "--uid", str(uid), "--gid", str(gid)]
    if readonly:
        args.append("--readonly")
    data = _privileged(args)
    return HostMount(device=data.get("device", ""), target=target_dir, mounts=data.get("mounts", []))


def unmount_image(target_dir: str) -> None:
    _privileged(["umount", target_dir])


def is_mounted(target_dir: str) -> bool:
    return (Path(target_dir) / ".nbd").exists()


def scan_files(mountpoints: list[str], max_depth: int = 4, limit: int = 500) -> list[dict]:
    """Database files on the mounted filesystems, in the shape the guest script returns."""
    wanted = {".mdf", ".ndf", ".ldf", ".bak", ".trn", ".dif"}
    files: list[dict] = []
    for root_dir in mountpoints:
        base_depth = len(Path(root_dir).parts)
        for dirpath, dirnames, filenames in os.walk(root_dir):
            if len(Path(dirpath).parts) - base_depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(("$", "System Volume Information"))]
            for name in filenames:
                suffix = Path(name).suffix.lower()
                if suffix in wanted:
                    full = Path(dirpath) / name
                    try:
                        stat = full.stat()
                    except OSError:
                        continue
                    files.append({"path": str(full), "sizeBytes": stat.st_size, "kind": suffix[1:], "modified": ""})
                    if len(files) >= limit:
                        return files
    return files


def host_support() -> list[str]:
    """What this host is missing for mounting images (empty when all is well)."""
    problems = []
    if not which("qemu-nbd"):
        problems.append("qemu-nbd (qemu-utils) is not installed")
    try:
        filesystems = Path("/proc/filesystems").read_text()
    except OSError:
        filesystems = ""
    has_ntfs3 = "ntfs3" in filesystems or any(Path(f"/lib/modules/{v}/kernel/fs/ntfs3").exists() for v in os.listdir("/lib/modules")) if Path("/lib/modules").exists() else "ntfs3" in filesystems
    if not has_ntfs3 and not which("ntfs-3g", "mount.ntfs-3g"):
        problems.append("No NTFS driver: a kernel with ntfs3 (5.15+) or the ntfs-3g package")
    if not which("pkexec", "sudo"):
        problems.append("pkexec (polkit) is needed to mount images as root")
    return problems
