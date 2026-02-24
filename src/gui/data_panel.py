"""Data visualization panel for reviewing session data."""

import csv
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import (
    QFileDialog,
    QHeaderView,
    QMessageBox,
    QTableWidgetItem,
    QWidget,
)

from src.data.database import Database
from src.gui.ui_data_panel import Ui_DataPanel


class DataPanel(QWidget, Ui_DataPanel):
    """Panel for viewing and exporting collected session data.

    Args:
        db: Open database instance to load sessions and events from.
    """

    def __init__(self, db: Database):
        super().__init__()
        self.setupUi(self)

        self._db = db

        for table in (self.sessions_table, self.events_table):
            h = table.horizontalHeader()
            for col in range(table.columnCount() - 1):
                h.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            h.setSectionResizeMode(table.columnCount() - 1, QHeaderView.ResizeMode.Stretch)
            table.setSortingEnabled(True)

        self.sessions_table.itemSelectionChanged.connect(self._on_session_selected)
        self.export_btn.clicked.connect(self._on_export)
        self.export_all_btn.clicked.connect(self._on_export_all)

        self.refresh()

    def refresh(self) -> None:
        """Reload all sessions from the database."""
        self.sessions_table.setRowCount(0)
        self.events_table.setRowCount(0)

        for record in self._db.get_all_sessions():
            row = self.sessions_table.rowCount()
            self.sessions_table.insertRow(row)
            self.sessions_table.setItem(row, 0, QTableWidgetItem(record.session_id))
            self.sessions_table.setItem(row, 1, QTableWidgetItem(record.activity_name))
            self.sessions_table.setItem(row, 2, QTableWidgetItem(
                record.start_time.isoformat(timespec="seconds")
            ))
            self.sessions_table.setItem(row, 3, QTableWidgetItem(
                record.end_time.isoformat(timespec="seconds") if record.end_time else "—"
            ))

    def _on_session_selected(self) -> None:
        """Load events for the selected session into the events table."""
        selected = self.sessions_table.selectedItems()
        if not selected:
            return

        session_id = self.sessions_table.item(selected[0].row(), 0).text()
        events = self._db.get_session_events(session_id)

        self.events_table.setRowCount(0)
        for event in events:
            row = self.events_table.rowCount()
            self.events_table.insertRow(row)
            self.events_table.setItem(row, 0, QTableWidgetItem(
                event.timestamp.isoformat(timespec="seconds")
            ))
            self.events_table.setItem(row, 1, QTableWidgetItem(event.participant_id))
            self.events_table.setItem(row, 2, QTableWidgetItem(event.type))
            self.events_table.setItem(row, 3, QTableWidgetItem(event.action))
            self.events_table.setItem(row, 4, QTableWidgetItem(event.target))
            self.events_table.setItem(row, 5, QTableWidgetItem(event.metadata))

    def _on_export(self) -> None:
        """Export events for the selected session to a CSV file."""
        selected = self.sessions_table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "Export", "Select a session first.")
            return

        row = selected[0].row()
        session_id = self.sessions_table.item(row, 0).text()
        activity = self.sessions_table.item(row, 1).text()
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        default_name = str(Path(docs) / f"SoftEdIBO_{session_id}_{activity.replace(' ', '_')}.csv")

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Session Events", default_name, "CSV files (*.csv)"
        )
        if not path:
            return

        session = next(
            (s for s in self._db.get_all_sessions() if s.session_id == session_id), None
        )
        events = self._db.get_session_events(session_id)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["session_id", "activity", "start", "end",
                             "timestamp", "participant_id", "type", "action", "target", "metadata"])
            for event in events:
                writer.writerow([
                    session_id,
                    activity,
                    session.start_time.isoformat(timespec="seconds") if session else "",
                    session.end_time.isoformat(timespec="seconds") if session and session.end_time else "",
                    event.timestamp.isoformat(timespec="seconds"),
                    event.participant_id,
                    event.type,
                    event.action,
                    event.target,
                    event.metadata,
                ])

        QMessageBox.information(self, "Export", f"Exported to {path}")

    def _on_export_all(self) -> None:
        """Export all sessions and their events to a CSV file."""
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export All Sessions", str(Path(docs) / "SoftEdIBO_all_sessions.csv"), "CSV files (*.csv)"
        )
        if not path:
            return

        sessions = self._db.get_all_sessions()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["session_id", "activity", "start", "end",
                             "timestamp", "participant_id", "type", "action", "target", "metadata"])
            for session in sessions:
                for event in self._db.get_session_events(session.session_id):
                    writer.writerow([
                        session.session_id,
                        session.activity_name,
                        session.start_time.isoformat(timespec="seconds"),
                        session.end_time.isoformat(timespec="seconds") if session.end_time else "",
                        event.timestamp.isoformat(timespec="seconds"),
                        event.participant_id,
                        event.type,
                        event.action,
                        event.target,
                        event.metadata,
                    ])

        QMessageBox.information(self, "Export", f"Exported to {path}")
