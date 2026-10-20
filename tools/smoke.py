"""Start Replicate with a throwaway config, visit every page, save screenshots, quit.

Used by CI (under Xvfb) and handy locally:  python3 tools/smoke.py [--dark]
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
home = Path(tempfile.mkdtemp(prefix="replicate-smoke-"))
os.environ["XDG_CONFIG_HOME"] = str(home / "config")
os.environ["XDG_DATA_HOME"] = str(home / "data")
os.environ["XDG_CACHE_HOME"] = str(home / "cache")
os.environ["REPLICATE_NO_KEYRING"] = "1"
out = ROOT / "tools" / "screenshots"
out.mkdir(exist_ok=True)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402

from replicate.ui.app import ReplicateApp  # noqa: E402

PAGES = ["overview", "servers", "images", "activity", "settings"]
dark = "--dark" in sys.argv
narrow = "--narrow" in sys.argv
app = ReplicateApp()
failures: list[str] = []


def snap(name: str) -> None:
    win = app.window
    w, h = win.get_width(), win.get_height()
    paintable = Gtk.WidgetPaintable.new(win)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, w, h)
    node = snapshot.to_node()
    texture = win.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(str(out / f"{name}{'-dark' if dark else ''}{'-narrow' if narrow else ''}.png"))
    print("saved", name)


def step(i: int = 0) -> bool:
    if i == 0 and narrow:
        app.window.set_default_size(640, 760)
    if i >= len(PAGES):
        app.quit()
        return False
    try:
        app.window.show_page(PAGES[i])
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{PAGES[i]}: {exc}")
    GLib.timeout_add(800, lambda: (snap(PAGES[i]), step(i + 1)) and False)
    return False


def on_activate(_app) -> None:
    if dark:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    GLib.timeout_add(1200, step)


app.connect("activate", on_activate)
code = app.run([])
if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
sys.exit(code)
