"""Main application window for SoftEdIBO."""

import sys

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.data.database import Database
from src.gui.data_panel import DataPanel
from src.gui.robot_panel import RobotPanel
from src.gui.session_panel import SessionPanel


class MainWindow(QMainWindow):
    """Main application window with tabbed panels."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SoftEdIBO - Robot Hospital")
        self.setMinimumSize(1024, 768)

        self._db = Database()
        self._db.connect()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        self._session_panel = SessionPanel(self._db)
        self._robot_panel = RobotPanel()
        self._data_panel = DataPanel(self._db)

        self._tabs.addTab(self._session_panel, "Session")
        self._tabs.addTab(self._robot_panel, "Robots")
        self._tabs.addTab(self._data_panel, "Data")

        self._session_panel.session_finished.connect(self._data_panel.refresh)

    def closeEvent(self, event) -> None:
        """Close the database connection on exit."""
        self._db.close()
        super().closeEvent(event)


def create_app() -> tuple[QApplication, MainWindow]:
    """Create and return the application and main window."""
    app = QApplication(sys.argv)
    window = MainWindow()
    return app, window
