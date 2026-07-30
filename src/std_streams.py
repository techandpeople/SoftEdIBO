"""Guarantee that ``sys.stdout`` / ``sys.stderr`` are usable.

A windowed frozen build (PyInstaller ``console=False``) starts with both set to
``None`` — there is no terminal to write to. Anything that touches them then
blows up: ``faulthandler.enable()`` raises ``RuntimeError: sys.stderr is None``
before the app can even show a dialog, and ``logging``'s console handler and the
loop watchdog would fail the same way later.

Call :func:`ensure_std_streams` first thing at startup. Missing streams are
pointed at a file in the app state dir, so those writes land somewhere readable
instead of killing the process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO

from src.app_paths import app_state_dir

_FALLBACK_NAME = "console.log"
_MAX_BYTES = 1 * 1024 * 1024  # truncate on startup past this, it is a scratch log


class StdStreamGuard:
    """Substitutes an append-mode file for whichever std stream is missing."""

    def __init__(self, fallback_path: Path) -> None:
        self._fallback_path = fallback_path
        self._file: IO[str] | None = None

    def ensure(self) -> IO[str] | None:
        """Replace any ``None`` std stream and return the resulting stderr."""
        if sys.stdout is None:
            sys.stdout = self._fallback()
        if sys.stderr is None:
            sys.stderr = self._fallback()
        return sys.stderr

    def _fallback(self) -> IO[str] | None:
        """Open (once) the shared substitute stream; ``os.devnull`` as a last resort."""
        if self._file is None:
            self._file = self._open(self._fallback_path) or self._open(Path(os.devnull))
        return self._file

    @staticmethod
    def _open(path: Path) -> IO[str] | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size > _MAX_BYTES:
                path.unlink()
            # Line buffered: a crash must not swallow what was already written.
            return open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        except OSError:
            return None


def ensure_std_streams() -> IO[str] | None:
    """Install the fallback for any missing std stream; returns ``sys.stderr``."""
    return StdStreamGuard(app_state_dir() / _FALLBACK_NAME).ensure()
