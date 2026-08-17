"""Participant management panel."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QDialog, QMessageBox, QTableWidget, QTableWidgetItem, QWidget

from src.data.database import Database
from src.data.models import ParticipantRecord
from src.gui.ui_participant_panel import Ui_ParticipantPanel


class ParticipantPanel(QWidget, Ui_ParticipantPanel):
    """Panel for creating, editing and deleting participants.

    Participants are stored in the SQLite database and their auto-generated
    IDs (P001, P002, ...) are shown in the table alongside alias and age.
    """

    def __init__(self, db: Database):
        super().__init__()
        self._db = db

        self.setupUi(self)

        self.participants_table.horizontalHeader().setStretchLastSection(True)

        self.add_btn.clicked.connect(self._on_add)
        self.edit_btn.clicked.connect(self._on_edit)
        self.delete_btn.clicked.connect(self._on_delete)
        self.participants_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.participants_table.itemDoubleClicked.connect(lambda _: self._on_edit())
        self.participants_table.keyPressEvent = self._on_table_key_press
        self.participants_table.setSortingEnabled(True)
        self._refresh()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        records = self._db.get_all_participants()
        self.participants_table.setRowCount(0)
        for record in records:
            row = self.participants_table.rowCount()
            self.participants_table.insertRow(row)
            id_item = QTableWidgetItem(record.participant_id)
            # Store the record for later retrieval
            id_item.setData(Qt.ItemDataRole.UserRole, record)
            self.participants_table.setItem(row, 0, id_item)
            self.participants_table.setItem(row, 1, QTableWidgetItem(record.alias))
            age_text = str(record.age) if record.age is not None else "-"
            self.participants_table.setItem(row, 2, QTableWidgetItem(age_text))

    def _selected_record(self) -> ParticipantRecord | None:
        rows = self.participants_table.selectedItems()
        if not rows:
            return None
        item = self.participants_table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_table_key_press(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Delete:
            self._on_delete()
        else:
            QTableWidget.keyPressEvent(self.participants_table, event)

    def _on_selection_changed(self) -> None:
        has_selection = bool(self.participants_table.selectedItems())
        self.edit_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_add(self) -> None:
        from src.gui.participant_dialog import ParticipantDialog

        new_id = self._db.next_participant_id()
        dlg = ParticipantDialog(new_id, self._db, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh()

    def _on_edit(self) -> None:
        record = self._selected_record()
        if record is None:
            return

        from src.gui.participant_dialog import ParticipantDialog

        dlg = ParticipantDialog(record.participant_id, self._db, record=record, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh()

    def _on_delete(self) -> None:
        record = self._selected_record()
        if record is None:
            return

        reply = QMessageBox.question(
            self,
            "Delete Participant",
            f"Delete participant {record.participant_id} ({record.alias})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._db.delete_participant(record.participant_id)
            self._refresh()
