"""Main entry point for the SoftEdIBO application."""

import logging
import sys
import traceback
from pathlib import Path

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
