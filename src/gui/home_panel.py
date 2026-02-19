"""Introductory home panel for SoftEdIBO."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from src.gui.ui_home_panel import Ui_HomePanel


class HomePanel(QWidget, Ui_HomePanel):
    """Introductory home screen with status overview and quick navigation.

    Signals:
        navigate_to: Emitted with a tab name when a navigation button is clicked.
    """

    navigate_to = Signal(str)

    def __init__(self):
        super().__init__()
        self.setupUi(self)

        self.nav_participants_btn.clicked.connect(lambda: self.navigate_to.emit("Participants"))
        self.nav_session_btn.clicked.connect(lambda: self.navigate_to.emit("Session"))
        self.nav_robots_btn.clicked.connect(lambda: self.navigate_to.emit("Robots"))
        self.nav_data_btn.clicked.connect(lambda: self.navigate_to.emit("Data"))

    def set_session_status(self, session_id: str | None) -> None:
        """Update the session status label."""
        if session_id:
            self.session_status_label.setText(f"Running — {session_id}")
            self.session_status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
        else:
            self.session_status_label.setText("No active session")
            self.session_status_label.setStyleSheet("")

    def set_gateway_status(self, connected: bool) -> None:
        """Update the gateway connection status label."""
        if connected:
            self.gateway_status_label.setText("Connected")
            self.gateway_status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
        else:
            self.gateway_status_label.setText("Not connected")
            self.gateway_status_label.setStyleSheet("color: #f44336;")
