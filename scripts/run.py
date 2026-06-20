"""Main entry point for the SoftEdIBO application."""

import logging
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.wayland.textinput=false")

# Run via XWayland (xcb) instead of native Wayland. On GNOME/Wayland, creating a
# new top-level window (a config dialog) occasionally costs ~120 ms in native Qt
# surface setup — compositor roundtrips whose latency varies — which makes GNOME
# flash the "busy" spinner cursor (the app never actually blocks; verified with
# the loop watchdog). Under XWayland that cost disappears. Override by exporting
# QT_QPA_PLATFORM=wayland to go back to native Wayland.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.log import setup as setup_logging
from src.crash_handler import install_exception_hooks

_debug = "--debug" in sys.argv
if _debug:
    sys.argv.remove("--debug")
setup_logging(console_level=logging.DEBUG if _debug else logging.WARNING)

from PySide6.QtWidgets import QApplication, QMessageBox

from src.gui.setup_wizard import SetupWizard, needs_setup


def _fatal(msg: str) -> None:
    """Show a graphical error dialog and exit — works even without a console."""
    QMessageBox.critical(None, "SoftEdIBO — Startup Error", msg)
    sys.exit(1)


def main():
    app = QApplication(sys.argv)
    install_exception_hooks("SoftEdIBO")

    # Diagnostic only — off unless SOFTEDIBO_WATCHDOG is set. Dumps the GUI
    # thread's stack to stderr whenever the event loop stalls (busy cursor).
    from src.gui.loop_watchdog import install_loop_watchdog
    install_loop_watchdog(app)

    if needs_setup():
        try:
            wizard = SetupWizard()
            if not wizard.exec():
                sys.exit(0)
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
