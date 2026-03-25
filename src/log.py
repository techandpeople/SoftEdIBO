"""Centralized logging configuration for SoftEdIBO.

Call ``setup()`` once at startup (before any other imports that use logging).
Logs go to:
  - **console** (stderr): WARNING and above (coloured if terminal supports it).
  - **file** (``data/softedibo.log``): DEBUG and above, rotated at 2 MB × 3 backups.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "data"
_LOG_FILE = _LOG_DIR / "softedibo.log"
_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_BACKUP_COUNT = 3
_FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


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
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
    root.addHandler(file_handler)
