"""Make small ISO images (answers CD, setup CD) with whatever tool is around."""

from __future__ import annotations

from pathlib import Path

from replicate.util import run, which


class IsoToolMissing(RuntimeError):
    pass


def make_iso(source_dir: Path, output: Path, label: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tool = which("xorriso")
    if tool:
        run([tool, "-as", "mkisofs", "-quiet", "-J", "-R", "-V", label, "-o", str(output), str(source_dir)])
        return
    tool = which("genisoimage", "mkisofs")
    if tool:
        run([tool, "-quiet", "-J", "-R", "-V", label, "-o", str(output), str(source_dir)])
        return
    raise IsoToolMissing("No ISO tool found; install xorriso (or genisoimage)")
