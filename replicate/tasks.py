"""Run blocking work off the UI thread and report back on it.

Everything that talks to libvirt, the guest agent or SQL Server blocks, so
the UI hands it to :func:`run_in_thread` and gets the result (or exception)
back through GLib's main loop. Without GLib (tests, CLI) callbacks run on the
worker thread.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

try:
    from gi.repository import GLib  # type: ignore
except Exception:  # noqa: BLE001
    GLib = None  # type: ignore


def on_main(func: Callable[..., Any], *args: Any) -> None:
    """Call ``func`` on the GLib main loop (or right away without one)."""
    if GLib is None:
        func(*args)
        return

    def _once() -> bool:
        func(*args)
        return False

    GLib.idle_add(_once)


class Progress:
    """Progress sink handed to long operations; thread-safe."""

    def __init__(self, on_update: Callable[[str, float], None] | None = None):
        self._on_update = on_update
        self.message = ""
        self.fraction = -1.0  # <0: indeterminate
        self.log: list[str] = []

    def update(self, message: str, fraction: float | None = None) -> None:
        self.message = message
        if fraction is not None:
            self.fraction = fraction
        self.log.append(message)
        log.info("%s", message)
        if self._on_update:
            on_main(self._on_update, message, self.fraction)


def run_in_thread(
    work: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    name: str | None = None,
    **kwargs: Any,
) -> threading.Thread:
    """Run ``work(*args, **kwargs)`` on a thread; every other keyword goes to ``work``."""

    def _runner() -> None:
        try:
            result = work(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            log.debug("background task %s failed:\n%s", name or work.__name__, traceback.format_exc())
            if on_error:
                on_main(on_error, exc)
            return
        if on_done:
            on_main(on_done, result)

    thread = threading.Thread(target=_runner, name=name or f"replicate-{work.__name__}", daemon=True)
    thread.start()
    return thread
