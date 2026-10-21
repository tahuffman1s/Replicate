"""Host-side checks and privileged file operations.

libvirt's system instance keeps VM disks in ``/var/lib/libvirt/images``,
which a normal user cannot write. The few steps that touch it (creating the
system disk, dropping the answers CD) go through ``pkexec`` when the
directory is not writable, so the desktop asks for the password once instead
of Replicate running as root.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from replicate.util import CommandError, run, which


class PrivilegeError(RuntimeError):
    pass


def writable(path: Path) -> bool:
    probe = path if path.exists() else path.parent
    return os.access(probe, os.W_OK)


def privileged(cmd: list[str], *, path_hint: Path | None = None) -> str:
    """Run ``cmd`` directly if ``path_hint`` is writable, else through pkexec/sudo."""
    if path_hint is not None and writable(path_hint):
        return run(cmd)
    resolved = which(cmd[0])
    if resolved:
        cmd = [resolved, *cmd[1:]]
    for helper in ("pkexec", "sudo"):
        binary = which(helper)
        if not binary:
            continue
        full = [binary, *cmd] if helper == "pkexec" else [binary, "-n", *cmd]
        try:
            return run(full, timeout=600)
        except CommandError as exc:
            if helper == "pkexec" and exc.returncode in (126, 127):
                raise PrivilegeError("Authentication was cancelled or refused") from exc
            if helper == "sudo":
                raise PrivilegeError(f"Need write access to {path_hint or cmd[-1]}: {exc}") from exc
            raise PrivilegeError(str(exc)) from exc
    raise PrivilegeError("Neither pkexec nor sudo is available to write to the libvirt images directory")


def ensure_dir(path: Path, owner_uid: int | None = None, mode: str = "755") -> None:
    if path.is_dir():
        return
    if writable(path.parent):
        path.mkdir(parents=True, exist_ok=True)
        return
    cmd = ["install", "-d", "-m", mode]
    if owner_uid is not None:
        cmd += ["-o", str(owner_uid), "-g", str(os.getgid())]
    cmd.append(str(path))
    privileged(cmd)


def install_file(src: Path, dest: Path, mode: str = "644") -> None:
    if writable(dest):
        shutil.copyfile(src, dest)
        os.chmod(dest, int(mode, 8))
        return
    privileged(["install", "-m", mode, str(src), str(dest)], path_hint=dest)


def remove_file(path: Path) -> None:
    if not path.exists():
        return
    if writable(path):
        path.unlink()
        return
    privileged(["rm", "-f", str(path)], path_hint=path)


def traversable_by_others(path: Path) -> bool:
    """True if every directory on the way to ``path`` has o+x, so the qemu
    user libvirt runs the VM as can open files in it."""
    current = path if path.is_dir() else path.parent
    current = current.resolve()
    while True:
        try:
            mode = current.stat().st_mode
        except OSError:
            return False
        if not mode & stat.S_IXOTH:
            return False
        if current.parent == current:
            return True
        current = current.parent


@dataclass
class Preflight:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def problem(self, text: str) -> None:
        self.ok = False
        self.problems.append(text)


def preflight(vm_uri: str) -> Preflight:
    """What the host is missing for Replicate to work."""
    from replicate.vm.virsh import Virsh

    result = Preflight()
    if not which("virsh"):
        result.problem("virsh (libvirt client) is not installed")
    if not which("qemu-img"):
        result.problem("qemu-img (qemu-utils) is not installed")
    if not which("xorriso", "genisoimage", "mkisofs"):
        result.problem("No ISO tool found (xorriso, genisoimage or mkisofs); needed to create the VM")
    if not Path("/dev/kvm").exists():
        result.problem("/dev/kvm is missing: enable virtualisation in the firmware and load the kvm module")
    elif not os.access("/dev/kvm", os.R_OK | os.W_OK):
        result.notes.append("/dev/kvm is not accessible to your user; add yourself to the kvm group")
    if which("virsh"):
        ok, message = Virsh(vm_uri).connection_ok()
        if not ok:
            result.problem(message)
    else:
        pass  # already a problem above
    if not which("virt-viewer", "remote-viewer", "virt-manager"):
        result.notes.append("Install virt-viewer to open the VM's screen from Replicate")
    from replicate.docker.engine import Docker
    from replicate.vhd import nbd

    ok, message = Docker("auto").ping()
    if not ok:
        result.notes.append(f"Docker: {message} (needed only for SQL Server in containers)")
    for problem in nbd.host_support():
        result.notes.append(f"Images for containers: {problem}")
    from replicate.vhd import shares

    for problem in shares.cifs_support():
        result.notes.append(f"Network shares: {problem}")
    return result
