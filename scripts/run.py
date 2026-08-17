"""Main entry point for the SoftEdIBO application."""

import faulthandler
import io
import logging
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.wayland.textinput=false")

# QtWebEngine (Behaviour Editor's block canvas) runs a Chromium process that
# hard-crashes on some Linux GPU drivers / Wayland-via-xcb setups. Force the
# web view to render in software - plenty for a block editor - which avoids the
# driver-dependent GPU crash. Override by exporting QTWEBENGINE_CHROMIUM_FLAGS.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

# Run via XWayland (xcb) instead of native Wayland. On GNOME/Wayland, creating a
# new top-level window (a config dialog) occasionally costs ~120 ms in native Qt
# surface setup - compositor roundtrips whose latency varies - which makes GNOME
# flash the "busy" spinner cursor (the app never actually blocks; verified with
# the loop watchdog). Under XWayland that cost disappears. Override by exporting
# QT_QPA_PLATFORM=wayland to go back to native Wayland.
#
# Linux only: xcb does not exist on Windows (whose plugins are direct2d/windows)
# or macOS (cocoa), and naming a missing platform plugin is fatal - Qt aborts
# with "no Qt platform plugin could be initialized" before any window appears.
# Leaving QT_QPA_PLATFORM unset lets Qt pick the right one per platform.
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# A windowed frozen build has no console: sys.stdout/sys.stderr are None and any
# write to them is fatal. Must run before faulthandler/logging touch stderr.
from src.std_streams import ensure_std_streams

_stderr = ensure_std_streams()

# Dump every thread's Python stack to stderr on a native crash (SIGSEGV etc.).
# Our crash_handler only catches Python exceptions; a segfault in Qt/Chromium
# kills the process without one, so this is the only trace we get for those.
# It is dormant until a fatal signal fires, so there is no runtime cost.
try:
    if _stderr is not None:
        faulthandler.enable(_stderr)
except (RuntimeError, ValueError, AttributeError, io.UnsupportedOperation):
    pass    # no dumpable stream - Python-level crash handling still works

from src.log import setup as setup_logging
from src.crash_handler import install_exception_hooks

_debug = "--debug" in sys.argv
if _debug:
    sys.argv.remove("--debug")
setup_logging(console_level=logging.DEBUG if _debug else logging.WARNING)

from PySide6.QtWidgets import QApplication, QMessageBox

from src.gui.setup_wizard import SetupWizard, mark_setup_done, needs_setup


def _fatal(msg: str) -> None:
    """Show a graphical error dialog and exit - works even without a console."""
    QMessageBox.critical(None, "SoftEdIBO - Startup Error", msg)
    sys.exit(1)


def main():
    # Both required BEFORE the QApplication exists for the Behaviour Editor's
    # QWebEngineView (Tools => Behaviour Editor...):
    #  1. shared OpenGL contexts (Qt requirement for the WebEngine widget);
    #  2. importing QtWebEngine itself - on PySide6/Linux, importing it AFTER
    #     the QApplication is created makes the web view segfault on open. The
    #     import only loads the libraries; the Chromium subprocess still starts
    #     lazily when the editor is actually opened, so sessions are unaffected.
    from PySide6.QtCore import Qt
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    try:
        from PySide6 import QtWebEngineWidgets  # noqa: F401
    except ImportError:
        pass   # editor will report a clear error if WebEngine is unavailable

    app = QApplication(sys.argv)
    install_exception_hooks("SoftEdIBO")

    # Diagnostic only - off unless SOFTEDIBO_WATCHDOG is set. Dumps the GUI
    # thread's stack to stderr whenever the event loop stalls (busy cursor).
    from src.gui.loop_watchdog import install_loop_watchdog
    install_loop_watchdog(app)

    if needs_setup():
        try:
            # Cancelling/skipping the wizard is a valid choice - the hardware
            # may have been flashed on a previous run. Start the app normally
            # instead of quitting, and mark setup done so it does not nag on
            # every launch (the wizard stays reachable from the Tools menu).
            SetupWizard().exec()
            mark_setup_done()
        except Exception:
            _fatal(f"Error in setup wizard:\n\n{traceback.format_exc()}")

    try:
        from src.gui.main_window import MainWindow
        window = MainWindow()
        window.show()
    except Exception:
        _fatal(f"Error opening main window:\n\n{traceback.format_exc()}")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
