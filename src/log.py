"""Centralized logging configuration for SoftEdIBO.

Call ``setup()`` once at startup (before any other imports that use logging).
Logs go to:
  - **console** (stderr): WARNING and above (coloured if terminal supports it).
  - **file** (``data/softedibo.log``): DEBUG and above, rotated at 2 MB × 3 backups.
"""

import logging
import sys
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "data"
_LOG_FILE = _LOG_DIR / "softedibo.log"
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_BACKUP_COUNT = 3
_FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _app_state_dir(app_name: str = "SoftEdIBO") -> Path:
    """
    Return a writable per-user directory for logs/state.
    Never points inside AppImage/_MEIPASS.
    """
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


def setup(*, console_level: int = logging.WARNING, file_level: int = logging.DEBUG) -> None:
    """Configure the root logger with console + rotating file handlers."""
    root = logging.getLogger()

    # Avoid duplicate handlers if called more than once
    if root.handlers:
        return

    root.setLevel(logging.DEBUG)

    # Console handler — terse, warnings+ only
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
    root.addHandler(console)

    # File handler — verbose, rotating
    log_dir = _app_state_dir("SoftEdIBO")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "softedibo.log"
    file_handler = RotatingFileHandler(
        log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
    root.addHandler(file_handler)
