"""Centralized logging configuration for SoftEdIBO.

Call ``setup()`` once at startup (before any other imports that use logging).
Logs go to:
  - **console** (stderr): WARNING and above - skipped when there is no console.
  - **file** (``<app state dir>/softedibo.log``): DEBUG and above, rotated at
    2 MB x 3 backups.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from src.app_paths import app_state_dir

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

    # Console handler - terse, warnings+ only. Skipped when there is no console
    # at all (windowed frozen build): StreamHandler(None) would raise on emit.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
        root.addHandler(console)

    # File handler - verbose, rotating
    log_dir = app_state_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "softedibo.log"
    file_handler = RotatingFileHandler(
        log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
    root.addHandler(file_handler)
