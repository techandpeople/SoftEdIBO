"""Data visualization panel for reviewing session data."""

from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import (
    QFileDialog,
    QHeaderView,
    QMessageBox,
    QTableWidgetItem,
    QWidget,
)

from src.config.settings import Settings
from src.core.session_trash import SessionTrash
from src.data.database import Database
from src.data.export import SessionExporter
from src.gui.trash_dialog import TrashDialog
from src.gui.ui_data_panel import Ui_DataPanel


class DataPanel(QWidget, Ui_DataPanel):
    """Panel for viewing, exporting and deleting collected session data.

    Deletion is recoverable: "Delete Session" moves a session to the trash, and
    the "Trash…" button opens a dialog to restore or permanently delete it.

    Args:
        db: Open database instance to load sessions and events from.
    """

    def __init__(self, db: Database):
        super().__init__()
        self.setupUi(self)

        self._db = db
        self._exporter = SessionExporter(db)
        self._trash = SessionTrash(db, Settings().recordings_dir)

        for table in (self.sessions_table, self.events_table):
            h = table.horizontalHeader()
            for col in range(table.columnCount() - 1):
                h.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            h.setSectionResizeMode(table.columnCount() - 1, QHeaderView.ResizeMode.Stretch)
            table.setSortingEnabled(True)

        self.sessions_table.itemSelectionChanged.connect(self._on_session_selected)
        self.export_btn.clicked.connect(self._on_export)
        self.export_all_btn.clicked.connect(self._on_export_all)
        self.delete_btn.clicked.connect(self._on_delete)
        self.trash_btn.clicked.connect(self._on_open_trash)

        self.refresh()

    def refresh(self) -> None:
        """Reload all sessions from the database."""
        self.sessions_table.setRowCount(0)
        self.events_table.setRowCount(0)
        self.delete_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self._update_export_label(0)

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

    def _selected_rows(self) -> list[int]:
        """Rows the user has selected, in table order (one per selected row)."""
        return sorted({item.row() for item in self.sessions_table.selectedItems()})

    def _update_export_label(self, count: int) -> None:
        """Reflect the number of selected sessions in the export button label."""
        suffix = f" ({count})" if count > 1 else ""
        self.export_btn.setText(f"Export Selected{suffix} to CSV")

    def _on_session_selected(self) -> None:
        """Load events for the first selected session into the events table."""
        rows = self._selected_rows()
        self.delete_btn.setEnabled(bool(rows))
        self.export_btn.setEnabled(bool(rows))
        self._update_export_label(len(rows))
        if not rows:
            self.events_table.setRowCount(0)
            return

        session_id = self.sessions_table.item(rows[0], 0).text()
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
        """Export events for the selected session(s) to a single CSV file."""
        rows = self._selected_rows()
        if not rows:
            QMessageBox.warning(self, "Export", "Select a session first.")
            return

        session_ids = [self.sessions_table.item(r, 0).text() for r in rows]
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        if len(rows) == 1:
            activity = self.sessions_table.item(rows[0], 1).text()
            default_name = f"SoftEdIBO_{session_ids[0]}_{activity.replace(' ', '_')}.csv"
        else:
            default_name = "SoftEdIBO_selected_sessions.csv"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Selected Sessions", str(Path(docs) / default_name), "CSV files (*.csv)"
        )
        if not path:
            return

        # Flush pending async writes so freshly-logged events are included.
        self._db.flush_events()
        written = self._exporter.export_sessions(session_ids, path)
        QMessageBox.information(
            self, "Export",
            f"Exported {written} events from {len(session_ids)} session(s) to {path}",
        )

    def _on_export_all(self) -> None:
        """Export all sessions and their events to a CSV file."""
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export All Sessions", str(Path(docs) / "SoftEdIBO_all_sessions.csv"), "CSV files (*.csv)"
        )
        if not path:
            return

        self._db.flush_events()
        rows = self._exporter.export_all(path)
        QMessageBox.information(self, "Export", f"Exported {rows} events to {path}")

    def _on_delete(self) -> None:
        """Move the selected session(s) to the trash (recoverable)."""
        rows = self._selected_rows()
        if not rows:
            QMessageBox.warning(self, "Delete", "Select a session first.")
            return

        session_ids = [self.sessions_table.item(r, 0).text() for r in rows]
        if len(rows) == 1:
            activity = self.sessions_table.item(rows[0], 1).text()
            text = f"Move session {session_ids[0]} ({activity}) to the trash?"
        else:
            text = f"Move {len(session_ids)} sessions to the trash?"

        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle("Delete Session")
        confirm.setText(text)
        confirm.setInformativeText(
            "It leaves the list and can be restored (or deleted for good) from "
            "the Trash button."
        )
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        confirm.setDefaultButton(QMessageBox.StandardButton.Yes)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        for session_id in session_ids:
            self._trash.trash(session_id)
        self.refresh()

    def _on_open_trash(self) -> None:
        """Open the trash dialog, then refresh in case a session was restored."""
        TrashDialog(self._trash, self).exec()
        self.refresh()
