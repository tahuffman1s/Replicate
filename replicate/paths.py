"""Where Replicate keeps things on the host (XDG directories)."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else default


HOME = Path.home()
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", HOME / ".config") / "replicate"
DATA_DIR = _xdg("XDG_DATA_HOME", HOME / ".local" / "share") / "replicate"
CACHE_DIR = _xdg("XDG_CACHE_HOME", HOME / ".cache") / "replicate"

CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
SECRETS_FILE = CONFIG_DIR / "secrets.json"
CLONES_DIR = DATA_DIR / "clones"
LOG_DIR = DATA_DIR / "logs"

# Resources shipped inside the package.
PACKAGE_DIR = Path(__file__).resolve().parent
GUEST_DIR = PACKAGE_DIR / "guest"
STYLE_CSS = PACKAGE_DIR / "ui" / "style.css"
ICONS_DIR = PACKAGE_DIR / "ui" / "icons"
# The hicolor app icon when running from a source checkout (installed builds ship it in share/icons).
SOURCE_ICONS_DIR = PACKAGE_DIR.parent / "data" / "icons"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, CACHE_DIR, CLONES_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
