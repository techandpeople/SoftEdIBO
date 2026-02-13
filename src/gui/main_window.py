"""Main application window for SoftEdIBO."""

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.gui.data_panel import DataPanel
from src.gui.robot_panel import RobotPanel
from src.gui.session_panel import SessionPanel


class MainWindow(QMainWindow):
    """Main application window with tabbed panels."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SoftEdIBO - Robot Hospital")
        self.setMinimumSize(1024, 768)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        self._session_panel = SessionPanel()
        self._robot_panel = RobotPanel()
        self._data_panel = DataPanel()

        self._tabs.addTab(self._session_panel, "Session")
        self._tabs.addTab(self._robot_panel, "Robots")
        self._tabs.addTab(self._data_panel, "Data")


def create_app() -> tuple[QApplication, MainWindow]:
    """Create and return the application and main window."""
    app = QApplication(sys.argv)
    window = MainWindow()
    return app, window
