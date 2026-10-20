"""Copy-on-write clones of an image.

A clone is a qcow2 file whose backing file is the VHD/VHDX. Writes go to the
clone; the image is never touched; many clones of one image cost next to
nothing until the guest writes. This is Replicate's own take on what SQL
Clone does with differencing disks, done with QEMU's image chain instead.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from replicate.util import CommandError, run, which
from replicate.vhd.images import ImageError, ImageInfo, inspect


@dataclass
class CloneInfo:
    path: str
    image_path: str
    size_on_disk: int
    created: float
    label: str


def _slug(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return text or "clone"


def create_clone(image: ImageInfo, clones_dir: str | Path, label: str = "") -> Path:
    if image.error:
        raise ImageError(f"{image.name}: {image.error}")
    if image.is_differencing and image.format != "qcow2":
        # QEMU cannot follow a VHDX parent chain. Tell the user instead of failing later.
        raise ImageError(
            f"{image.name} is a differencing disk (its parent is {image.backing_file}). "
            "Merge it in Windows first (Merge-VHD) or pick its parent image."
        )
    qemu_img = which("qemu-img")
    if not qemu_img:
        raise ImageError("qemu-img is not installed")
    clones_dir = Path(clones_dir).expanduser()
    clones_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = _slug(label) if label else _slug(Path(image.path).stem)
    target = clones_dir / f"{stem}-{stamp}.qcow2"
    counter = 1
    while target.exists():
        counter += 1
        target = clones_dir / f"{stem}-{stamp}-{counter}.qcow2"
    try:
        run([qemu_img, "create", "-q", "-f", "qcow2", "-F", image.format, "-b", str(Path(image.path).resolve()), str(target)])
    except CommandError as exc:
        raise ImageError(exc.stderr.strip() or str(exc)) from exc
    return target


def full_copy(image: ImageInfo, clones_dir: str | Path, label: str = "") -> Path:
    """An independent qcow2 copy (slow; for images on removable or network storage)."""
    qemu_img = which("qemu-img")
    if not qemu_img:
        raise ImageError("qemu-img is not installed")
    clones_dir = Path(clones_dir).expanduser()
    clones_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = clones_dir / f"{_slug(label or Path(image.path).stem)}-copy-{stamp}.qcow2"
    try:
        run([qemu_img, "convert", "-q", "-f", image.format, "-O", "qcow2", image.path, str(target)], timeout=None)
    except CommandError as exc:
        raise ImageError(exc.stderr.strip() or str(exc)) from exc
    return target


def list_clones(clones_dir: str | Path) -> list[CloneInfo]:
    clones_dir = Path(clones_dir).expanduser()
    if not clones_dir.is_dir():
        return []
    result = []
    for path in sorted(clones_dir.glob("*.qcow2"), key=lambda p: p.stat().st_mtime, reverse=True):
        info = inspect(path)
        result.append(
            CloneInfo(
                path=str(path),
                image_path=info.backing_file or "",
                size_on_disk=info.actual_size,
                created=path.stat().st_mtime,
                label=path.stem,
            )
        )
    return result


def delete_clone(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)
