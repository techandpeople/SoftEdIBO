"""Main entry point for the SoftEdIBO application."""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from PySide6.QtWidgets import QApplication

from src.gui.setup_wizard import SetupWizard, needs_setup


def main():
    app = QApplication(sys.argv)

    if needs_setup():
        wizard = SetupWizard()
        if not wizard.exec():
            # User cancelled setup — exit cleanly without opening the main window
            sys.exit(0)

    from src.gui.main_window import MainWindow

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
