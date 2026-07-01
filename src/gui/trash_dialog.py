"""Trash dialog — restore or permanently delete soft-deleted sessions."""

from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QMessageBox,
    QTableWidgetItem,
)

from src.core.session_trash import SessionTrash
from src.gui.ui_trash_dialog import Ui_TrashDialog


class TrashDialog(QDialog, Ui_TrashDialog):
    """Lists trashed sessions and lets the user restore or purge them.

    Args:
        trash: Session-trash service handling the database and recording files.
        parent: Optional parent widget.
    """

    def __init__(self, trash: SessionTrash, parent=None):
        super().__init__(parent)
        self.setupUi(self)

        self._trash = trash

        h = self.trash_table.horizontalHeader()
        for col in range(self.trash_table.columnCount() - 1):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(
            self.trash_table.columnCount() - 1, QHeaderView.ResizeMode.Stretch
        )

        self.trash_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.restore_btn.clicked.connect(self._on_restore)
        self.purge_btn.clicked.connect(self._on_purge)
        self.empty_trash_btn.clicked.connect(self._on_empty)
        self.button_box.rejected.connect(self.reject)

        self.refresh()

    def refresh(self) -> None:
        """Reload the trash table from the service."""
        self.trash_table.setRowCount(0)
        records = self._trash.list()
        for record in records:
            row = self.trash_table.rowCount()
            self.trash_table.insertRow(row)
            self.trash_table.setItem(row, 0, QTableWidgetItem(record.session_id))
            self.trash_table.setItem(row, 1, QTableWidgetItem(record.activity_name))
            self.trash_table.setItem(row, 2, QTableWidgetItem(
                record.start_time.isoformat(timespec="seconds")
            ))
            self.trash_table.setItem(row, 3, QTableWidgetItem(
                record.trashed_at.isoformat(timespec="seconds")
            ))
        self.restore_btn.setEnabled(False)
        self.purge_btn.setEnabled(False)
        self.empty_trash_btn.setEnabled(bool(records))

    def _selected_session_id(self) -> str | None:
        selected = self.trash_table.selectedItems()
        if not selected:
            return None
        return self.trash_table.item(selected[0].row(), 0).text()

    def _on_selection_changed(self) -> None:
        has_selection = bool(self.trash_table.selectedItems())
        self.restore_btn.setEnabled(has_selection)
        self.purge_btn.setEnabled(has_selection)

    def _on_restore(self) -> None:
        session_id = self._selected_session_id()
        if session_id is None:
            return
        self._trash.restore(session_id)
        self.refresh()

    def _on_purge(self) -> None:
        session_id = self._selected_session_id()
        if session_id is None:
            return
        if not self._confirm_permanent(
            f"Permanently delete session {session_id}?",
            "This session and its recording will be gone for good.",
        ):
            return
        self._trash.purge(session_id)
        self.refresh()

    def _on_empty(self) -> None:
        if not self._confirm_permanent(
            "Empty the trash?",
            "Every session in the trash and its recording will be gone for good.",
        ):
            return
        self._trash.empty()
        self.refresh()

    def _confirm_permanent(self, text: str, informative: str) -> bool:
        """Show a danger confirmation for an irreversible delete. True on Yes."""
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Delete Permanently")
        confirm.setText(text)
        confirm.setInformativeText(f"{informative} This cannot be undone.")
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        return confirm.exec() == QMessageBox.StandardButton.Yes
