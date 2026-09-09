"""Global crash handling for uncaught exceptions and native crashes.

Two kinds of crash reach the user very differently:

- **Python exceptions** propagate to ``sys.excepthook`` / ``threading.excepthook``.
  :func:`install_exception_hooks` logs them, writes a timestamped traceback
  file in the app state directory and shows a dialog with the trace.

- **Native crashes** never raise anything in Python: a Qt ``qFatal`` (for
  example ``Could not initialize GLX`` while opening the Activity Editor) calls
  ``abort()``, and a segfault in Qt/Chromium kills the process outright. The
  only trace is what ``faulthandler`` prints - to stderr, which a double-clicked
  AppImage / windowed exe has nowhere to show. :class:`NativeCrashLog` points
  ``faulthandler`` at a file in the state directory instead, and on the NEXT
  start :func:`report_previous_crash` shows that file in a dialog.
  :class:`QtMessageBridge` catches the ``qFatal`` case *before* the abort so the
  Qt message is shown immediately as well, together with the Python stack that
  led to it.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from PySide6.QtCore import QMessageLogContext, QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QMessageBox

from src.app_paths import APP_NAME, app_state_dir

logger = logging.getLogger(__name__)

_NATIVE_LOG_NAME = "native-crash.log"


def _persist_trace(trace_text: str, app_name: str) -> Path | None:
    """Write traceback to disk and return the output path."""
    try:
        state_dir = app_state_dir(app_name)
        state_dir.mkdir(parents=True, exist_ok=True)
        trace_path = state_dir / f"crash-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        trace_path.write_text(trace_text, encoding="utf-8")
        return trace_path
    except OSError:
        logger.exception("Could not persist crash trace")
        return None


def _show_crash_dialog(trace_text: str, trace_path: Path | None,
                       message: str = "The application stopped with an unexpected error.",
                       *, icon: QMessageBox.Icon = QMessageBox.Icon.Critical) -> None:
    """Display an error dialog with an expandable detailed traceback."""
    if trace_path:
        message += f"\n\nTrace saved to:\n{trace_path}"

    app = QApplication.instance()
    if app is None:
        # No GUI loop available: keep a deterministic fallback.
        print(message, file=sys.stderr)
        print(trace_text, file=sys.stderr)
        return

    box = QMessageBox(icon, f"{APP_NAME} - Crash", message)
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


def install_exception_hooks(app_name: str = APP_NAME) -> None:
    """Install global exception hooks for main thread and worker threads."""

    def _sys_hook(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any) -> None:
        _handle_exception(exc_type, exc_value, exc_tb, app_name)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is None or args.exc_traceback is None:
            return
        _handle_exception(args.exc_type, args.exc_value, args.exc_traceback, app_name)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


# ---------------------------------------------------------------------------
# Native crashes (qFatal / segfault): trace file now, dialog on next start
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreviousCrash:
    """What a crashed previous run left behind in the native crash log."""

    text: str
    saved_to: Path | None


class NativeCrashLog:
    """The file ``faulthandler`` (and the Qt fatal handler) write into.

    Lifecycle per run: :meth:`take_previous` first (harvest whatever the last
    run left, move it to a timestamped ``crash-*.log``), then :meth:`open` to
    hand a fresh, line-buffered stream to ``faulthandler.enable``. Whatever is
    in the file when the process dies is, by construction, this run's crash.
    """

    def __init__(self, path: Path, app_name: str = APP_NAME) -> None:
        self._path = path
        self._app_name = app_name
        self._file: IO[str] | None = None

    @classmethod
    def default(cls, app_name: str = APP_NAME) -> "NativeCrashLog":
        return cls(app_state_dir(app_name) / _NATIVE_LOG_NAME, app_name)

    @property
    def path(self) -> Path:
        return self._path

    def take_previous(self) -> PreviousCrash | None:
        """Return (and clear) the trace a crashed previous run left, if any."""
        try:
            if not self._path.is_file():
                return None
            text = self._path.read_text(encoding="utf-8", errors="replace").strip()
            self._path.unlink()
        except OSError:
            logger.exception("Could not read the native crash log")
            return None
        if not text:
            return None
        return PreviousCrash(text, _persist_trace(text, self._app_name))

    def open(self) -> IO[str] | None:
        """Open (once) the log for appending; ``None`` if the location is unusable."""
        if self._file is None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # Line buffered: a crash must not swallow what was already written.
                self._file = open(self._path, "a", buffering=1, encoding="utf-8",
                                  errors="replace")
            except OSError:
                logger.exception("Could not open the native crash log")
                return None
        return self._file

    def write(self, text: str) -> None:
        """Append text and flush - the process may abort right after."""
        stream = self.open()
        if stream is None:
            return
        try:
            stream.write(text)
            if not text.endswith("\n"):
                stream.write("\n")
            stream.flush()
        except OSError:
            logger.exception("Could not write the native crash log")


def format_qt_fatal(message: str) -> str:
    """Trace text for a ``qFatal``: the Qt message plus the Python stack.

    The handler's own frames are dropped - what matters is which Python call
    (e.g. opening the Activity Editor) drove Qt into the fatal.
    """
    frames = [f for f in traceback.extract_stack() if f.filename != __file__]
    stack = "".join(traceback.format_list(frames))
    return (f"Qt fatal error: {message}\n\n"
            f"Python stack when Qt aborted (most recent call last):\n{stack}")


class QtMessageBridge:
    """Routes Qt's own messages (qWarning/qCritical/qFatal) into ``logging``.

    A ``qFatal`` is turned into a visible crash: the message and the Python
    stack that led to it go to the native crash log (so the next start reports
    it too), to a timestamped ``crash-*.log``, and into a dialog - all before
    the handler returns and Qt aborts the process.
    """

    _LEVELS = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
    }

    def __init__(self, crash_log: NativeCrashLog, app_name: str = APP_NAME, *,
                 expected: tuple[str, ...] = ()) -> None:
        """``expected``: message prefixes that are a known, deliberate
        consequence of how the app is configured (e.g. the "no OpenGL
        context" warnings when Qt runs without a GL integration). They are
        logged at DEBUG instead of WARNING so they do not bury real ones."""
        self._crash_log = crash_log
        self._app_name = app_name
        self._expected = expected

    def install(self) -> None:
        qInstallMessageHandler(self._handle)

    def _handle(self, mode: QtMsgType, _context: QMessageLogContext, message: str) -> None:
        if mode == QtMsgType.QtFatalMsg:
            self._fatal(message)
            return
        level = self._LEVELS.get(mode, logging.WARNING)
        if level > logging.DEBUG and message.startswith(self._expected):
            level = logging.DEBUG
        logging.getLogger("qt").log(level, "%s", message)

    def _fatal(self, message: str) -> None:
        trace_text = format_qt_fatal(message)
        logger.critical("Qt fatal error\n%s", trace_text)
        self._crash_log.write(trace_text)
        trace_path = _persist_trace(trace_text, self._app_name)
        _show_crash_dialog(
            trace_text, trace_path,
            "A component of the application failed fatally and the "
            f"application must close:\n\n{message}")


# Qt warnings that follow directly from running without a GL integration
# (QT_XCB_GL_INTEGRATION=none): every GL probe fails by design, and Qt says so
# a dozen times per Activity Editor open.
NO_GL_EXPECTED_WARNINGS: tuple[str, ...] = (
    "QXcbIntegration: Cannot create platform OpenGL context",
    "QXcbIntegration: Cannot create platform offscreen surface",
    "QRhiGles2: Failed to create",
    "Failed to create RHI for backend: OpenGL",
    "Failed to create QRhi for QBackingStoreRhiSupport",
)


def install_qt_message_handler(crash_log: NativeCrashLog, app_name: str = APP_NAME, *,
                               expected: tuple[str, ...] = ()) -> QtMessageBridge:
    """Install the Qt message bridge and return it."""
    bridge = QtMessageBridge(crash_log, app_name, expected=expected)
    bridge.install()
    return bridge


def report_previous_crash(previous: PreviousCrash | None) -> None:
    """Tell the user the last run died with a native crash (needs a QApplication)."""
    if previous is None:
        return
    logger.error("Previous run crashed\n%s", previous.text)
    _show_crash_dialog(
        previous.text, previous.saved_to,
        f"{APP_NAME} crashed the last time it ran (a native crash that no "
        "error dialog could report at the time).",
        icon=QMessageBox.Icon.Warning)
