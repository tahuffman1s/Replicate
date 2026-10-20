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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv:
        print(f"{APP_NAME} {__version__}")
        return 0
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
