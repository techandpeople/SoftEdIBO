"""Global crash handling for uncaught exceptions.

Installs process-wide hooks that:
- log uncaught exceptions,
- write a timestamped traceback file in the app state directory,
- show a GUI dialog with a short message and expandable trace details.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication, QMessageBox

logger = logging.getLogger(__name__)


def _app_state_dir(app_name: str = "SoftEdIBO") -> Path:
    """Return a writable per-user directory for logs/state."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name

    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / app_name
    return Path.home() / ".local" / "state" / app_name


def _persist_trace(trace_text: str, app_name: str) -> Path | None:
    """Write traceback to disk and return the output path."""
    try:
        state_dir = _app_state_dir(app_name)
        state_dir.mkdir(parents=True, exist_ok=True)
        trace_path = state_dir / f"crash-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        trace_path.write_text(trace_text, encoding="utf-8")
        return trace_path
    except OSError:
        logger.exception("Could not persist crash trace")
        return None


def _show_crash_dialog(trace_text: str, trace_path: Path | None) -> None:
    """Display a fatal error dialog with optional detailed traceback."""
    message = "A aplicacao terminou com um erro inesperado."
    if trace_path:
        message += f"\n\nTrace guardado em:\n{trace_path}"

    app = QApplication.instance()
    if app is None:
        # No GUI loop available: keep a deterministic fallback.
        print(message, file=sys.stderr)
        print(trace_text, file=sys.stderr)
        return

    box = QMessageBox(QMessageBox.Icon.Critical, "SoftEdIBO - Crash", message)
    box.setDetailedText(trace_text)
    box.exec()


def _handle_exception(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any, app_name: str) -> None:
    """Common handler used by sys.excepthook and threading.excepthook."""
    if issubclass(exc_type, KeyboardInterrupt):
        return

    trace_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.critical("Uncaught exception\n%s", trace_text)
    trace_path = _persist_trace(trace_text, app_name)
    _show_crash_dialog(trace_text, trace_path)


def install_exception_hooks(app_name: str = "SoftEdIBO") -> None:
    """Install global exception hooks for main thread and worker threads."""

    def _sys_hook(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any) -> None:
        _handle_exception(exc_type, exc_value, exc_tb, app_name)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is None or args.exc_traceback is None:
            return
        _handle_exception(args.exc_type, args.exc_value, args.exc_traceback, app_name)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
