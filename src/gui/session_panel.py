"""Session control panel for managing study sessions."""

from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SessionPanel(QWidget):
    """Panel for creating, starting, and managing study sessions."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # Session info group
        info_group = QGroupBox("Session Info")
        info_layout = QVBoxLayout(info_group)

        id_row = QHBoxLayout()
        id_row.addWidget(QLabel("Session ID:"))
        self._session_id_input = QLineEdit()
        self._session_id_input.setPlaceholderText("Enter session ID...")
        id_row.addWidget(self._session_id_input)
        info_layout.addLayout(id_row)

        activity_row = QHBoxLayout()
        activity_row.addWidget(QLabel("Activity:"))
        self._activity_input = QLineEdit()
        self._activity_input.setPlaceholderText("Select activity...")
        activity_row.addWidget(self._activity_input)
        info_layout.addLayout(activity_row)

        layout.addWidget(info_group)

        # Controls
        controls = QHBoxLayout()
        self._start_btn = QPushButton("Start Session")
        self._pause_btn = QPushButton("Pause")
        self._stop_btn = QPushButton("Stop Session")

        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)

        controls.addWidget(self._start_btn)
        controls.addWidget(self._pause_btn)
        controls.addWidget(self._stop_btn)
        layout.addLayout(controls)

        # Status
        self._status_label = QLabel("Status: No active session")
        layout.addWidget(self._status_label)

        layout.addStretch()
