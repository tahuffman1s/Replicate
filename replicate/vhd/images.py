"""Find and inspect disk images with ``qemu-img``.

QEMU reads VHD (``vpc``) and VHDX natively, so an image made on Windows
(Hyper-V, Disk Management, a SQL Clone image, ``New-VHD``) needs no
conversion: it is attached to the VM as is, or through a copy-on-write clone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from replicate.util import CommandError, run, which

IMAGE_SUFFIXES = {".vhd", ".vhdx", ".avhd", ".avhdx", ".qcow2", ".img", ".raw", ".vdi", ".vmdk"}
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

    @property
    def name(self) -> str:
        return Path(self.path).name

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


def scan(directories: list[str], *, include_clones: bool = False, max_depth: int = 3) -> list[ImageInfo]:
    """Images under the given directories (a few levels deep), newest first."""
    seen: set[str] = set()
    found: list[ImageInfo] = []
    for directory in directories:
        root = Path(directory).expanduser()
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if len(Path(dirpath).parts) - base_depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for filename in filenames:
                if Path(filename).suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                full = str(Path(dirpath) / filename)
                real = os.path.realpath(full)
                if real in seen:
                    continue
                seen.add(real)
                info = inspect(full)
                if info.is_clone and not include_clones:
                    continue
                found.append(info)
    found.sort(key=lambda i: i.modified, reverse=True)
    return found
