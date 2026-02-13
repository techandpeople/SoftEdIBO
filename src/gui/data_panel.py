"""Data visualization panel for reviewing session data."""

from PyQt6.QtWidgets import (
    QGroupBox,
    QLabel,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)


class DataPanel(QWidget):
    """Panel for viewing and exporting collected session data."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # Sessions table
        sessions_group = QGroupBox("Past Sessions")
        sessions_layout = QVBoxLayout(sessions_group)
        self._sessions_table = QTableWidget()
        self._sessions_table.setColumnCount(4)
        self._sessions_table.setHorizontalHeaderLabels(
            ["Session ID", "Activity", "Start", "End"]
        )
        sessions_layout.addWidget(self._sessions_table)
        layout.addWidget(sessions_group)

        # Events table
        events_group = QGroupBox("Events")
        events_layout = QVBoxLayout(events_group)
        self._events_table = QTableWidget()
        self._events_table.setColumnCount(6)
        self._events_table.setHorizontalHeaderLabels(
            ["Time", "Participant", "Robot", "Action", "Target", "Metadata"]
        )
        events_layout.addWidget(self._events_table)
        layout.addWidget(events_group)

        # Export button
        self._export_btn = QPushButton("Export to CSV")
        layout.addWidget(self._export_btn)
