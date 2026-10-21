"""Find and inspect disk images with ``qemu-img``.

QEMU reads VHD (``vpc``) and VHDX natively, so an image made on Windows
(Hyper-V, Disk Management, ``New-VHD``) needs no
conversion: it is attached to the VM as is, or through a copy-on-write clone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from replicate.util import CommandError, run, which

IMAGE_SUFFIXES = {".vhd", ".vhdx", ".avhd", ".avhdx", ".qcow2", ".img", ".raw", ".vdi", ".vmdk"}
# Files with these extensions are never disk images; everything else without a
# known image extension gets its first and last bytes looked at.
NOT_IMAGE_SUFFIXES = {
    ".txt", ".log", ".md", ".json", ".xml", ".yml", ".yaml", ".ini", ".cfg", ".conf", ".csv", ".html", ".htm", ".pdf",
    ".bak", ".trn", ".dif", ".mdf", ".ndf", ".ldf", ".zip", ".7z", ".gz", ".xz", ".bz2", ".tar", ".rar", ".iso",
    ".exe", ".dll", ".msi", ".ps1", ".bat", ".cmd", ".sh", ".py", ".sql", ".doc", ".docx", ".xls", ".xlsx", ".png",
    ".jpg", ".jpeg", ".gif", ".svg", ".mp4", ".mkv", ".avi", ".wav", ".mp3", ".lnk", ".url", ".db", ".sqlite",
    ".pyc", ".o", ".so", ".a", ".jar", ".class", ".bin", ".dat", ".tmp", ".part", ".lock", ".nbd",
}
SNIFF_MIN_SIZE = 512  # a fresh dynamic VHD is only a few KB, so anything with a sector gets a look


def sniff_format(path: str | Path, size: int | None = None) -> str | None:
    """The image format a file's bytes say it is, or None.

    Two small reads per file (its first KiB and last 512 bytes), so looking
    at every unknown file on a share costs little more than listing it.

    VHDX starts with ``vhdxfile``; a VHD ends with a ``conectix`` footer
    (dynamic ones carry a copy at the start too); qcow2 starts with
    ``QFI\\xfb``; VDI with its banner; VMDK with ``KDMV`` or a text
    descriptor; a raw disk with a GPT header at 512 or an MBR signature.
    """
    try:
        if size is None:
            size = os.stat(path).st_size
        if size < SNIFF_MIN_SIZE:
            return None
        with open(path, "rb") as fh:
            head = fh.read(1024)
            fh.seek(-min(1024, size), os.SEEK_END)
            tail = fh.read(1024)
    except OSError:
        return None
    if head.startswith(b"vhdxfile"):
        return "vhdx"
    # The VHD footer is the last 512 bytes, or 511 on disks from old Virtual PC versions.
    if head.startswith(b"conectix") or b"conectix" in tail[-520:]:
        return "vpc"
    if head.startswith(b"QFI\xfb"):
        return "qcow2"
    if head.startswith(b"<<< ") and b"VirtualBox Disk Image" in head[:64]:
        return "vdi"
    if head.startswith(b"KDMV") or head.startswith(b"# Disk DescriptorFile"):
        return "vmdk"
    if head[512:520] == b"EFI PART":
        return "raw"
    if head[510:512] == b"\x55\xaa" and any(head[446 + 16 * i + 4] not in (0, 0xEE) or head[446 + 16 * i + 4] == 0xEE for i in range(4) if head[446 + 16 * i + 4]):
        return "raw"
    return None
FORMAT_LABELS = {"vpc": "VHD", "vhdx": "VHDX", "qcow2": "QCOW2", "raw": "Raw", "vdi": "VDI", "vmdk": "VMDK"}


class ImageError(RuntimeError):
    pass


@dataclass
class ImageInfo:
    path: str
    format: str = "unknown"
    virtual_size: int = 0
    actual_size: int = 0
    backing_file: str | None = None
    backing_format: str | None = None
    modified: float = 0.0
    error: str = ""
    extra: dict = field(default_factory=dict)
    root: str = ""  # the image folder the scan started from, when found by a scan

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def folder(self) -> str:
        """The image's folder relative to the scanned root (empty at the root itself)."""
        parent = Path(self.path).parent
        if self.root:
            try:
                rel = parent.relative_to(self.root)
                return "" if str(rel) == "." else str(rel)
            except ValueError:
                pass
        return str(parent)

    @property
    def format_label(self) -> str:
        return FORMAT_LABELS.get(self.format, self.format.upper())

    @property
    def is_differencing(self) -> bool:
        return bool(self.backing_file)

    @property
    def is_clone(self) -> bool:
        return self.format == "qcow2" and bool(self.backing_file)


def inspect(path: str | Path) -> ImageInfo:
    path = str(path)
    info = ImageInfo(path=path)
    try:
        info.modified = os.stat(path).st_mtime
    except OSError as exc:
        info.error = str(exc)
        return info
    qemu_img = which("qemu-img")
    if not qemu_img:
        info.error = "qemu-img is not installed"
        return info
    try:
        out = run([qemu_img, "info", "-U", "--output=json", path], timeout=60)
    except CommandError as exc:
        info.error = exc.stderr.strip() or str(exc)
        return info
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        info.error = f"unreadable qemu-img output: {exc}"
        return info
    info.format = data.get("format", "unknown")
    info.virtual_size = int(data.get("virtual-size") or 0)
    info.actual_size = int(data.get("actual-size") or 0)
    info.backing_file = data.get("backing-filename") or data.get("full-backing-filename")
    info.backing_format = data.get("backing-filename-format")
    info.extra = {k: v for k, v in data.items() if k in ("cluster-size", "dirty-flag", "format-specific")}
    return info


SKIP_DIR_NAMES = {"$RECYCLE.BIN", "System Volume Information", "lost+found", "node_modules", ".git"}


@dataclass
class ScanReport:
    """What a scan went through, for the page's status line and for --scan."""

    folders: int = 0
    files: int = 0
    looked_inside: int = 0
    unreadable: int = 0
    missing_roots: list[str] = field(default_factory=list)
    seconds: float = 0.0
    decisions: list[tuple[str, str]] = field(default_factory=list)  # (path, why) when verbose

    def describe(self, found: int) -> str:
        parts = [f"{found} image{'s' if found != 1 else ''}", f"{self.folders:,} folder{'s' if self.folders != 1 else ''}", f"{self.files:,} files"]
        if self.unreadable:
            parts.append(f"{self.unreadable} could not be read")
        if self.missing_roots:
            parts.append("missing: " + ", ".join(self.missing_roots))
        return " · ".join(parts) + f" · {self.seconds:.1f} s"


def scan(directories: list[str], *, include_clones: bool = False, max_depth: int = 0, max_dirs: int = 2_000_000,
         report: ScanReport | None = None, verbose: bool = False, progress=None) -> list[ImageInfo]:
    """Images anywhere under the given directories, newest first.

    Files with an image extension are taken as such; every other file whose
    name does not rule it out is opened and its signature checked. ``max_depth``
    0 means no limit. Hidden folders, recycle bins and symlinked directories
    are skipped. ``progress(folders, found)`` is called now and then.
    """
    import time

    started = time.monotonic()
    report = report if report is not None else ScanReport()
    seen: set[str] = set()
    found: list[ImageInfo] = []

    def note(path: str, why: str) -> None:
        if verbose:
            report.decisions.append((path, why))

    for directory in directories:
        root = Path(directory).expanduser()
        if not root.is_dir():
            report.missing_roots.append(str(root))
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            report.folders += 1
            if report.folders > max_dirs:
                dirnames[:] = []
                break
            if progress and report.folders % 200 == 0:
                progress(report.folders, len(found))
            depth = len(Path(dirpath).parts) - base_depth
            if max_depth and depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIR_NAMES)
            for filename in filenames:
                report.files += 1
                suffix = Path(filename).suffix.lower()
                full = str(Path(dirpath) / filename)
                detected = None
                if suffix not in IMAGE_SUFFIXES:
                    if filename.startswith("."):
                        note(full, "hidden file")
                        continue
                    if suffix in NOT_IMAGE_SUFFIXES:
                        note(full, f"{suffix} is never an image")
                        continue
                    try:
                        size = os.stat(full).st_size
                    except OSError as exc:
                        report.unreadable += 1
                        note(full, f"cannot stat: {exc.strerror}")
                        continue
                    if size < SNIFF_MIN_SIZE:
                        note(full, f"too small ({size} bytes)")
                        continue
                    report.looked_inside += 1
                    try:
                        with open(full, "rb"):
                            pass
                    except OSError as exc:
                        report.unreadable += 1
                        note(full, f"cannot read: {exc.strerror}")
                        continue
                    detected = sniff_format(full, size)
                    if detected is None:
                        note(full, "no disk image signature")
                        continue
                    note(full, f"{detected} by signature")
                else:
                    note(full, f"{suffix} extension")
                real = os.path.realpath(full)
                if real in seen:
                    continue
                seen.add(real)
                info = inspect(full)
                info.root = str(root)
                if detected and info.format in ("raw", "unknown") and detected != "raw":
                    info.format = detected
                if info.is_clone and not include_clones:
                    note(full, "a clone (hidden from the image list)")
                    continue
                found.append(info)
    found.sort(key=lambda i: i.modified, reverse=True)
    report.seconds = time.monotonic() - started
    if progress:
        progress(report.folders, len(found))
    return found
