"""Main entry point for the SoftEdIBO application."""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.gui.main_window import create_app


def main():
    app, window = create_app()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
