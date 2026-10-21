#!/usr/bin/env python3
"""Mount a disk image on the host, as root. Run through pkexec by Replicate.

    mount_helper.py mount IMAGE TARGET_DIR [--format qcow2] [--uid N] [--gid N] [--readonly]
    mount_helper.py umount TARGET_DIR
    mount_helper.py mount-share //SERVER/SHARE TARGET_DIR --options OPTS [--credentials-stdin]
    mount_helper.py umount-share TARGET_DIR

``mount`` attaches IMAGE to a free /dev/nbdN with qemu-nbd, waits for its
partitions, mounts each filesystem it understands under TARGET_DIR/p<N> (NTFS
through the kernel's ntfs3 driver, ntfs-3g when that is missing; ext4, xfs,
exfat and vfat as they are), owned by --uid/--gid so SQL Server inside a
container can use the files, and prints JSON describing what it did. A
record of the device goes to TARGET_DIR/.nbd so ``umount`` can undo it.

Only the standard library and the host's mount, qemu-nbd and modprobe are
used, so the same file works from a package, a checkout or the AppImage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

NBD_COUNT = 16


def sh(*cmd: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=check, timeout=timeout)


def fail(message: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": message}))
    sys.exit(code)


def ensure_nbd() -> None:
    if not Path("/sys/module/nbd").exists():
        proc = sh("modprobe", "nbd", f"max_part={NBD_COUNT}", check=False)
        if proc.returncode != 0:
            fail("The nbd kernel module could not be loaded: " + proc.stderr.strip())
    for _ in range(20):
        if Path("/dev/nbd0").exists():
            return
        time.sleep(0.2)
    fail("/dev/nbd0 did not appear after loading the nbd module")


def free_nbd() -> str:
    for n in range(NBD_COUNT):
        dev = Path(f"/dev/nbd{n}")
        if not dev.exists():
            continue
        pid = Path(f"/sys/block/nbd{n}/pid")
        size = Path(f"/sys/block/nbd{n}/size")
        if not pid.exists() and size.exists() and size.read_text().strip() == "0":
            return str(dev)
    fail("All nbd devices are in use")
    return ""


def partitions(device: str) -> list[dict]:
    """lsblk's view of the device: partitions with their filesystem types."""
    for _ in range(40):
        proc = sh("lsblk", "-J", "-o", "NAME,FSTYPE,SIZE,LABEL,PATH", device, check=False)
        if proc.returncode == 0:
            data = json.loads(proc.stdout or "{}")
            devices = data.get("blockdevices", [])
            if devices:
                children = devices[0].get("children") or []
                if children and any(c.get("fstype") for c in children):
                    return children
                if not children and devices[0].get("fstype"):
                    return [devices[0]]  # a filesystem on the whole disk
        sh("partprobe", device, check=False) if shutil.which("partprobe") else None
        time.sleep(0.25)
    return []


def mount_one(part: dict, target: Path, uid: int, gid: int, readonly: bool) -> dict:
    fstype = (part.get("fstype") or "").lower()
    path = part["path"]
    target.mkdir(parents=True, exist_ok=True)
    ro = ["-o", "ro"] if readonly else []
    attempts: list[list[str]] = []
    if fstype == "ntfs":
        owner = f"uid={uid},gid={gid},umask=0007,noatime"
        attempts.append(["mount", "-t", "ntfs3", "-o", owner + (",ro" if readonly else ""), path, str(target)])
        attempts.append(["mount", "-t", "ntfs-3g", "-o", owner + ",big_writes" + (",ro" if readonly else ""), path, str(target)])
        attempts.append(["mount", "-t", "ntfs", "-o", owner + (",ro" if readonly else ""), path, str(target)])
    elif fstype in ("vfat", "exfat"):
        attempts.append(["mount", "-t", fstype, "-o", f"uid={uid},gid={gid},umask=0007" + (",ro" if readonly else ""), path, str(target)])
    elif fstype in ("ext4", "ext3", "ext2", "xfs", "btrfs"):
        attempts.append(["mount", "-t", fstype, *ro, path, str(target)])
    else:
        return {"partition": path, "fstype": fstype or "unknown", "mounted": False, "error": "not a filesystem Replicate mounts"}
    errors = []
    for cmd in attempts:
        proc = sh(*cmd, check=False)
        if proc.returncode == 0:
            return {"partition": path, "fstype": fstype, "driver": cmd[2], "mountpoint": str(target), "mounted": True,
                    "size": part.get("size"), "label": part.get("label")}
        errors.append(f"{cmd[2]}: {proc.stderr.strip()}")
    try:
        target.rmdir()
    except OSError:
        pass
    return {"partition": path, "fstype": fstype, "mounted": False, "error": "; ".join(errors)}


def do_mount(args: list[str]) -> None:
    image, target = args[0], Path(args[1])
    fmt = "auto"
    uid, gid = 0, 0
    readonly = False
    rest = args[2:]
    while rest:
        flag = rest.pop(0)
        if flag == "--format":
            fmt = rest.pop(0)
        elif flag == "--uid":
            uid = int(rest.pop(0))
        elif flag == "--gid":
            gid = int(rest.pop(0))
        elif flag == "--readonly":
            readonly = True
    if not Path(image).is_file():
        fail(f"{image} is not a file")
    if (target / ".nbd").exists():
        fail(f"{target} already has an image mounted; unmount it first")
    ensure_nbd()
    device = free_nbd()
    target.mkdir(parents=True, exist_ok=True)
    cmd = ["qemu-nbd", "--connect", device, "--discard=unmap", "--cache=none" if not readonly else "--cache=unsafe"]
    if fmt and fmt != "auto":
        cmd += ["--format", fmt]
    if readonly:
        cmd.append("--read-only")
    cmd.append(image)
    proc = sh(*cmd, check=False)
    if proc.returncode != 0:
        fail("qemu-nbd could not attach the image: " + proc.stderr.strip())
    # Wait for the kernel to see a size, then the partitions.
    for _ in range(40):
        size = Path(f"/sys/block/{Path(device).name}/size").read_text().strip()
        if size not in ("", "0"):
            break
        time.sleep(0.25)
    parts = partitions(device)
    results = []
    for index, part in enumerate(parts, start=1):
        results.append(mount_one(part, target / f"p{index}", uid, gid, readonly))
    (target / ".nbd").write_text(json.dumps({"device": device, "image": image, "mounts": [r["mountpoint"] for r in results if r.get("mounted")]}))
    if not any(r.get("mounted") for r in results):
        do_umount([str(target)], quiet=True)
        fail("No filesystem on the image could be mounted: " + "; ".join(r.get("error", "") for r in results) if results else "the image has no partitions lsblk recognises")
    print(json.dumps({"ok": True, "device": device, "mounts": results}))


def do_umount(args: list[str], quiet: bool = False) -> None:
    target = Path(args[0])
    record = target / ".nbd"
    if not record.exists():
        if quiet:
            return
        fail(f"{target} has no mounted image")
    info = json.loads(record.read_text())
    errors = []
    for mountpoint in sorted(info.get("mounts", []), reverse=True):
        proc = sh("umount", mountpoint, check=False)
        if proc.returncode != 0:
            proc = sh("umount", "-l", mountpoint, check=False)
        if proc.returncode != 0:
            errors.append(proc.stderr.strip())
        else:
            try:
                Path(mountpoint).rmdir()
            except OSError:
                pass
    proc = sh("qemu-nbd", "--disconnect", info["device"], check=False)
    if proc.returncode != 0:
        errors.append(proc.stderr.strip())
    record.unlink(missing_ok=True)
    try:
        target.rmdir()
    except OSError:
        pass
    if errors and not quiet:
        fail("; ".join(errors))
    if not quiet:
        print(json.dumps({"ok": True, "device": info["device"]}))


SHARES_ROOT = "/run/replicate/shares"


def is_mountpoint(target: Path) -> bool:
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            return any(len(f := line.split()) > 1 and f[1].replace("\\040", " ") == str(target) for line in fh)
    except OSError:
        return False


def do_mount_share(args: list[str]) -> None:
    """mount -t cifs UNC TARGET -o OPTS[,credentials=FILE]; credentials come on stdin as JSON."""
    unc, target = args[0], Path(args[1])
    options = ""
    creds_stdin = False
    rest = args[2:]
    while rest:
        flag = rest.pop(0)
        if flag == "--options":
            options = rest.pop(0)
        elif flag == "--credentials-stdin":
            creds_stdin = True
    if not unc.startswith("//") or unc.count("/") < 3:
        fail(f"{unc} is not //server/share")
    if not str(target).startswith(SHARES_ROOT + "/") or ".." in target.parts:
        fail(f"Shares are mounted under {SHARES_ROOT} only")
    if any(ch in options for ch in " \n\t") or "credentials=" in options:
        fail("Unexpected mount options")
    if is_mountpoint(target):
        print(json.dumps({"ok": True, "target": str(target), "already": True}))
        return
    if not shutil.which("mount.cifs"):
        fail("mount.cifs is not installed (package cifs-utils)")
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(SHARES_ROOT, 0o755)
    creds_file = None
    try:
        if creds_stdin:
            data = json.loads(sys.stdin.read() or "{}")
            creds_file = Path(f"/run/replicate/creds-{target.name}")
            creds_file.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"username={data.get('username', '')}", f"password={data.get('password', '')}"]
            if data.get("domain"):
                lines.append(f"domain={data['domain']}")
            fd = os.open(creds_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(lines) + "\n")
            options = f"{options},credentials={creds_file}" if options else f"credentials={creds_file}"
        proc = sh("mount", "-t", "cifs", unc, str(target), "-o", options, check=False, timeout=120)
        if proc.returncode != 0:
            try:
                target.rmdir()
            except OSError:
                pass
            fail((proc.stderr or proc.stdout).strip() or f"mount exited with {proc.returncode}")
    finally:
        if creds_file is not None:
            try:
                creds_file.unlink()
            except OSError:
                pass
    print(json.dumps({"ok": True, "target": str(target)}))


def do_umount_share(args: list[str]) -> None:
    target = Path(args[0])
    if not str(target).startswith(SHARES_ROOT + "/") or ".." in target.parts:
        fail(f"Shares are mounted under {SHARES_ROOT} only")
    if not is_mountpoint(target):
        try:
            target.rmdir()
        except OSError:
            pass
        print(json.dumps({"ok": True, "target": str(target), "already": True}))
        return
    proc = sh("umount", str(target), check=False, timeout=60)
    if proc.returncode != 0:
        proc = sh("umount", "-l", str(target), check=False, timeout=60)
    if proc.returncode != 0:
        fail(proc.stderr.strip() or "umount failed")
    try:
        target.rmdir()
    except OSError:
        pass
    print(json.dumps({"ok": True, "target": str(target)}))


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        fail("must run as root (Replicate uses pkexec)")
    if len(argv) >= 3 and argv[0] == "mount":
        do_mount(argv[1:])
    elif len(argv) == 2 and argv[0] == "umount":
        do_umount(argv[1:])
    elif len(argv) >= 3 and argv[0] == "mount-share":
        do_mount_share(argv[1:])
    elif len(argv) == 2 and argv[0] == "umount-share":
        do_umount_share(argv[1:])
    else:
        fail(__doc__ or "usage error", 2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
