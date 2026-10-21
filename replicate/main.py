"""Entry point."""

from __future__ import annotations

import logging
import os
import sys

from replicate import APP_NAME, __version__, paths


def _setup_logging() -> None:
    paths.ensure_dirs()
    level = logging.DEBUG if os.environ.get("REPLICATE_DEBUG") else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(paths.LOG_DIR / "replicate.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def scan_command(args: list[str]) -> int:
    """``replicate --scan DIR [DIR...] [--all]``: say what the image scan makes of every file.

    Prints the images found and, with --all, the decision for each file, so a
    folder whose images do not show up can be looked at from a terminal.
    """
    from replicate.vhd.images import ScanReport, scan

    verbose = "--all" in args
    dirs = [a for a in args if not a.startswith("--")]
    if not dirs:
        print("usage: replicate --scan DIR [DIR...] [--all]", file=sys.stderr)
        return 2
    report = ScanReport()
    images = scan(dirs, report=report, verbose=True)
    for info in images:
        print(f"IMAGE  {info.format_label:6} {info.virtual_size / 2**30:8.1f} GiB  {info.path}" + (f"  ({info.error})" if info.error else ""))
    if verbose:
        for path, why in report.decisions:
            if not why.endswith("by signature") and not why.endswith("extension"):
                print(f"skip   {why:40} {path}")
    print(report.describe(len(images)))
    if report.unreadable:
        print("Some files could not be read as this user; check the share's mount options (uid=, file_mode=) or permissions.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv:
        print(f"{APP_NAME} {__version__}")
        return 0
    if "--scan" in argv:
        return scan_command(argv[argv.index("--scan") + 1 :])
    _setup_logging()
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
    except (ImportError, ValueError) as exc:
        print(f"Replicate needs GTK 4 and libadwaita with GObject introspection: {exc}", file=sys.stderr)
        return 1
    from replicate.ui.app import ReplicateApp

    return ReplicateApp().run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
