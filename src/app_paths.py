"""Filesystem locations the app writes to.

Single source of truth so logging, crash traces and the std-stream fallback all
land in the same place — and never inside a read-only frozen bundle
(PyInstaller ``_MEIPASS`` / AppImage mount).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "SoftEdIBO"


def app_state_dir(app_name: str = APP_NAME) -> Path:
    """Return a writable per-user directory for logs/state (not created)."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name

    # Linux / Unix (XDG)
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / app_name
    return Path.home() / ".local" / "state" / app_name
