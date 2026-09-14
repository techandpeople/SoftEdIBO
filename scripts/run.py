"""Main entry point for the SoftEdIBO application."""

import faulthandler
import io
import logging
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.wayland.textinput=false")

# QtWebEngine (Activity Editor's block canvas) runs a Chromium process that
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

# No OpenGL at all for the Qt side of the Activity Editor. QWebEngineView is a
# QQuickWidget underneath; by default its scene graph and the window's backing
# store composite through OpenGL, which on xcb means GLX - and a GLX that
# cannot produce a context is FATAL inside Qt: qFatal("Could not initialize
# GLX") -> abort(), with no Python exception and therefore no error dialog.
# That is exactly what the AppImage does on WSL2 (WSLg): its bundled
# libstdc++/libgbm shadow the host Mesa driver stack, GLX finds no visual and
# the editor takes the whole app down; where GLX does come up, the GL path
# still segfaults on that machine. A block editor has no use for a GPU:
#  - QT_QUICK_BACKEND=software makes QQuickWidget paint through the software
#    scene graph (plain QImage into the raster backing store, no RHI). This is
#    the part that actually shows pixels - with only the GL integration off,
#    Qt has no RHI to composite with and the editor window stays black.
#  - QT_XCB_GL_INTEGRATION=none stops Qt from even probing GLX, so the fatal
#    path above cannot be reached. Chromium already runs with --disable-gpu.
# Override both by exporting them (e.g. QT_XCB_GL_INTEGRATION=xcb_glx and
# QT_QUICK_BACKEND=rhi) to get GL back.
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    os.environ.setdefault("QT_XCB_GL_INTEGRATION", "none")

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# A windowed frozen build has no console: sys.stdout/sys.stderr are None and any
# write to them is fatal. Must run before faulthandler/logging touch stderr.
from src.std_streams import ensure_std_streams

_stderr = ensure_std_streams()

from src.crash_handler import (
    NO_GL_EXPECTED_WARNINGS, NativeCrashLog, install_exception_hooks,
    install_qt_message_handler, report_previous_crash)

# Dump every thread's Python stack on a native crash (SIGSEGV, a Qt qFatal's
# abort, ...). The exception hooks only catch Python exceptions; a crash in
# Qt/Chromium kills the process without one, so this is the only trace we get.
# A frozen build has no console anyone is looking at, so the dump goes to the
# native crash log in the state directory and is shown in a dialog on the next
# start (report_previous_crash below); in development it stays on stderr.
# Dormant until a fatal signal fires, so there is no runtime cost.
_crash_log = NativeCrashLog.default()
_previous_crash = _crash_log.take_previous()
_frozen = bool(getattr(sys, "frozen", False))
try:
    _dump_target = (_crash_log.open() if _frozen else None) or _stderr
    if _dump_target is not None:
        faulthandler.enable(_dump_target)
except (RuntimeError, ValueError, AttributeError, io.UnsupportedOperation):
    pass    # no dumpable stream - Python-level crash handling still works

from src.log import setup as setup_logging

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
    # Both required BEFORE the QApplication exists for the Activity Editor's
    # QWebEngineView (Tools => Activity Editor...):
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

    # Before any window is created (the setup wizard may open first), so every
    # top-level window inherits it.
    from src.gui.app_icon import apply_app_icon
    apply_app_icon(app)

    install_exception_hooks("SoftEdIBO")
    # Qt-side fatals (qFatal -> abort) get a dialog + trace file before the
    # abort; Qt warnings/criticals land in the normal log.
    _no_gl = os.environ.get("QT_XCB_GL_INTEGRATION") == "none"
    install_qt_message_handler(
        _crash_log, expected=NO_GL_EXPECTED_WARNINGS if _no_gl else ())

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

    # A native crash last time could not show anything then - tell the user now.
    report_previous_crash(_previous_crash)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
